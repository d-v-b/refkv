//! The standalone writer (spec/bindings/phkv.md §23
//! <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#23-writing-a-phkv-store>), in the
//! recommended layout (§23.2
//! <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#232-recommended-layout-informative>):
//!
//! ```text
//! [bytes values]       as they are added, unless inline
//! [RECORDS]            in key order
//! [SLOTS]              8-byte aligned
//! [PROPERTY]*          8-byte aligned, in name order
//! [PAGES] [PTRHASH] [SOURCES] [METADATA]
//! [section directory]
//! [superblock]         D = 0
//! ```
//!
//! The same input gives the same file.

use crate::format::{self, section, Codec, DirEntry, Page, Prop, Record, Stored, Superblock, Value};
use crate::{ptrhash, Error};
use refkv_vzip::pb::{self, Range, Source};
use refkv_vzip::sources::{Keys, SourceTable};
use std::collections::{BTreeMap, HashMap};
use std::io::{Read, Write};
use std::path::Path;

pub(crate) fn invalid<T>(m: impl Into<String>) -> Result<T, Error> {
    Err(Error::Invalid(m.into()))
}

/// How the store is written.
#[derive(Clone, Copy, Debug)]
pub struct Options {
    /// The codec of bytes values added with [`Writer::add_bytes`].
    pub codec: Codec,
    /// Bytes values whose stored bytes are at most this long are inline in their records.
    pub inline_max: u64,
    /// Whether bytes records carry the CRC-32C of their values.
    pub crc32c: bool,
    /// The codec of the SOURCES, PAGES, PTRHASH and METADATA sections.
    pub section_codec: Codec,
    /// About how many bytes of records each page holds.
    pub page_size: u64,
    /// The superblock's distance from the end of the file: a probe distance (spec/bindings/phkv.md
    /// §17.2 <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#172-finding-the-superblock>).
    /// The writer reserves that many zero bytes after it, for data that is not the store's.
    pub distance: u64,
}

impl Default for Options {
    fn default() -> Options {
        Options { codec: Codec::None, inline_max: 4096, crc32c: true, section_codec: Codec::Zstd, page_size: 4096, distance: 0 }
    }
}

/// The store's keys, as the source table's checks see them: a `key` source must name a bytes value,
/// whose ranges are checked against its size.
struct StoreKeys<'a> {
    entries: &'a [Entry],
    names: &'a HashMap<String, usize>,
}

impl StoreKeys<'_> {
    fn value(&self, key: &str) -> Option<&Value> {
        self.names.get(key).map(|&i| &self.entries[i].value)
    }
}

impl Keys for StoreKeys<'_> {
    fn has(&self, key: &str) -> bool {
        self.names.contains_key(key)
    }

    fn is_ref(&self, key: &str) -> bool {
        matches!(self.value(key), Some(Value::Reference { .. }))
    }

    fn size(&self, key: &str) -> Option<u64> {
        match self.value(key) {
            Some(Value::Bytes { size, .. }) => Some(*size),
            _ => None,
        }
    }
}

/// Collects properties and metadata, and checks their names and types.
#[derive(Default)]
pub(crate) struct Props {
    /// name -> (type, key -> value)
    pub columns: BTreeMap<String, (u8, HashMap<String, Prop>)>,
    pub metadata: BTreeMap<String, Vec<u8>>,
}

impl Props {
    pub fn set(&mut self, key: &str, name: &str, value: Prop) -> Result<(), Error> {
        format::check_name(name, |n| format::registered_type(n).is_some()).map_err(Error::Invalid)?;
        let t = value.type_code();
        if let Some(rt) = format::registered_type(name)
            && rt != t
        {
            return invalid(format!("property {name:?} has type {rt}, not {t}"));
        }
        let col = self.columns.entry(name.to_string()).or_insert_with(|| (t, HashMap::new()));
        if col.0 != t {
            return invalid(format!("property {name:?} has values of types {} and {t}", col.0));
        }
        col.1.insert(key.to_string(), value);
        Ok(())
    }

    pub fn set_metadata(&mut self, name: &str, value: &[u8]) -> Result<(), Error> {
        format::check_name(name, |n| n == "writer").map_err(Error::Invalid)?;
        self.metadata.insert(name.to_string(), value.to_vec());
        Ok(())
    }

