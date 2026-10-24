//! The messages of spec/proto/refkv.proto, hand-written as python/src/refkv/pb.py is: no protoc
//! and no generated code. Encoding follows proto3 (default values are not written; an optional
//! field is written whenever it is set, even to 0), and decoding the rules of spec/refkv.md §4.1
//! <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#41-encoding>: unknown fields are
//! skipped, a known field with the wrong wire type is an error, and every occurrence of a field
//! is checked, even one a later occurrence overrides.

use std::fmt;

const VARINT: u8 = 0;
const LEN: u8 = 2;

/// A message that does not decode (a payload error, or an archive error for the format entries).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DecodeError(pub String);

impl fmt::Display for DecodeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for DecodeError {}

type Res<T> = Result<T, DecodeError>;

fn bad<T>(m: impl Into<String>) -> Res<T> {
    Err(DecodeError(m.into()))
}

/// Appends `n` as a base-128 varint.
pub fn put_varint(out: &mut Vec<u8>, mut n: u64) {
    while n > 0x7f {
        out.push((n as u8 & 0x7f) | 0x80);
        n >>= 7;
    }
    out.push(n as u8);
}

fn put_tag(out: &mut Vec<u8>, field: u32, wire: u8) {
    put_varint(out, ((field as u64) << 3) | wire as u64);
}

pub(crate) fn put_bytes(out: &mut Vec<u8>, field: u32, data: &[u8]) {
    put_tag(out, field, LEN);
    put_varint(out, data.len() as u64);
    out.extend_from_slice(data);
}

/// A proto3 scalar: written only when it is not 0.
pub(crate) fn put_uint(out: &mut Vec<u8>, field: u32, n: u64) {
    if n != 0 {
        put_tag(out, field, VARINT);
        put_varint(out, n);
    }
}

/// An optional (explicit presence) scalar: written whenever it is set, even to 0.
pub(crate) fn put_optional(out: &mut Vec<u8>, field: u32, n: u64) {
    put_tag(out, field, VARINT);
    put_varint(out, n);
}

/// Reads a varint at `pos`, and moves `pos` past it: at most 10 bytes, and at most 2^64 - 1.
pub fn get_varint(buf: &[u8], pos: &mut usize) -> Res<u64> {
    let mut result: u128 = 0;
    for i in 0..10 {
        let Some(&b) = buf.get(*pos) else {
            return bad("truncated varint");
        };
        *pos += 1;
        result |= ((b & 0x7f) as u128) << (7 * i);
        if b & 0x80 == 0 {
            if result > u64::MAX as u128 {
                return bad("varint exceeds 2^64 - 1");
            }
            return Ok(result as u64);
        }
    }
    bad("varint longer than 10 bytes")
}

/// A field's value: a varint, or the bytes of a length-delimited field.
#[derive(Clone, Copy)]
enum Value<'a> {
    Varint(u64),
    Len(&'a [u8]),
    Fixed,
}

/// The fields of a message, in order: (field number, value).
fn fields(buf: &[u8]) -> Res<Vec<(u32, Value<'_>)>> {
    let mut out = Vec::new();
    let mut pos = 0;
    while pos < buf.len() {
        let key = get_varint(buf, &mut pos)?;
        let (field, wire) = (key >> 3, (key & 7) as u8);
        let value = match wire {
            VARINT => Value::Varint(get_varint(buf, &mut pos)?),
            LEN => {
                let n = get_varint(buf, &mut pos)?;
                if n > (buf.len() - pos) as u64 {
                    return bad("length-delimited field runs past the end of the message");
                }
                let v = &buf[pos..pos + n as usize];
                pos += n as usize;
                Value::Len(v)
            }
            1 | 5 => {
                // I64 / I32: never used by the schema, skipped
                let n = if wire == 1 { 8 } else { 4 };
                if pos + n > buf.len() {
                    return bad("truncated fixed-width field");
                }
                pos += n;
                Value::Fixed
            }
            _ => return bad(format!("unsupported wire type {wire}")),
        };
        if field == 0 || field > (1 << 29) - 1 {
            return bad(format!("invalid field number {field}"));
        }
        out.push((field as u32, value));
    }
    Ok(out)
}

