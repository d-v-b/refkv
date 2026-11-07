//! `seal`: the check that vouches for an output, and the numbering of its source table (the
//! design's "Building and writing",
//! <https://github.com/d-v-b/refkv/blob/main/design/proposals/profile-plugins.md#4-building-and-writing>).
//! It is one function for every output: each host's built-in output passes through it (`out`),
//! and `refkv.build.seal` calls it for an output a caller builds. It reads nothing: the hosts read
//! the ranges' checksums after it, and it measures each checksum at its largest.

use crate::out::{Out, Part, MAX_PAYLOAD};
use serde_json::Value as J;
use std::collections::{HashMap, HashSet};
use std::fmt;

/// The bytes read at open, and the largest zero data source (spec/refkv.md §6.7
/// <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>'s default
/// `max_format_entry`).
pub const MAX_OPEN: u64 = 256 << 20;
/// The sources of a table a reader opens (spec/refkv.md §6.7's `max_sources`).
pub const MAX_SOURCES: usize = 1 << 22;
/// The url reads of one value (spec/refkv.md §6.7 rule 5).
pub const MAX_READS: usize = 1024;
/// Ranges of one source this close are one read, as refkv's readers merge them.
pub const MERGE_GAP: u64 = 1 << 16;
/// The bytes of a key (spec/bindings/vzip.md, a ZIP entry's name).
pub const MAX_KEY: usize = 65535;
/// The keys the ZIP binding keeps for itself.
pub const RESERVED_PREFIX: &str = "__vz__/";
/// The most bytes a range's `crc32c` takes: a tag, and a varint of 32 bits (spec/refkv.md §4.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#42-range>).
pub const CRC_FIELD: usize = 6;

/// What a refusal is of.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Code {
    /// a key no archive holds
    Key,
    /// a part that names nothing of the request
    Value,
    /// a range outside its source
    Range,
    /// a payload over the limit
    Payload,
    /// a payload over the limit only with its checksums: a failure, not a rejection
    Checksums,
    /// a value whose reads pass the reader's limit
    Reads,
    /// inputs that do not go together
    Inputs,
    /// a table or source past an archive's limits
    Archive,
}

impl Code {
    pub fn name(self) -> &'static str {
        match self {
            Code::Key => "key",
            Code::Value => "value",
            Code::Range => "range",
            Code::Payload => "payload",
            Code::Checksums => "checksums",
            Code::Reads => "reads",
            Code::Inputs => "inputs",
            Code::Archive => "archive",
        }
    }
}

/// Why `seal` refuses an output: its code, the entry's key (None for the output as a whole), and
/// the reason.
#[derive(Clone, Debug, PartialEq)]
pub struct SealError {
    pub code: Code,
    pub key: Option<String>,
    pub message: String,
}

impl SealError {
    fn new(code: Code, key: Option<&str>, message: String) -> SealError {
        SealError { code, key: key.map(str::to_string), message }
    }
}

impl fmt::Display for SealError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match &self.key {
            Some(k) => write!(f, "{k}: {}", self.message),
            None => f.write_str(&self.message),
        }
    }
}

/// Why a built-in output is not written: a rejection of the input, or a failure of the
/// zarrifier (spec/zarrify.md §1.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>).
#[derive(Clone, Debug, PartialEq)]
pub enum Refusal {
    Rejected(String),
    Failed(String),
}

impl From<String> for Refusal {
    fn from(e: String) -> Refusal {
        Refusal::Rejected(e)
    }
}

impl From<SealError> for Refusal {
    fn from(e: SealError) -> Refusal {
        match e.code {
            Code::Checksums => Refusal::Failed(e.to_string()),
            _ => Refusal::Rejected(e.to_string()),
        }
    }
}

impl From<Refusal> for String {
    fn from(r: Refusal) -> String {
        match r {
            Refusal::Rejected(e) | Refusal::Failed(e) => e,
        }
    }
}

/// A url source as an input found it: equal ones are one source.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Url {
    pub url: String,
    pub size: Option<u64>,
    pub etag: Option<String>,
}