    /// Fails if a property names a key the store does not have.
    pub fn check_keys(&self, has: impl Fn(&str) -> bool) -> Result<(), Error> {
        for (name, (_, vals)) in &self.columns {
            let mut missing: Vec<&String> = vals.keys().filter(|k| !has(k)).collect();
            missing.sort();
            if let Some(k) = missing.first() {
                return invalid(format!("property {name:?} of missing key {k:?}"));
            }
        }
        Ok(())
    }
}

/// A key and its value, before the records are written.
pub(crate) struct Entry {
    pub key: String,
    pub hidden: bool,
    pub value: Value,
}

/// The sections that follow the values, built in memory from the entries (sorted here): RECORDS,
/// SLOTS, PROPERTY*, PAGES, PTRHASH, SOURCES (unless `sources` is given, a section already in the
/// file), METADATA, then the directory and superblock. `at` is the offset of the first byte.
pub(crate) struct Tail {
    pub bytes: Vec<u8>,
    pub superblock_end: u64,
}

pub(crate) struct TailInput<'a> {
    pub entries: &'a mut Vec<Entry>,
    pub sources: &'a SourceTable,
    pub sources_section: Option<DirEntry>,
    pub props: &'a Props,
    pub opts: &'a Options,
    pub hybrid: bool,
}

/// The sections, directory and superblock, from offset `at`: the superblock's `distance` and the
/// region are `d` and `region`, which the caller has worked out.
pub(crate) fn build_tail(input: TailInput, at: u64, d: u64, region_offset: u64, region_extra: u64) -> Result<Tail, Error> {
    let TailInput { entries, sources, sources_section, props, opts, hybrid } = input;
    entries.sort_unstable_by(|a, b| a.key.as_bytes().cmp(b.key.as_bytes()));
    let n = entries.len() as u64;
    let mut out: Vec<u8> = Vec::new();
    let mut dir: Vec<DirEntry> = Vec::new();
    let pos = |out: &Vec<u8>| at + out.len() as u64;
    let push = |out: &mut Vec<u8>, dir: &mut Vec<DirEntry>, kind: u32, codec: Codec, data: &[u8]| {
        let stored = format::encode(codec, data);
        dir.push(DirEntry { kind, codec: codec as u8, flags: 0, offset: at + out.len() as u64, length: stored.len() as u64, size: data.len() as u64, crc32c: format::crc32c(&stored) });
        out.extend_from_slice(&stored);
    };
    let align8 = |out: &mut Vec<u8>| {
        while (at + out.len() as u64) % 8 != 0 {
            out.push(0);
        }
    };

    if n > 0 {
        // RECORDS, and the pages over them
        let records_start = pos(&out);
        let mut offsets = Vec::with_capacity(entries.len());
        let mut lengths = Vec::with_capacity(entries.len());
        let mut pages: Vec<Page> = Vec::new();
        let mut page_start = records_start;
        let mut rec = Vec::new();
        for (rank, e) in entries.iter().enumerate() {
            let off = pos(&out);
            if rank == 0 || off - page_start >= opts.page_size {
                if let Some(p) = pages.last_mut() {
                    p.length = off - p.offset;
                }
                pages.push(Page { first_key: e.key.as_bytes().to_vec(), offset: off, length: 0 });
                page_start = off;
            }
            offsets.push(off);
            rec.clear();
            Record { key: e.key.as_bytes().to_vec(), rank: rank as u64, hidden: e.hidden, value: e.value.clone() }.encode(&mut rec);
            out.extend_from_slice(&rec);
            lengths.push(rec.len() as u64);
        }
        let records_end = pos(&out);
        if let Some(p) = pages.last_mut() {
            p.length = records_end - p.offset;
        }
        let records = &out[(records_start - at) as usize..];
        dir.push(DirEntry { kind: section::RECORDS, codec: 0, flags: 0, offset: records_start, length: records.len() as u64, size: records.len() as u64, crc32c: format::crc32c(records) });

        // the perfect hash, and SLOTS
        let keys: Vec<&[u8]> = entries.iter().map(|e| e.key.as_bytes()).collect();
        let built = ptrhash::build(&keys).ok_or_else(|| Error::Invalid("no seed places every key (duplicate keys?)".into()))?;
        align8(&mut out);
        let mut slots = Vec::with_capacity(built.slots.len() * format::SLOT_SIZE as usize);
        for s in &built.slots {
            let slot = match s {
                None => format::EMPTY_SLOT,
                Some(i) => format::pack_slot(offsets[*i as usize] - records_start, lengths[*i as usize]),
            };
            slots.extend_from_slice(&slot);
        }
        push(&mut out, &mut dir, section::SLOTS, Codec::None, &slots);

        // PROPERTY sections, in name order
        let ranks: HashMap<&str, usize> = entries.iter().enumerate().map(|(r, e)| (e.key.as_str(), r)).collect();
        for (name, (t, vals)) in &props.columns {
            let mut col: Vec<Option<Prop>> = vec![None; entries.len()];
            for (k, v) in vals {
                col[ranks[k.as_str()]] = Some(v.clone());
            }
            align8(&mut out);
            push(&mut out, &mut dir, section::PROPERTY, Codec::None, &format::encode_property(name, *t, &col));
        }
        push(&mut out, &mut dir, section::PAGES, opts.section_codec, &format::encode_pages(&pages));
        push(&mut out, &mut dir, section::PTRHASH, opts.section_codec, &format::encode_ptrhash(&built.params, &built.pilots));
    } else if !props.columns.is_empty() {
        return invalid("properties in a store with no keys");
    }
    match sources_section {
        Some(s) => dir.push(s),
        None => push(&mut out, &mut dir, section::SOURCES, opts.section_codec, &sources.encode()),
    }
    if !props.metadata.is_empty() {
        let entries: Vec<(String, Vec<u8>)> = props.metadata.iter().map(|(k, v)| (k.clone(), v.clone())).collect();
        push(&mut out, &mut dir, section::METADATA, opts.section_codec, &format::encode_metadata(&entries));
    }
    let directory_offset = pos(&out);
    let mut dbytes = Vec::with_capacity(dir.len() * 40);
    for e in &dir {
        e.encode(&mut dbytes);
    }
    out.extend_from_slice(&dbytes);
    let superblock_end = pos(&out) + format::SUPERBLOCK_SIZE;
    let sb = Superblock {
        version: format::VERSION,
        flags: if hybrid { format::FLAG_HYBRID } else { 0 },
        distance: d,
        key_count: n,
        directory_offset,
        directory_length: dbytes.len() as u64,
        region_offset,
        region_length: superblock_end - region_offset + region_extra,
        directory_crc32c: format::crc32c(&dbytes),
    };
    out.extend_from_slice(&sb.encode());
    Ok(Tail { bytes: out, superblock_end })
}