/// The fields of `buf` that `schema` names ((field, wire type)); a known field with another wire
/// type is an error.
fn known<'a>(buf: &'a [u8], schema: &[(u32, u8)]) -> Res<Vec<(u32, Value<'a>)>> {
    let mut out = Vec::new();
    for (f, v) in fields(buf)? {
        let Some(&(_, want)) = schema.iter().find(|(n, _)| *n == f) else {
            continue;
        };
        let wire = match v {
            Value::Varint(_) => VARINT,
            Value::Len(_) => LEN,
            Value::Fixed => 1,
        };
        if wire != want {
            return bad(format!("field {f} has wire type {wire}, expected {want}"));
        }
        out.push((f, v));
    }
    Ok(out)
}

fn varint(v: Value) -> u64 {
    match v {
        Value::Varint(n) => n,
        _ => unreachable!("checked by known()"),
    }
}

fn len(v: Value<'_>) -> &[u8] {
    match v {
        Value::Len(b) => b,
        _ => unreachable!("checked by known()"),
    }
}

fn u32_of(n: u64) -> Res<u32> {
    u32::try_from(n).or_else(|_| bad("uint32 field exceeds 2^32 - 1"))
}

fn utf8(b: &[u8]) -> Res<String> {
    match std::str::from_utf8(b) {
        Ok(s) => Ok(s.to_string()),
        Err(e) => bad(format!("invalid UTF-8 in string field: {e}")),
    }
}

/// Bytes `[offset, offset + length)` of source `source`, or the literal bytes `data`
/// (spec/refkv.md §4.2 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#42-range>), with
/// the CRC-32C of a source range's bytes when it carries one.
#[derive(Clone, Debug, Default, PartialEq, Eq, Hash)]
pub struct Range {
    pub source: u32,
    pub offset: u64,
    pub length: u64,
    pub data: Option<Vec<u8>>,
    pub crc32c: Option<u32>,
}

impl Range {
    /// A range of a source.
    pub fn of(source: u32, offset: u64, length: u64) -> Range {
        Range { source, offset, length, ..Default::default() }
    }

    /// Literal bytes.
    pub fn literal(data: Vec<u8>) -> Range {
        Range { data: Some(data), ..Default::default() }
    }

    pub fn encode(&self) -> Vec<u8> {
        let mut out = Vec::new();
        self.encode_into(&mut out);
        out
    }

    fn encode_into(&self, out: &mut Vec<u8>) {
        put_uint(out, 1, self.source as u64);
        put_uint(out, 3, self.offset);
        put_uint(out, 4, self.length);
        if let Some(d) = &self.data {
            put_bytes(out, 5, d);
        }
        if let Some(c) = self.crc32c {
            put_optional(out, 6, c as u64);
        }
    }

    pub fn decode(buf: &[u8]) -> Result<Range, DecodeError> {
        let mut r = Range::default();
        for (f, v) in known(buf, &[(1, VARINT), (3, VARINT), (4, VARINT), (5, LEN), (6, VARINT)])? {
            match f {
                1 => r.source = u32_of(varint(v))?,
                3 => r.offset = varint(v),
                4 => r.length = varint(v),
                5 => r.data = Some(len(v).to_vec()),
                _ => r.crc32c = Some(u32_of(varint(v))?),
            }
        }
        if r.data.is_some() && (r.source != 0 || r.offset != 0 || r.length != 0) {
            return bad("literal range with non-zero source/offset/length");
        }
        if r.data.is_some() && r.crc32c.is_some() {
            return bad("literal range with a crc32c");
        }
        if r.offset.checked_add(r.length).is_none() {
            return bad("offset + length exceeds 2^64 - 1");
        }
        Ok(r)
    }

    /// The number of bytes the range stands for.
    pub fn size(&self) -> u64 {
        match &self.data {
            Some(d) => d.len() as u64,
            None => self.length,
        }
    }
}