/// A part of a value: a range of a url source (its index among the request's `urls`) or of a
/// data string (among its `data`), zero bytes, or literal bytes.
#[derive(Clone, Debug, PartialEq)]
pub enum Piece {
    Url(usize, u64, u64),
    Data(usize, u64, u64),
    Zeros(u64),
    Lit(Vec<u8>),
}

/// A reference entry: its key, its parts, and whether a check of refkv's lifts the read rule
/// for it (the host decides; `seal` lifts nothing under a reader stricter than refkv's).
#[derive(Clone, Debug, PartialEq)]
pub struct Value {
    pub key: String,
    pub pieces: Vec<Piece>,
    pub lifted: bool,
}

/// What the store's reader allows (refkv's limits, or fewer), and whether the hosts record the
/// ranges' checksums.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Limits {
    pub max_sources: usize,
    pub max_reads: usize,
    pub checksums: bool,
}

impl Default for Limits {
    fn default() -> Limits {
        Limits { max_sources: MAX_SOURCES, max_reads: MAX_READS, checksums: false }
    }
}

/// A source of the sealed table: a url source (the index of the first of the request's `urls`
/// equal to it), a data string (the index of the first equal one among `data`), or the zero
/// data source of so many bytes.
#[derive(Clone, Debug, PartialEq)]
pub enum Source {
    Url(usize),
    Data(usize),
    Zeros(u64),
}

/// A range of a sealed value: (source, offset, length) of the sealed table, or literal bytes.
#[derive(Clone, Debug, PartialEq)]
pub enum Range {
    Src(u32, u64, u64),
    Lit(Vec<u8>),
}

/// A sealed output: its source table, and its values in the order of their keys (UTF-8), each
/// with one range per piece, in order.
#[derive(Clone, Debug, PartialEq)]
pub struct Sealed {
    pub sources: Vec<Source>,
    pub values: Vec<(String, Vec<Range>)>,
}

/// Why `key` is no key an archive holds, or None.
pub fn key_problem(key: &str) -> Option<String> {
    if key.is_empty() {
        return Some("a key is not empty".into());
    }
    if key.len() > MAX_KEY {
        return Some(format!("a key of {} bytes, more than an archive's {MAX_KEY}", key.len()));
    }
    if key.starts_with(RESERVED_PREFIX) {
        return Some(format!("keys beginning {RESERVED_PREFIX:?} are the ZIP binding's own"));
    }
    None
}

fn varint_size(v: u64) -> usize {
    (64 - v.leading_zeros() as usize).div_ceil(7).max(1)
}

/// A range's payload, as written (spec/refkv.md §4.4
/// <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#44-reference-payloads>), without a
/// checksum.
fn range_size(r: &Range) -> usize {
    match r {
        Range::Lit(b) => 1 + varint_size(b.len() as u64) + b.len(),
        Range::Src(s, o, n) => [*s as u64, *o, *n].iter().filter(|&&v| v != 0).map(|&v| 1 + varint_size(v)).sum(),
    }
}

/// The payload of a value of ranges whose sizes are `sizes`.
fn payload(sizes: &[usize]) -> usize {
    match sizes {
        [one] => *one,
        _ => sizes.iter().map(|&n| 1 + varint_size(n as u64) + n).sum(),
    }
}

/// The url reads `ranges` make, ranges of one source at most `MERGE_GAP` apart counted once.
fn reads(ranges: &[Range], url: &[bool]) -> usize {
    let mut spans: HashMap<u32, Vec<(u64, u64)>> = HashMap::new();
    for r in ranges {
        if let Range::Src(s, o, n) = *r {
            if n > 0 && url[s as usize] {
                spans.entry(s).or_default().push((o, o.saturating_add(n)));
            }
        }
    }
    let mut count = 0;
    for got in spans.values_mut() {
        got.sort_unstable();
        let mut end: Option<u64> = None;
        for &(a, b) in got.iter() {
            if end.is_none_or(|e| a > e.saturating_add(MERGE_GAP)) {
                count += 1;
            }
            end = Some(end.map_or(b, |e| e.max(b)));
        }
    }
    count
}

#[derive(Clone, Copy, PartialEq, Eq, Hash)]
enum Slot {
    Data(usize),
    Zeros,
}