/// Writes a standalone phkv store to `W`.
pub struct Writer<W: Write> {
    w: W,
    pos: u64,
    opts: Options,
    entries: Vec<Entry>,
    names: HashMap<String, usize>,
    sources: SourceTable,
    props: Props,
}

impl<W: Write> Writer<W> {
    pub fn new(w: W, opts: Options) -> Writer<W> {
        Writer { w, pos: 0, opts, entries: Vec::new(), names: HashMap::new(), sources: SourceTable::default(), props: Props::default() }
    }

    /// Interns `src` into the source table, and gives its index for `Range::source`.
    pub fn source(&mut self, src: Source) -> Result<u32, Error> {
        Ok(self.sources.add(src)?)
    }

    pub fn url(&mut self, url: &str) -> Result<u32, Error> {
        self.source(Source::url(url))
    }

    pub fn internal(&mut self, key: &str) -> Result<u32, Error> {
        self.source(Source::key(key))
    }

    pub fn blob(&mut self, data: &[u8]) -> Result<u32, Error> {
        self.source(Source::data(data))
    }

    fn write(&mut self, b: &[u8]) -> Result<(), Error> {
        self.w.write_all(b)?;
        self.pos += b.len() as u64;
        Ok(())
    }

    /// Fails for a key the store cannot take. A key is registered only by `push`, once its value
    /// is written, so a failed add leaves no key behind.
    fn check_key(&self, key: &str) -> Result<(), Error> {
        if key.is_empty() {
            return invalid("empty key");
        }
        if self.names.contains_key(key) {
            return invalid(format!("duplicate key {key:?}"));
        }
        Ok(())
    }

    fn push(&mut self, entry: Entry) {
        self.names.insert(entry.key.clone(), self.entries.len());
        self.entries.push(entry);
    }