/// A value made of several ranges (spec/refkv.md §4.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#43-concat>).
pub fn encode_concat(parts: &[Range]) -> Vec<u8> {
    let mut out = Vec::new();
    let mut sub = Vec::new();
    for r in parts {
        sub.clear();
        r.encode_into(&mut sub);
        put_bytes(&mut out, 1, &sub);
    }
    out
}

pub fn decode_concat(buf: &[u8]) -> Result<Vec<Range>, DecodeError> {
    let parts = known(buf, &[(1, LEN)])?.into_iter().map(|(_, v)| Range::decode(len(v))).collect::<Res<Vec<_>>>()?;
    let mut total: u64 = 0;
    for p in &parts {
        total = total.checked_add(p.size()).map_or_else(|| bad("concat size exceeds 2^64 - 1"), Ok)?;
    }
    Ok(parts)
}

/// The kind of a source: an external object (a URL, possibly relative to the archive), another
/// entry of the archive, or literal bytes.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum SourceKind {
    Url(String),
    Key(String),
    Data(Vec<u8>),
}

/// One entry of the source table: its kind, and the pins of a url source (spec/refkv.md §5.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#51-pins>).
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Source {
    pub kind: SourceKind,
    /// the object's size
    pub size: Option<u64>,
    /// its strong entity tag, with the quotes
    pub etag: Option<String>,
    /// seconds since the Unix epoch
    pub modified_not_after: Option<i64>,
}

/// Is `s` a strong entity tag, `DQUOTE *etagc DQUOTE` (RFC 9110)?
pub fn is_strong_etag(s: &str) -> bool {
    let b = s.as_bytes();
    b.len() >= 2 && b[0] == b'"' && b[b.len() - 1] == b'"' && b[1..b.len() - 1].iter().all(|&c| c == 0x21 || (0x23..=0x7e).contains(&c))
}

impl Source {
    pub fn url(url: impl Into<String>) -> Source {
        Source::of(SourceKind::Url(url.into()))
    }

    pub fn key(key: impl Into<String>) -> Source {
        Source::of(SourceKind::Key(key.into()))
    }

    pub fn data(data: impl Into<Vec<u8>>) -> Source {
        Source::of(SourceKind::Data(data.into()))
    }

    fn of(kind: SourceKind) -> Source {
        Source { kind, size: None, etag: None, modified_not_after: None }
    }

    /// The URL of a url source.
    pub fn url_str(&self) -> Option<&str> {
        match &self.kind {
            SourceKind::Url(u) => Some(u),
            _ => None,
        }
    }

    pub fn pinned(&self) -> bool {
        self.size.is_some() || self.etag.is_some() || self.modified_not_after.is_some()
    }

    /// The source's own rules: pins only on a url source, and an `etag` pin a strong entity tag.
    pub fn check(&self) -> Result<(), String> {
        if self.pinned() && self.url_str().is_none() {
            return Err("pins are only allowed on url sources".into());
        }
        if let Some(e) = &self.etag
            && !is_strong_etag(e) {
                return Err(format!("etag pin is not a strong entity tag in quotes: {e:?}"));
            }
        Ok(())
    }

    pub fn encode(&self) -> Vec<u8> {
        let mut out = Vec::new();
        match &self.kind {
            SourceKind::Url(u) => put_bytes(&mut out, 1, u.as_bytes()),
            SourceKind::Key(k) => put_bytes(&mut out, 2, k.as_bytes()),
            SourceKind::Data(d) => put_bytes(&mut out, 3, d),
        }
        if let Some(n) = self.size {
            put_optional(&mut out, 4, n);
        }
        if let Some(e) = &self.etag {
            put_bytes(&mut out, 5, e.as_bytes());
        }
        if let Some(t) = self.modified_not_after {
            put_optional(&mut out, 6, t as u64); // int64: two's complement
        }
        out
    }

