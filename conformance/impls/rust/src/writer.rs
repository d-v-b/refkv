//! The HARNESS.md `write` command: the JSON description parser, and the archive written by the
//! archive writer of rust/refkv-vzip (spec/bindings/vzip.md §14
//! <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#14-writing-a-vzip-archive>), as the reference
//! CLI (python/src/refkv/vzip/cli.py) writes it: the sources in order, then each entry, a pinned
//! one late (just before the central directory, and in the page index's pinned list).

use crate::json::{self, Value};
use refkv_vzip::{Options, Range, Source, SourceKind, Writer, RESERVED_PREFIX};
use std::io::Write;

#[derive(Debug, Clone)]
pub enum EntryData {
    Bytes { data: Vec<u8>, compress: bool },
    Ranges(Vec<Range>),
}

#[derive(Debug, Clone)]
pub struct EntrySpec {
    pub key: String,
    pub data: EntryData,
    pub pinned: bool,
}

#[derive(Debug, Clone)]
pub struct ArchiveSpec {
    pub page_size: Option<u64>,
    pub mirror: bool,
    pub sources: Vec<Source>,
    pub entries: Vec<EntrySpec>,
}

/// Writes the archive a description gives; an invalid one is an error, after which the output
/// holds no archive.
pub fn write_archive<W: Write>(spec: &ArchiveSpec, w: W) -> Result<(), String> {
    let mut w = Writer::new(w, Options { mirror_refs: spec.mirror, page_size: spec.page_size });
    for s in &spec.sources {
        w.source(s.clone()).map_err(|e| e.to_string())?;
    }
    for e in &spec.entries {
        match &e.data {
            EntryData::Ranges(ranges) => {
                if e.pinned {
                    return Err(format!("{:?}: only bytes entries can be pinned", e.key));
                }
                w.add_ranges(&e.key, ranges.clone()).map_err(|x| x.to_string())?;
            }
            EntryData::Bytes { data, compress } => {
                if e.pinned && spec.page_size.is_none() {
                    return Err(format!("{:?}: pinned requires page_size", e.key));
                }
                match e.key.strip_prefix(RESERVED_PREFIX) {
                    Some(name) => w.add_hidden(name, data, *compress, e.pinned).map(|_| ()),
                    None => w.add_bytes(&e.key, data, *compress, e.pinned),
                }
                .map_err(|x| x.to_string())?;
            }
        }
    }
    w.close().map_err(|e| e.to_string())?;
    Ok(())
}

// ------------------------------------------------------------ JSON description

fn int_field(v: &Value, what: &str, min: i128, max: i128) -> Result<i128, String> {
    let n = v.as_int().ok_or_else(|| format!("{what} must be a JSON integer"))?;
    if n < min || n > max {
        return Err(format!("{what} out of range"));
    }
    Ok(n)
}

fn not_null<'a>(v: &'a Value, k: &str) -> Result<Option<&'a Value>, String> {
    match v.get(k) {
        None => Ok(None),
        Some(Value::Null) => Err(format!("{k} must not be null")),
        Some(x) => Ok(Some(x)),
    }
}

fn bool_field(v: &Value, k: &str, default: bool) -> Result<bool, String> {
    match not_null(v, k)? {
        None => Ok(default),
        Some(Value::Bool(b)) => Ok(*b),
        Some(_) => Err(format!("{k} must be a boolean")),
    }
}

fn str_field(v: &Value, k: &str) -> Result<Option<String>, String> {
    match not_null(v, k)? {
        None => Ok(None),
        Some(Value::String(s)) => Ok(Some(s.clone())),
        Some(_) => Err(format!("{k} must be a string")),
    }
}

fn hex_field(v: &Value, k: &str) -> Result<Option<Vec<u8>>, String> {
    match str_field(v, k)? {
        None => Ok(None),
        Some(s) => json::unhex(&s).map(Some).ok_or_else(|| format!("{k} must be lowercase even-length hex")),
    }
}

fn array_field<'a>(v: &'a Value, k: &str) -> Result<&'a [Value], String> {
    match not_null(v, k)? {
        None => Ok(&[]),
        Some(Value::Array(a)) => Ok(a),
        Some(_) => Err(format!("{k} must be an array")),
    }
}