    /// Adds a bytes value, stored with the options' codec.
    pub fn add_bytes(&mut self, key: &str, data: &[u8]) -> Result<(), Error> {
        self.add_bytes_with(key, data, self.opts.codec)
    }

    /// Adds a bytes value, stored with `codec`: inline if short enough, else written now.
    pub fn add_bytes_with(&mut self, key: &str, data: &[u8], codec: Codec) -> Result<(), Error> {
        self.check_key(key)?;
        let stored = format::encode(codec, data);
        let crc = self.opts.crc32c.then(|| format::crc32c(data));
        let stored_length = stored.len() as u64;
        let at = if stored_length <= self.opts.inline_max {
            Stored::Inline(stored)
        } else {
            let at = self.pos;
            self.write(&stored)?;
            Stored::At(at)
        };
        self.push(Entry { key: key.into(), hidden: false, value: Value::Bytes { codec, stored: at, stored_length, size: data.len() as u64, crc32c: crc } });
        Ok(())
    }

    /// Adds the bytes of the file at `path`, uncompressed, streaming it. If reading fails partway,
    /// the bytes already written stay in the output, and no key refers to them.
    pub fn add_file(&mut self, key: &str, path: &Path) -> Result<(), Error> {
        self.check_key(key)?;
        let mut f = std::fs::File::open(path)?;
        let at = self.pos;
        let mut buf = vec![0u8; 1 << 24];
        let mut crc = 0u32;
        loop {
            let n = f.read(&mut buf)?;
            if n == 0 {
                break;
            }
            crc = crc32c::crc32c_append(crc, &buf[..n]);
            self.write(&buf[..n])?;
        }
        let size = self.pos - at;
        let crc = self.opts.crc32c.then_some(crc);
        self.push(Entry { key: key.into(), hidden: false, value: Value::Bytes { codec: Codec::None, stored: Stored::At(at), stored_length: size, size, crc32c: crc } });
        Ok(())
    }

    /// Adds a reference whose value is the concatenation of `ranges`.
    pub fn add_ranges(&mut self, key: &str, ranges: &[Range]) -> Result<(), Error> {
        self.check_key(key)?;
        self.sources.check_ranges(key, ranges)?;
        let (kind, payload) = if ranges.len() == 1 { (format::KIND_RANGE, ranges[0].encode()) } else { (format::KIND_CONCAT, pb::encode_concat(ranges)) };
        if payload.len() as u64 > format::MAX_PAYLOAD {
            return invalid(format!("the payload of {key:?} exceeds 2^24 bytes"));
        }
        // only a reference the writer takes leaves ranges to check at close
        self.sources.keep(key, ranges);
        self.push(Entry { key: key.into(), hidden: false, value: Value::Reference { kind, payload } });
        Ok(())
    }

    /// Sets a property of a key (added before or after).
    pub fn set_property(&mut self, key: &str, name: &str, value: Prop) -> Result<(), Error> {
        self.props.set(key, name, value)
    }

    /// Sets an entry of the store's METADATA section.
    pub fn set_metadata(&mut self, name: &str, value: &[u8]) -> Result<(), Error> {
        self.props.set_metadata(name, value)
    }

    /// Writes the records and the other sections, and gives the output back.
    pub fn close(mut self) -> Result<W, Error> {
        if !format::probe_distances().any(|d| d == self.opts.distance) {
            return invalid(format!("{} is not a probe distance", self.opts.distance));
        }
        // a standalone store reserves no names
        self.sources.check(&StoreKeys { entries: &self.entries, names: &self.names }, &[])?;
        self.props.check_keys(|k| self.names.contains_key(k))?;
        let at = self.pos;
        let tail = build_tail(
            TailInput { entries: &mut self.entries, sources: &self.sources, sources_section: None, props: &self.props, opts: &self.opts, hybrid: false },
            at,
            self.opts.distance,
            0,
            0,
        )?;
        self.write(&tail.bytes)?;
        let zeros = [0u8; 1 << 16];
        let mut left = self.opts.distance;
        while left > 0 {
            let n = left.min(zeros.len() as u64) as usize;
            self.write(&zeros[..n])?;
            left -= n as u64;
        }
        self.w.flush()?;
        Ok(self.w)
    }
}