/// Seals an output: `documents` are the keys of its bytes entries and `values` its reference
/// entries, whose url pieces name `urls` and data pieces `data`; `first`, a url that is source 0
/// whether or not a value uses it (one input that is a plain file, spec/zarrify.md §1.2).
///
/// The table is numbered by first use over the values in the order of their keys (UTF-8), within
/// a value in the order of its pieces: the url sources first, then the data sources (each distinct
/// data string, and the zero data source, as long as the most `Zeros` any piece holds), in one
/// order of first use. Equal url sources (URL and pins) are one; one URL with different pins is
/// refused. Then each key, range and payload is checked, in the order of the keys: an unlifted
/// value reads at most `max_reads` url ranges, counted as readers merge them, and a payload is at
/// most 65519 bytes, and when the hosts record checksums, with each url range's checksum at its
/// largest (`CRC_FIELD`).
pub fn seal(urls: &[Url], data: &[Vec<u8>], first: Option<usize>, documents: &[String], mut values: Vec<Value>,
            limits: &Limits) -> Result<Sealed, SealError> {
    let mut keys: Vec<&str> = documents.iter().map(String::as_str).chain(values.iter().map(|v| v.key.as_str())).collect();
    keys.sort_unstable_by(|a, b| a.as_bytes().cmp(b.as_bytes()));
    for (i, k) in keys.iter().enumerate() {
        if let Some(why) = key_problem(k) {
            return Err(SealError::new(Code::Key, Some(k), why));
        }
        if i > 0 && keys[i - 1] == *k {
            return Err(SealError::new(Code::Key, Some(k), "a key is given twice".into()));
        }
    }
    values.sort_unstable_by(|a, b| a.key.as_bytes().cmp(b.key.as_bytes()));

    // url sources: each request url's table position, by first use
    let mut identity: HashMap<&Url, usize> = HashMap::new();
    let mut pins: HashMap<&str, &Url> = HashMap::new();
    let mut url_at: Vec<Option<usize>> = vec![None; urls.len()];
    let mut table_urls: Vec<usize> = Vec::new();
    let mut use_url = |i: usize, key: Option<&str>| -> Result<usize, SealError> {
        if let Some(t) = url_at[i] {
            return Ok(t);
        }
        let u = &urls[i];
        if let Some(other) = pins.insert(u.url.as_str(), u) {
            if (other.size, &other.etag) != (u.size, &u.etag) {
                return Err(SealError::new(Code::Inputs, key, format!(
                    "{} with different pins in two inputs: the file changed between two reads", u.url)));
            }
        }
        let t = *identity.entry(u).or_insert_with(|| {
            table_urls.push(i);
            table_urls.len() - 1
        });
        url_at[i] = Some(t);
        Ok(t)
    };
    if let Some(f) = first {
        if f >= urls.len() {
            return Err(SealError::new(Code::Value, None, format!("source 0 is url {f}, which the request lacks")));
        }
        use_url(f, None)?;
    }
    // data sources: each distinct string, and the zero source, by first use
    let mut canonical: HashMap<&[u8], usize> = HashMap::new();
    let data_of: Vec<usize> = data.iter().enumerate().map(|(i, d)| *canonical.entry(d.as_slice()).or_insert(i)).collect();
    let mut slots: Vec<Slot> = Vec::new();
    let mut seen: HashSet<Slot> = HashSet::new();
    let mut zeros = 0u64;
    for v in &values {
        let key = Some(v.key.as_str());
        for p in &v.pieces {
            let slot = match *p {
                Piece::Url(i, o, n) => {
                    if i >= urls.len() {
                        return Err(SealError::new(Code::Value, key, format!("url {i} is not among the request's")));
                    }
                    use_url(i, key)?;
                    let u = &urls[i];
                    let Some(size) = u.size else {
                        return Err(SealError::new(Code::Range, key, format!(
                            "{}'s size is not known, so a range of it cannot be checked", u.url)));
                    };
                    match o.checked_add(n) {
                        Some(end) if end <= size => {}
                        _ => return Err(SealError::new(Code::Range, key, format!(
                            "bytes {o} to {} of {}, past its {size}", o as u128 + n as u128, u.url))),
                    }
                    continue;
                }
                Piece::Data(i, o, n) => {
                    let Some(d) = data.get(i) else {
                        return Err(SealError::new(Code::Value, key, format!("data {i} is not among the request's")));
                    };
                    if o.checked_add(n).is_none_or(|end| end > d.len() as u64) {
                        return Err(SealError::new(Code::Range, key, "a data range's bytes lie outside its data".into()));
                    }
                    Slot::Data(data_of[i])
                }
                Piece::Zeros(n) => {
                    zeros = zeros.max(n);
                    Slot::Zeros
                }
                Piece::Lit(_) => continue,
            };
            if seen.insert(slot) {
                slots.push(slot);
            }
        }
    }
    if zeros > MAX_OPEN {
        return Err(SealError::new(Code::Archive, None, format!(
            "a zero data source of {zeros} bytes, more than the {MAX_OPEN} of a source table")));
    }
    let mut sources: Vec<Source> = table_urls.iter().map(|&i| Source::Url(i)).collect();
    let mut slot_at: HashMap<Slot, u32> = HashMap::new();
    for s in slots {
        slot_at.insert(s, sources.len() as u32);
        sources.push(match s {
            Slot::Data(i) => Source::Data(i),
            Slot::Zeros => Source::Zeros(zeros),
        });
    }
    let most = limits.max_sources.min(MAX_SOURCES);
    if sources.len() > most {
        return Err(SealError::new(Code::Archive, None, format!(
            "{} sources, more than the {most} its reader allows", sources.len())));
    }

    let url: Vec<bool> = sources.iter().map(|s| matches!(s, Source::Url(_))).collect();
    let max_reads = limits.max_reads.min(MAX_READS);
    let mut sealed = Vec::with_capacity(values.len());
    for v in values {
        let key = Some(v.key.as_str());
        let ranges: Vec<Range> = v.pieces.into_iter().map(|p| match p {
            Piece::Url(i, o, n) => Range::Src(url_at[i].unwrap_or(0) as u32, o, n),
            Piece::Data(i, o, n) => Range::Src(slot_at[&Slot::Data(data_of[i])], o, n),
            Piece::Zeros(n) => Range::Src(slot_at[&Slot::Zeros], 0, n),
            Piece::Lit(b) => Range::Lit(b),
        }).collect();
        // a reader stricter than refkv's lifts nothing
        if !(v.lifted && max_reads == MAX_READS) {
            let n = reads(&ranges, &url);
            if n > max_reads {
                return Err(SealError::new(Code::Reads, key, format!(
                    "it reads {n} ranges, more than the {max_reads} its reader makes for one value (spec/refkv.md \
                     §6.7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>)")));
            }
        }
        let mut sizes: Vec<usize> = ranges.iter().map(range_size).collect();
        let bare = payload(&sizes);
        if bare > MAX_PAYLOAD {
            return Err(SealError::new(Code::Payload, key, format!("a payload of {bare} bytes, more than {MAX_PAYLOAD}")));
        }
        if limits.checksums {
            for (n, r) in sizes.iter_mut().zip(&ranges) {
                if matches!(r, Range::Src(s, ..) if url[*s as usize]) {
                    *n += CRC_FIELD;
                }
            }
            let most = payload(&sizes);
            if most > MAX_PAYLOAD {
                return Err(SealError::new(Code::Checksums, key, format!(
                    "a payload of up to {most} bytes with its checksums, more than {MAX_PAYLOAD}")));
            }
        }
        sealed.push((v.key, ranges));
    }
    Ok(Sealed { sources, values: sealed })
}