const MAX_U64: i128 = u64::MAX as i128;

/// Parses a HARNESS.md write description.
pub fn parse_description(bytes: &[u8]) -> Result<ArchiveSpec, String> {
    let v = json::parse(bytes)?;
    if !matches!(v, Value::Object(_)) {
        return Err("description must be a JSON object".into());
    }
    let page_size = match v.get("page_size") {
        None | Some(Value::Null) => None,
        Some(x) => Some(int_field(x, "page_size", 1, MAX_U64)? as u64),
    };
    let mirror = bool_field(&v, "mirror", true)?;
    let mut sources = Vec::new();
    for (i, s) in array_field(&v, "sources")?.iter().enumerate() {
        if !matches!(s, Value::Object(_)) {
            return Err(format!("source {i} must be an object"));
        }
        let url = str_field(s, "url")?;
        let key = str_field(s, "key")?;
        let data = hex_field(s, "data")?;
        let kind = match (url, key, data) {
            (Some(u), None, None) => SourceKind::Url(u),
            (None, Some(k), None) => SourceKind::Key(k),
            (None, None, Some(d)) => SourceKind::Data(d),
            _ => return Err(format!("source {i} must have exactly one of url, key, data")),
        };
        let size = match not_null(s, "size")? {
            None => None,
            Some(x) => Some(int_field(x, "size", 0, MAX_U64)? as u64),
        };
        let etag = str_field(s, "etag")?;
        let modified_not_after = match not_null(s, "modified_not_after")? {
            None => None,
            Some(x) => Some(int_field(x, "modified_not_after", i64::MIN as i128, i64::MAX as i128)? as i64),
        };
        sources.push(Source { kind, size, etag, modified_not_after });
    }
    let mut entries = Vec::new();
    for (i, e) in array_field(&v, "entries")?.iter().enumerate() {
        if !matches!(e, Value::Object(_)) {
            return Err(format!("entry {i} must be an object"));
        }
        let key = str_field(e, "key")?.ok_or_else(|| format!("entry {i} has no key"))?;
        let compress = bool_field(e, "compress", false)?;
        let pinned = bool_field(e, "pinned", false)?;
        let bytes = hex_field(e, "bytes")?;
        let ranges = match not_null(e, "ranges")? {
            None => None,
            Some(Value::Array(a)) => Some(a),
            Some(_) => return Err(format!("entry {i}: ranges must be an array")),
        };
        let data = match (bytes, ranges) {
            (Some(b), None) => EntryData::Bytes { data: b, compress },
            (None, Some(rs)) => {
                if compress {
                    return Err(format!("entry {i}: compress is only allowed on bytes entries"));
                }
                let mut out = Vec::new();
                for (j, r) in rs.iter().enumerate() {
                    if !matches!(r, Value::Object(_)) {
                        return Err(format!("entry {i} range {j} must be an object"));
                    }
                    let lit = hex_field(r, "data")?;
                    let src = not_null(r, "source")?;
                    let off = not_null(r, "offset")?;
                    let len = not_null(r, "length")?;
                    match lit {
                        Some(d) => {
                            if src.is_some() || off.is_some() || len.is_some() {
                                return Err(format!("entry {i} range {j} mixes data with source fields"));
                            }
                            out.push(Range { data: Some(d), ..Default::default() });
                        }
                        None => {
                            let g = |x: Option<&Value>, n: &str, max: i128| -> Result<i128, String> {
                                x.map(|x| int_field(x, n, 0, max)).unwrap_or(Ok(0))
                            };
                            out.push(Range {
                                source: g(src, "source", u32::MAX as i128)? as u32,
                                offset: g(off, "offset", MAX_U64)? as u64,
                                length: g(len, "length", MAX_U64)? as u64,
                                ..Default::default()
                            });
                        }
                    }
                }
                EntryData::Ranges(out)
            }
            _ => return Err(format!("entry {i} must have exactly one of bytes, ranges")),
        };
        entries.push(EntrySpec { key, data, pinned });
    }
    Ok(ArchiveSpec { page_size, mirror, sources, entries })
}