    pub fn decode(buf: &[u8]) -> Result<Source, DecodeError> {
        let mut kind = None;
        let (mut size, mut etag, mut modified) = (None, None, None);
        let schema = [(1, LEN), (2, LEN), (3, LEN), (4, VARINT), (5, LEN), (6, VARINT)];
        for (f, v) in known(buf, &schema)? {
            match f {
                // a oneof: the last member on the wire wins, and every occurrence is checked
                1 => kind = Some(SourceKind::Url(utf8(len(v))?)),
                2 => kind = Some(SourceKind::Key(utf8(len(v))?)),
                3 => kind = Some(SourceKind::Data(len(v).to_vec())),
                4 => size = Some(varint(v)),
                5 => etag = Some(utf8(len(v))?),
                _ => modified = Some(varint(v) as i64),
            }
        }
        let Some(kind) = kind else {
            return bad("Source has no kind set");
        };
        let s = Source { kind, size, etag, modified_not_after: modified };
        s.check().map_err(DecodeError)?;
        Ok(s)
    }
}

/// The source table and the revision of spec/refkv.md it records (spec/refkv.md §5
/// <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#5-source-table>).
pub fn encode_source_table(sources: &[Source], revision: Option<u32>) -> Vec<u8> {
    let mut out = Vec::new();
    for s in sources {
        put_bytes(&mut out, 1, &s.encode());
    }
    if let Some(r) = revision {
        put_optional(&mut out, 2, r as u64);
    }
    out
}

/// The sources of a source table, and the revision it records, if any.
pub fn decode_source_table(buf: &[u8]) -> Result<(Vec<Source>, Option<u32>), DecodeError> {
    let (mut sources, mut revision) = (Vec::new(), None);
    for (f, v) in known(buf, &[(1, LEN), (2, VARINT)])? {
        if f == 1 {
            sources.push(Source::decode(len(v))?);
        } else {
            revision = Some(u32_of(varint(v))?);
        }
    }
    Ok((sources, revision))
}

/// A slice of the sorted central directory, relative to its start (spec/bindings/vzip.md §12
/// <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#12-page-index-optional>).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Page {
    pub first_key: String,
    pub offset: u64,
    pub length: u64,
}

/// The location of an entry a reader knows without loading any page.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Pinned {
    pub key: String,
    pub data_offset: u64,
    pub size: u64,
    pub csize: u64,
    pub method: u32,
}

/// The page index (`CdIndex`).
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct CdIndex {
    pub pages: Vec<Page>,
    pub pinned: Vec<Pinned>,
}

pub fn encode_cd_index(index: &CdIndex) -> Vec<u8> {
    let mut out = Vec::new();
    for p in &index.pages {
        let mut sub = Vec::new();
        put_bytes(&mut sub, 1, p.first_key.as_bytes());
        put_uint(&mut sub, 2, p.offset);
        put_uint(&mut sub, 3, p.length);
        put_bytes(&mut out, 1, &sub);
    }
    for e in &index.pinned {
        let mut sub = Vec::new();
        put_bytes(&mut sub, 1, e.key.as_bytes());
        put_uint(&mut sub, 2, e.data_offset);
        put_uint(&mut sub, 3, e.size);
        put_uint(&mut sub, 4, e.csize);
        put_uint(&mut sub, 5, e.method as u64);
        put_bytes(&mut out, 2, &sub);
    }
    out
}