/// Whether the array document `doc` shards its chunks (`sharding_indexed`, after any transpose).
fn sharded(doc: &[u8]) -> bool {
    let Ok(v) = serde_json::from_slice::<J>(doc) else { return false };
    v["node_type"] == "array"
        && v["codecs"].as_array().is_some_and(|cs| cs.iter().any(|c| c["name"] == "sharding_indexed"))
}

/// Seals a built-in output, as every host writes it: its url sources are the file (source 0, of
/// `size` bytes) for a file run, and for a store run the objects its references use (`objects`,
/// each key's size); its data strings are `out.data`. A chunk of a sharded array is read in parts
/// (spec/zarrify.md §2.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#21-virtual-shards>),
/// so the read rule is lifted for the references under an array document that shards; every
/// other reference meets it. On success `out.data` holds only the data sources a reference uses,
/// renumbered in `seal`'s order (from 1, the hosts' numbering), and its references name them so.
pub fn out(out: &mut Out, size: Option<u64>, objects: &HashMap<String, u64>, checksums: bool) -> Result<(), SealError> {
    let mut urls: Vec<Url> = Vec::new();
    let mut object_at: HashMap<&str, usize> = HashMap::new();
    let first = size.map(|n| {
        urls.push(Url { url: out.url.clone(), size: Some(n), etag: None });
        0
    });
    let mut docs: HashMap<&str, bool> = HashMap::new();
    for (k, b) in &out.entries {
        if k == "zarr.json" || k.ends_with("/zarr.json") {
            docs.insert(k.strip_suffix("zarr.json").unwrap_or(k), sharded(b));
        }
    }
    let lifted = |key: &str| -> bool {
        let mut at = key.len();
        while let Some(cut) = key[..at].rfind('/') {
            if let Some(&s) = docs.get(&key[..cut + 1]) {
                return s;
            }
            at = cut;
        }
        docs.get("").copied().unwrap_or(false)
    };
    let mut values = Vec::with_capacity(out.refs.len());
    for (k, parts) in &out.refs {
        let mut pieces = Vec::with_capacity(parts.len());
        for p in parts {
            pieces.push(match p {
                Part::Src(o, n) => match first {
                    Some(f) => Piece::Url(f, *o, *n),
                    None => return Err(SealError::new(Code::Value, Some(k), "internal: a range of source 0 in a store's output".into())),
                },
                Part::Obj(key, o, n) => {
                    let i = *object_at.entry(key.as_str()).or_insert_with(|| {
                        urls.push(Url { url: key.clone(), size: objects.get(key).copied(), etag: None });
                        urls.len() - 1
                    });
                    Piece::Url(i, *o, *n)
                }
                Part::Data(i, o, n) => Piece::Data((*i as usize).wrapping_sub(1), *o, *n),
                Part::Lit(b) => Piece::Lit(b.clone()),
            });
        }
        values.push(Value { key: k.clone(), pieces, lifted: lifted(k) });
    }
    let documents: Vec<String> = out.entries.iter().map(|e| e.0.clone()).collect();
    let limits = Limits { checksums, ..Limits::default() };
    let sealed = seal(&urls, &out.data, first, &documents, values, &limits)?;
    let order: Vec<usize> = sealed.sources.iter().filter_map(|s| match s {
        Source::Data(i) => Some(*i),
        _ => None,
    }).collect();
    out.renumber_data(&order);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn url(u: &str, size: Option<u64>, etag: Option<&str>) -> Url {
        Url { url: u.into(), size, etag: etag.map(str::to_string) }
    }

    fn value(key: &str, pieces: Vec<Piece>) -> Value {
        Value { key: key.into(), pieces, lifted: false }
    }

    fn refused(r: Result<Sealed, SealError>) -> (Code, Option<String>) {
        let e = r.unwrap_err();
        (e.code, e.key)
    }

    #[test]
    fn numbers_by_first_use_in_key_order() {
        let urls = [url("a", Some(100), None), url("b", Some(100), Some("\"t\"")), url("a", Some(100), None),
                    url("c", Some(100), None)];
        let data = [b"jpeg".to_vec(), b"tail".to_vec(), b"jpeg".to_vec()];
        let values = vec![
            // given out of key order: "z" uses url 3 and data 0 first, but "m" sorts before it
            value("z", vec![Piece::Url(3, 0, 4), Piece::Data(2, 0, 4), Piece::Zeros(9)]),
            value("m", vec![Piece::Url(1, 10, 5), Piece::Lit(b"x".to_vec()), Piece::Zeros(3), Piece::Data(1, 1, 2)]),
            value("m/c/0", vec![Piece::Url(2, 0, 100)]),
        ];
        let cases: [(Option<usize>, Vec<Source>); 2] = [
            // no source 0: url 0 is url 2's identity, first used by "m/c/0"
            (None, vec![Source::Url(1), Source::Url(2), Source::Url(3), Source::Zeros(9), Source::Data(1), Source::Data(0)]),
            // a plain file: url 0 is source 0, and url 2 is the same source
            (Some(0), vec![Source::Url(0), Source::Url(1), Source::Url(3), Source::Zeros(9), Source::Data(1), Source::Data(0)]),
        ];
        for (first, want) in cases {
            let s = seal(&urls, &data, first, &["zarr.json".into()], values.clone(), &Limits::default()).unwrap();
            assert_eq!(s.sources, want, "{first:?}");
            let keys: Vec<&str> = s.values.iter().map(|v| v.0.as_str()).collect();
            assert_eq!(keys, ["m", "m/c/0", "z"]);
            let at = |src: &Source| want.iter().position(|s| s == src).unwrap() as u32;
            let a = at(&Source::Url(if first.is_some() { 0 } else { 2 }));
            assert_eq!(s.values[0].1, vec![Range::Src(at(&Source::Url(1)), 10, 5), Range::Lit(b"x".to_vec()),
                                           Range::Src(at(&Source::Zeros(9)), 0, 3), Range::Src(at(&Source::Data(1)), 1, 2)]);
            assert_eq!(s.values[1].1, vec![Range::Src(a, 0, 100)]);
            assert_eq!(s.values[2].1, vec![Range::Src(at(&Source::Url(3)), 0, 4), Range::Src(at(&Source::Data(0)), 0, 4),
                                           Range::Src(at(&Source::Zeros(9)), 0, 9)]);
        }
        // a lifted value may read past the rule; checksums fit a small payload
        let many: Vec<Piece> = (0..2000).map(|k| Piece::Url(0, k * (1 << 17), 1)).collect();
        let lifted = Value { key: "s/c/0".into(), pieces: many, lifted: true };
        let limits = Limits { checksums: true, ..Limits::default() };
        assert!(seal(&[url("a", Some(1 << 40), None)], &[], None, &[], vec![lifted], &limits).is_ok());
    }

    #[test]
    fn refuses_a_bad_key() {
        for (key, documents) in [("", vec![]), ("__vz__/x", vec![]), (&*"k".repeat(MAX_KEY + 1), vec![]),
                                 ("a", vec!["a".to_string()])] {
            let r = seal(&[], &[], None, &documents, vec![value(key, vec![])], &Limits::default());
            assert_eq!(refused(r), (Code::Key, Some(key.to_string())), "{key:.10}");
        }
    }

    #[test]
    fn refuses_a_piece_that_names_nothing() {
        for p in [Piece::Url(1, 0, 1), Piece::Data(0, 0, 1)] {
            let r = seal(&[url("a", Some(9), None)], &[], None, &[], vec![value("k", vec![p])], &Limits::default());
            assert_eq!(refused(r), (Code::Value, Some("k".into())));
        }
        let r = seal(&[], &[], Some(0), &[], vec![], &Limits::default());
        assert_eq!(refused(r), (Code::Value, None));
    }

    #[test]
    fn refuses_a_range_outside_its_source() {
        let urls = [url("a", Some(10), None), url("b", None, None)];
        for p in [Piece::Url(0, 8, 3), Piece::Url(0, u64::MAX, 2), Piece::Url(1, 0, 1), Piece::Data(0, 2, 3)] {
            let r = seal(&urls, &[b"abcd".to_vec()], None, &[], vec![value("k", vec![p])], &Limits::default());
            assert_eq!(refused(r), (Code::Range, Some("k".into())));
        }
    }

    #[test]
    fn refuses_one_url_with_different_pins() {
        let urls = [url("a", Some(10), None), url("a", Some(11), None)];
        let r = seal(&urls, &[], None, &[], vec![value("k", vec![Piece::Url(0, 0, 1), Piece::Url(1, 0, 1)])], &Limits::default());
        assert_eq!(refused(r), (Code::Inputs, Some("k".into())));
    }

    #[test]
    fn refuses_a_zero_source_past_the_limit() {
        let r = seal(&[], &[], None, &[], vec![value("k", vec![Piece::Zeros(MAX_OPEN + 1)])], &Limits::default());
        assert_eq!(refused(r), (Code::Archive, None));
    }

    #[test]
    fn refuses_more_sources_than_the_reader_allows() {
        let urls = [url("a", Some(9), None), url("b", Some(9), None)];
        let limits = Limits { max_sources: 1, ..Limits::default() };
        let r = seal(&urls, &[], None, &[], vec![value("k", vec![Piece::Url(0, 0, 1), Piece::Url(1, 0, 1)])], &limits);
        assert_eq!(refused(r), (Code::Archive, None));
    }

    #[test]
    fn refuses_a_value_past_the_read_rule() {
        let urls = [url("a", Some(1 << 40), None)];
        // ranges 64 KiB apart merge; ranges further apart are one read each
        let near: Vec<Piece> = (0..3000).map(|k| Piece::Url(0, k * (1 << 16), 1)).collect();
        assert!(seal(&urls, &[], None, &[], vec![value("k", near)], &Limits::default()).is_ok());
        let far: Vec<Piece> = (0..1025).map(|k| Piece::Url(0, k * (1 << 17), 1)).collect();
        let r = seal(&urls, &[], None, &[], vec![value("k", far.clone())], &Limits::default());
        assert_eq!(refused(r), (Code::Reads, Some("k".into())));
        // a stricter reader lifts nothing
        let few: Vec<Piece> = far[..3].to_vec();
        let stricter = Limits { max_reads: 2, ..Limits::default() };
        let r = seal(&urls, &[], None, &[], vec![Value { key: "k".into(), pieces: few, lifted: true }], &stricter);
        assert_eq!(refused(r), (Code::Reads, Some("k".into())));
    }

    #[test]
    fn refuses_a_payload_past_the_limit() {
        let r = seal(&[], &[], None, &[], vec![value("k", vec![Piece::Lit(vec![0; MAX_PAYLOAD])])], &Limits::default());
        assert_eq!(refused(r), (Code::Payload, Some("k".into())));
    }

    #[test]
    fn fails_a_payload_past_the_limit_only_with_its_checksums() {
        let urls = [url("a", Some(1 << 40), None)];
        // 6000 ranges of 10 bytes each fit; with 6 more each they do not
        let pieces: Vec<Piece> = (0..6000).map(|k| Piece::Url(0, (1 << 30) + k * 3, 1)).collect();
        let v = Value { key: "k".into(), pieces, lifted: true };
        assert!(seal(&urls, &[], None, &[], vec![v.clone()], &Limits::default()).is_ok());
        let limits = Limits { checksums: true, ..Limits::default() };
        let e = seal(&urls, &[], None, &[], vec![v], &limits).unwrap_err();
        assert_eq!((e.code, e.key.as_deref()), (Code::Checksums, Some("k")));
        assert!(matches!(Refusal::from(e), Refusal::Failed(_)));
    }

    #[test]
    fn seals_a_built_in_output_renumbering_its_data() {
        let mut o = Out::new("https://data.test/f");
        // data strings in the order the projection used them, not the keys' order
        let late = o.shared(b"late");
        let early = o.shared(b"early");
        o.shared(b"unused");
        o.json("s/zarr.json", &serde_json::json!({"node_type": "array", "codecs": [{"name": "sharding_indexed"}]}));
        o.refs("b", vec![Part::Src(0, 4), late.clone()]);
        o.refs("a", vec![early, Part::Lit(b"x".to_vec())]);
        o.refs("s/c/0", (0..1500).map(|k| Part::Src(k * (1 << 17), 1)).collect());
        out(&mut o, Some(1 << 40), &HashMap::new(), false).unwrap();
        assert_eq!(o.data, vec![b"early".to_vec(), b"late".to_vec()]);
        assert_eq!(o.reference("a").unwrap()[0], Part::Data(1, 0, 5));
        assert_eq!(o.reference("b").unwrap()[1], Part::Data(2, 0, 4));
        // the same reference outside a sharded array passes the read rule
        o.refs("u/c/0", (0..1500).map(|k| Part::Src(k * (1 << 17), 1)).collect());
        assert_eq!(out(&mut o, Some(1 << 40), &HashMap::new(), false).unwrap_err().code, Code::Reads);
    }
}