pub fn decode_cd_index(buf: &[u8]) -> Result<CdIndex, DecodeError> {
    let mut index = CdIndex::default();
    for (f, v) in known(buf, &[(1, LEN), (2, LEN)])? {
        // each member's last occurrence wins; only the name kept is checked for UTF-8
        let mut vals: [Option<Value>; 6] = [None; 6];
        let schema: &[(u32, u8)] =
            if f == 2 { &[(1, LEN), (2, VARINT), (3, VARINT), (4, VARINT), (5, VARINT)] } else { &[(1, LEN), (2, VARINT), (3, VARINT)] };
        for (g, x) in known(len(v), schema)? {
            vals[g as usize] = Some(x);
        }
        let name = utf8(vals[1].map(len).unwrap_or(b""))?;
        let n = |i: usize| vals[i].map(varint).unwrap_or(0);
        if f == 1 {
            index.pages.push(Page { first_key: name, offset: n(2), length: n(3) });
        } else {
            index.pinned.push(Pinned { key: name, data_offset: n(2), size: n(3), csize: n(4), method: u32_of(n(5))? });
        }
    }
    Ok(index)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn hex(b: &[u8]) -> String {
        b.iter().map(|x| format!("{x:02x}")).collect()
    }

    #[test]
    fn encodings() {
        // the bytes python/src/refkv/pb.py writes (checked there against the protobuf runtime)
        let cases: Vec<(Vec<u8>, &str)> = vec![
            (Range::of(0, 0, 0).encode(), ""),
            (Range::of(1, 300, 5).encode(), "080118ac022005"),
            (Range { crc32c: Some(0), ..Range::of(0, 2, 3) }.encode(), "180220033000"),
            (Range { crc32c: Some(0xffff_ffff), ..Range::of(0, 0, 1) }.encode(), "200130ffffffff0f"),
            (Range::literal(b"ab".to_vec()).encode(), "2a026162"),
            (Range::literal(Vec::new()).encode(), "2a00"),
            (encode_concat(&[Range::of(0, 1, 2), Range::literal(vec![9])]), "0a04180120020a032a0109"),
            (Source::url("a").encode(), "0a0161"),
            (Source { size: Some(0), etag: Some("\"e\"".into()), modified_not_after: Some(-1), ..Source::url("a") }.encode(),
             "0a016120002a0322652230ffffffffffffffffff01"),
            (Source::key("k").encode(), "12016b"),
            (Source::data(vec![1, 2]).encode(), "1a020102"),
            (encode_source_table(&[Source::url("a"), Source::data(vec![])], Some(11)), "0a030a01610a021a00100b"),
            (encode_source_table(&[], None), ""),
            (encode_cd_index(&CdIndex {
                pages: vec![Page { first_key: "a".into(), offset: 0, length: 7 }],
                pinned: vec![Pinned { key: "z".into(), data_offset: 1, size: 2, csize: 2, method: 0 }],
            }), "0a050a0161180712090a017a100118022002"),
        ];
        for (got, want) in cases {
            assert_eq!(hex(&got), want);
        }
    }

    #[test]
    fn roundtrips() {
        let ranges = [
            Range::of(3, 1 << 40, 7),
            Range { crc32c: Some(12345), ..Range::of(0, 0, 9) },
            Range::literal(b"xyz".to_vec()),
            Range::default(),
        ];
        for r in &ranges {
            assert_eq!(&Range::decode(&r.encode()).unwrap(), r);
        }
        assert_eq!(decode_concat(&encode_concat(&ranges)).unwrap(), ranges);
        let sources = vec![
            Source { size: Some(5), etag: Some("\"x\"".into()), modified_not_after: Some(-7), ..Source::url("http://h/a") },
            Source::key("k"),
            Source::data(vec![0, 1]),
        ];
        assert_eq!(decode_source_table(&encode_source_table(&sources, Some(11))).unwrap(), (sources.clone(), Some(11)));
        assert_eq!(decode_source_table(&encode_source_table(&sources, None)).unwrap(), (sources, None));
        let index = CdIndex {
            pages: vec![Page { first_key: "a".into(), offset: 0, length: 9 }, Page { first_key: "b".into(), offset: 9, length: 1 }],
            pinned: vec![Pinned { key: "zarr.json".into(), data_offset: 100, size: 3, csize: 3, method: 8 }],
        };
        assert_eq!(decode_cd_index(&encode_cd_index(&index)).unwrap(), index);
        // unknown fields, and the fixed-width wire types, are skipped; the last occurrence wins
        assert_eq!(Range::decode(&[0x08, 0x01, 0x38, 0x05, 0x39, 0, 0, 0, 0, 0, 0, 0, 0, 0x08, 0x02]).unwrap(), Range::of(2, 0, 0));
        // the last kind on the wire wins
        assert_eq!(Source::decode(&[0x0a, 0x01, b'a', 0x12, 0x01, b'k']).unwrap(), Source::key("k"));
    }

    #[test]
    fn varints_round_trip() {
        for n in [0, 1, 127, 128, 300, u32::MAX as u64, u64::MAX] {
            let mut b = Vec::new();
            put_varint(&mut b, n);
            let mut pos = 0;
            assert_eq!((get_varint(&b, &mut pos).unwrap(), pos), (n, b.len()));
        }
    }

    #[test]
    fn rejects_truncated_varint() {
        assert!(Range::decode(&[0x08, 0x80]).is_err());
    }

    #[test]
    fn rejects_long_varint() {
        assert!(Range::decode(&[0x18, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x01]).is_err());
    }

    #[test]
    fn rejects_varint_over_u64() {
        assert!(Range::decode(&[0x18, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0x7f]).is_err());
    }

    #[test]
    fn rejects_group_wire_type() {
        assert!(Range::decode(&[0x0b]).is_err());
    }

    #[test]
    fn rejects_field_zero() {
        assert!(Range::decode(&[0x00, 0x01]).is_err());
    }

    #[test]
    fn rejects_length_past_end() {
        assert!(Range::decode(&[0x2a, 0x05, 0x01]).is_err());
    }

    #[test]
    fn rejects_truncated_fixed() {
        assert!(Range::decode(&[0x0d, 0x00]).is_err());
    }

    #[test]
    fn rejects_wrong_wire_type() {
        assert!(Range::decode(&[0x0a, 0x00]).is_err());
    }

    #[test]
    fn rejects_uint32_overflow() {
        assert!(Range::decode(&[0x08, 0x80, 0x80, 0x80, 0x80, 0x10]).is_err());
    }

    #[test]
    fn rejects_literal_with_source_fields() {
        assert!(Range::decode(&[0x08, 0x01, 0x2a, 0x00]).is_err());
    }

    #[test]
    fn rejects_literal_with_crc() {
        assert!(Range::decode(&[0x2a, 0x00, 0x30, 0x00]).is_err());
    }

    #[test]
    fn rejects_offset_plus_length_overflow() {
        let mut b = Vec::new();
        put_uint(&mut b, 3, u64::MAX);
        put_uint(&mut b, 4, 1);
        assert!(Range::decode(&b).is_err());
    }

    #[test]
    fn rejects_concat_size_overflow() {
        assert!(decode_concat(&encode_concat(&[Range::of(0, 0, u64::MAX), Range::of(0, 0, 1)])).is_err());
    }

    #[test]
    fn rejects_bad_utf8_even_overridden() {
        assert!(Source::decode(&[0x0a, 0x01, 0xff, 0x0a, 0x01, b'a']).is_err());
    }

    #[test]
    fn rejects_source_without_kind() {
        assert!(Source::decode(&[0x20, 0x01]).is_err());
    }

    #[test]
    fn rejects_pins_on_non_url_source() {
        assert!(Source::decode(&[0x12, 0x01, b'k', 0x20, 0x01]).is_err());
    }

    #[test]
    fn rejects_weak_etag() {
        assert!(Source::decode(&[0x0a, 0x01, b'a', 0x2a, 0x04, b'W', b'/', b'"', b'"']).is_err());
    }

    #[test]
    fn rejects_revision_over_u32() {
        assert!(decode_source_table(&[0x10, 0x80, 0x80, 0x80, 0x80, 0x10]).is_err());
    }

    #[test]
    fn rejects_pinned_method_over_u32() {
        let mut sub = Vec::new();
        put_uint(&mut sub, 5, 1 << 32);
        let mut b = Vec::new();
        put_bytes(&mut b, 2, &sub);
        assert!(decode_cd_index(&b).is_err());
    }

    #[test]
    fn strong_etags() {
        for (s, ok) in [("\"abc\"", true), ("\"\"", true), ("W/\"abc\"", false), ("\"a b\"", false), ("\"a\"b\"", false), ("\"", false)] {
            assert_eq!(is_strong_etag(s), ok, "{s}");
        }
    }
}
