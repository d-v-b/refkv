//! The sans-IO netCDF3 parser (spec/zarrify/netcdf3.md
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md>): the classic (CDF-1),
//! 64-bit offset (CDF-2) and 64-bit data (CDF-5) formats, their variables, dimensions and
//! attributes, as a Zarr hierarchy.
//!
//! The header is read from byte 0 in rounds (§7.2
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#72-reading>): the first
//! 64 KiB, then a prefix at least twice as long while the header runs past what was read. Each
//! pass parses the header from its start, so a rejection never depends on the reads. The data is
//! never read.
//!
//! `finish` emits the IR: the header, the data of each fixed-size variable, and the records, and
//! describes each variable as the file stores it (spec/stored-array.md §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>; `stored.rs`).

use crate::fed::{Fed, Overlap};
use crate::hdf5::types::{float_json, int_of};
use crate::ir::{Ir, NO_INDEX};
use crate::nd2::Step;
use crate::out::int_json;
use crate::rules::{self, Rules};
use crate::sans::Asked;
use crate::text::{text_json, utf8_or_latin1};
use serde_json::{json, Map, Value as J};
use std::collections::{BTreeMap, HashSet};

mod stored;

const PROFILE: &str = "netcdf3";

// The header's tags (spec/zarrify/netcdf3.md §7.2
// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#72-reading>)
const NC_DIMENSION: u32 = 10;
const NC_VARIABLE: u32 = 11;
const NC_ATTRIBUTE: u32 = 12;
const NC_CHAR: u32 = 2;

/// The names a variable may not take, and the attribute names the hierarchy writes itself.
const RESERVED: &[&str] = &["refkv_source"];
const RESERVED_ATTRIBUTES: &[&str] = &["zarr_conventions", "refkv_zarrified"];

/// Whether a file's first bytes are a netCDF3 file's: `CDF` and the version byte 1, 2 or 5.
pub fn detect(head: &[u8]) -> bool {
    head.len() >= 4 && &head[..3] == b"CDF" && matches!(head[3], 1 | 2 | 5)
}

/// A type of the format (spec/zarrify/netcdf3.md §3.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#31-data-types>).
#[derive(Clone, Copy, Debug)]
struct Ty {
    name: &'static str,
    /// the Zarr data type, but for `NC_CHAR`'s
    zarr: &'static str,
    bytes: u64,
    signed: bool,
    float: bool,
    /// of CDF-5 alone
    cdf5: bool,
    /// netCDF-C's `NC_FILL_*`, its bits as the type's big-endian bytes
    fill: u64,
}

fn ty(code: u32) -> Option<Ty> {
    let t = |name, zarr, bytes, signed, float, cdf5, fill| Ty { name, zarr, bytes, signed, float, cdf5, fill };
    Some(match code {
        1 => t("NC_BYTE", "int8", 1, true, false, false, 0x81),
        2 => t("NC_CHAR", "", 1, false, false, false, 0),
        3 => t("NC_SHORT", "int16", 2, true, false, false, 0x8001),
        4 => t("NC_INT", "int32", 4, true, false, false, 0x8000_0001),
        // 15 × 2^119, 9.9692099683868690e36, exact in both
        5 => t("NC_FLOAT", "float32", 4, true, true, false, 0x7CF0_0000),
        6 => t("NC_DOUBLE", "float64", 8, true, true, false, 0x479E_0000_0000_0000),
        7 => t("NC_UBYTE", "uint8", 1, false, false, true, 0xFF),
        8 => t("NC_USHORT", "uint16", 2, false, false, true, 0xFFFF),
        9 => t("NC_UINT", "uint32", 4, false, false, true, 0xFFFF_FFFF),
        10 => t("NC_INT64", "int64", 8, true, false, true, 0x8000_0000_0000_0002),
        11 => t("NC_UINT64", "uint64", 8, false, false, true, 0xFFFF_FFFF_FFFF_FFFE),
        _ => return None,
    })
}

impl Ty {
    /// The Zarr data type.
    fn data_type(&self) -> J {
        if self.zarr.is_empty() {
            json!({"name": "null_terminated_bytes", "configuration": {"length_bytes": 1}})
        } else {
            json!(self.zarr)
        }
    }

    /// One value of the type in `b` (its bytes, big-endian), as a fill value or attribute value.
    fn value(&self, b: &[u8]) -> J {
        if self.float {
            float_json(b, true)
        } else {
            int_json(int_of(b, true, self.signed))
        }
    }

    /// The default fill value, as Zarr writes it.
    fn default_fill(&self) -> J {
        if self.zarr.is_empty() {
            return json!("");
        }
        let all = self.fill.to_be_bytes();
        self.value(&all[8 - self.bytes as usize..])
    }
}

/// A failed pass: the bytes from 0 to the offset are needed, or the file breaks a rule.
#[derive(Debug)]
enum Fail {
    Need(u64),
    Bad(String),
}

type R<T> = Result<T, Fail>;

fn bad<T>(m: impl Into<String>) -> R<T> {
    Err(Fail::Bad(m.into()))
}

/// A big-endian reader of the header, over the file's first bytes `b`: a read past the file, or
/// past the header's budget, rejects; one past `b` needs more of the file.
struct Rd<'a> {
    b: &'a [u8],
    p: u64,
    size: u64,
    budget: u64,
    /// CDF-5: counts and lengths of 8 bytes
    wide: bool,
    /// CDF-2 and CDF-5: `begin` of 8 bytes
    long_offsets: bool,
}

impl<'a> Rd<'a> {
    fn bytes(&mut self, n: u64) -> R<&'a [u8]> {
        let end = self.p.checked_add(n).filter(|&e| e <= self.size).ok_or_else(|| Fail::Bad("the header runs past the end of the file".into()))?;
        if end > self.budget {
            return bad(format!("budget: the header is more than {} bytes", self.budget));
        }
        if end > self.b.len() as u64 {
            return Err(Fail::Need(end));
        }
        let s = &self.b[self.p as usize..end as usize];
        self.p = end;
        Ok(s)
    }
    fn u32(&mut self) -> R<u32> {
        let b = self.bytes(4)?;
        Ok(u32::from_be_bytes([b[0], b[1], b[2], b[3]]))
    }
    fn u64(&mut self) -> R<u64> {
        let b = self.bytes(8)?;
        let mut a = [0u8; 8];
        a.copy_from_slice(b);
        Ok(u64::from_be_bytes(a))
    }
    /// `NON_NEG`: `nelems`, `dim_length`, `dimid`, `vsize` and `numrecs`.
    fn size(&mut self) -> R<u64> {
        if self.wide { self.u64() } else { self.u32().map(u64::from) }
    }
    /// `OFFSET`: a variable's `begin`.
    fn offset(&mut self) -> R<u64> {
        if self.long_offsets { self.u64() } else { self.u32().map(u64::from) }
    }
    /// The padding after `n` bytes of a name or of values.
    fn pad(&mut self, n: u64) -> R<()> {
        self.bytes((4 - n % 4) % 4).map(|_| ())
    }
    /// A name: its bytes to the first NUL, as netCDF-C reads it (spec/zarrify/netcdf3.md §7.2
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#72-reading>).
    fn name(&mut self) -> R<Vec<u8>> {
        let n = self.size()?;
        let b = self.bytes(n)?;
        let name = b.split(|&c| c == 0).next().unwrap_or(&[]).to_vec();
        self.pad(n)?;
        Ok(name)
    }
    /// A list's tag and count: `Some(count)` of the list `tag`, `None` for an absent one.
    fn list(&mut self, tag: u32, what: &str) -> R<Option<u64>> {
        let (t, n) = (self.u32()?, self.size()?);
        match (t, n) {
            (t, n) if t == tag => Ok(Some(n)),
            (0, 0) => Ok(None),
            (t, n) => bad(format!("the {what} has the tag {t} and {n} elements")),
        }
    }
}

/// An attribute: its name, its type, its count, its values and where they are in the header.
#[derive(Clone, Debug)]
struct Attr {
    name: Vec<u8>,
    code: u32,
    count: u64,
    raw: Vec<u8>,
    /// the offset of its values in the header, and of its entry (its name's length)
    at: u64,
    entry: (u64, u64),
}

#[derive(Debug)]
struct Dim {
    name: Vec<u8>,
    len: u64,
}

#[derive(Debug)]
struct Var {
    name: Vec<u8>,
    dims: Vec<usize>,
    attrs: Vec<Attr>,
    code: u32,
    /// the offset of its `nc_type` in the header
    type_at: u64,
    vsize: u64,
    begin: u64,
}

/// The header as parsed.
#[derive(Debug)]
struct Header {
    version: u8,
    /// None: the all-ones value a streaming writer leaves
    numrecs: Option<u64>,
    dims: Vec<Dim>,
    attrs: Vec<Attr>,
    vars: Vec<Var>,
    end: u64,
}

/// The attribute list of `what`, at the reader.
fn att_list(r: &mut Rd, version: u8, what: &str) -> R<Vec<Attr>> {
    let mut out = Vec::new();
    let Some(n) = r.list(NC_ATTRIBUTE, what)? else { return Ok(out) };
    for _ in 0..n {
        let start = r.p;
        let name = r.name()?;
        let code = r.u32()?;
        let shown = utf8_or_latin1(&name);
        let t = ty(code).ok_or_else(|| Fail::Bad(format!("the attribute {shown} has the unknown type {code}")))?;
        if t.cdf5 && version != 5 {
            return bad(format!("the attribute {shown} has the type {code}, of CDF-5 only"));
        }
        let count = r.size()?;
        let nbytes = count.checked_mul(t.bytes).ok_or_else(|| Fail::Bad("the header runs past the end of the file".into()))?;
        let at = r.p;
        let raw = r.bytes(nbytes)?.to_vec();
        r.pad(nbytes)?;
        out.push(Attr { name, code, count, raw, at, entry: (start, r.p - start) });
    }
    Ok(out)
}

/// The header of a file of `size` bytes, from its first bytes `b` (spec/zarrify/netcdf3.md §7.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#72-reading>), and the
/// header's checks of §7.4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#74-checks>.
fn header(b: &[u8], size: u64, budget: u64) -> R<Header> {
    if size < 4 {
        return bad("not a netCDF3 file");
    }
    let head = b.get(..4).ok_or(Fail::Need(4))?;
    if !detect(head) {
        return bad("not a netCDF3 file");
    }
    let version = head[3];
    let mut r = Rd { b, p: 4, size, budget, wide: version == 5, long_offsets: version != 1 };
    let numrecs = r.size()?;
    let streaming = if r.wide { u64::MAX } else { u32::MAX as u64 };
    let numrecs = (numrecs != streaming).then_some(numrecs);
    let mut dims = Vec::new();
    if let Some(n) = r.list(NC_DIMENSION, "dimension list")? {
        for _ in 0..n {
            let name = r.name()?;
            dims.push(Dim { name, len: r.size()? });
        }
    }
    let attrs = att_list(&mut r, version, "global attribute list")?;
    let mut vars = Vec::new();
    if let Some(n) = r.list(NC_VARIABLE, "variable list")? {
        for k in 0..n {
            let name = r.name()?;
            let rank = r.size()?;
            let mut ids = Vec::new();
            for a in 0..rank {
                let d = r.size()?;
                let Some(dim) = usize::try_from(d).ok().and_then(|i| dims.get(i)) else {
                    return bad(format!("variable {k} names the dimension {d}, of {}", dims.len()));
                };
                if a > 0 && dim.len == 0 {
                    return bad(format!("variable {k} has the unlimited dimension at axis {a}"));
                }
                ids.push(d as usize);
            }
            let attrs = att_list(&mut r, version, &format!("attribute list of variable {k}"))?;
            let type_at = r.p;
            let code = r.u32()?;
            let t = ty(code).ok_or_else(|| Fail::Bad(format!("variable {k} has the unknown type {code}")))?;
            if t.cdf5 && version != 5 {
                return bad(format!("variable {k} has the type {code}, of CDF-5 only"));
            }
            let vsize = r.size()?;
            let begin = r.offset()?;
            vars.push(Var { name, dims: ids, attrs, code, type_at, vsize, begin });
        }
    }
    Ok(Header { version, numrecs, dims, attrs, vars, end: r.p })
}

/// A variable's layout, as the profile computes it (spec/zarrify/netcdf3.md §7.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#72-reading>).
#[derive(Clone, Debug)]
struct Layout {
    record: bool,
    /// a fixed-size variable's bytes, or a record variable's slice
    bytes: u64,
    /// the lengths of its dimensions, a record variable's first the record count
    shape: Vec<u64>,
}

/// What the file holds where, by the checks of the data (spec/zarrify/netcdf3.md §7.4
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#74-checks>).
#[derive(Debug)]
struct Placed {
    layouts: Vec<Layout>,
    records: u64,
    recsize: u64,
    begin_rec: u64,
    /// the records element's extent, when the file has a record variable and a record
    span: Option<(u64, u64)>,
}

fn place(h: &Header, size: u64) -> R<Placed> {
    let big = |k: usize| Fail::Bad(format!("variable {k} holds more than 2^64 bytes"));
    let mut layouts = Vec::with_capacity(h.vars.len());
    for (k, v) in h.vars.iter().enumerate() {
        let t = ty(v.code).ok_or_else(|| Fail::Bad("internal: a variable of no type".into()))?;
        let lens: Vec<u64> = v.dims.iter().map(|&d| h.dims.get(d).map_or(0, |d| d.len)).collect();
        let record = lens.first() == Some(&0);
        let bytes = lens.iter().skip(record as usize).try_fold(t.bytes, |a, &n| a.checked_mul(n)).ok_or_else(|| big(k))?;
        bytes.checked_next_multiple_of(4).ok_or_else(|| big(k))?;
        layouts.push(Layout { record, bytes, shape: lens });
    }
    let recvars: Vec<usize> = (0..h.vars.len()).filter(|&k| layouts[k].record).collect();
    // the record size: the padded slices' sum, or one record variable's slice unpadded
    let recsize = match recvars.as_slice() {
        [k] => layouts[*k].bytes,
        ks => ks.iter().try_fold(0u64, |a, &k| a.checked_add(layouts[k].bytes.next_multiple_of(4))).ok_or_else(|| big(*ks.last().unwrap_or(&0)))?,
    };
    let begin_rec = recvars.iter().map(|&k| h.vars[k].begin).min().unwrap_or(0);
    let records = match h.numrecs {
        Some(n) => n,
        None if recsize == 0 => 0,
        None => size.saturating_sub(begin_rec) / recsize,
    };
    for (k, v) in h.vars.iter().enumerate() {
        let l = &layouts[k];
        if !l.record && v.begin.checked_add(l.bytes).is_none_or(|e| e > size) {
            return bad(format!("variable {k} ({} bytes at byte {}) runs past the end of the file", l.bytes, v.begin));
        }
        if !l.record && v.begin < h.end {
            return bad(format!("variable {k} begins at byte {}, inside the header of {} bytes", v.begin, h.end));
        }
    }
    for &k in &recvars {
        if h.vars[k].begin < h.end {
            return bad(format!("variable {k} begins at byte {}, inside the header of {} bytes", h.vars[k].begin, h.end));
        }
    }
    // each record's slices, by their offsets in it
    let mut slices: Vec<(u64, u64, usize)> = recvars.iter().map(|&k| (h.vars[k].begin - begin_rec, layouts[k].bytes, k)).collect();
    slices.sort_unstable();
    for (i, &(off, n, k)) in slices.iter().enumerate() {
        let next = slices.get(i + 1).map_or(recsize, |s| s.0);
        if off.checked_add(n).is_none_or(|e| e > next) {
            return bad(format!("the slices of record variable {k} overlap the next"));
        }
    }
    let mut span = None;
    if records > 0 && !recvars.is_empty() {
        let last = records - 1;
        let mut end = 0u64;
        for &k in &recvars {
            let e = last.checked_mul(recsize).and_then(|o| o.checked_add(h.vars[k].begin)).and_then(|o| o.checked_add(layouts[k].bytes));
            match e {
                Some(e) if e <= size => end = end.max(e),
                _ => return bad(format!("record {last} of variable {k} runs past the end of the file")),
            }
        }
        span = Some((begin_rec, end - begin_rec));
    }
    // no two fixed-size variables, nor one and the records, share a byte
    let mut runs: Vec<(u64, u64, Option<usize>)> =
        h.vars.iter().enumerate().filter(|(k, _)| !layouts[*k].record).map(|(k, v)| (v.begin, v.begin + layouts[k].bytes, Some(k))).collect();
    if let Some((o, n)) = span {
        runs.push((o, o + n, None));
    }
    runs.sort_unstable_by_key(|r| (r.0, r.2.map_or(0, |k| k + 1)));
    let mut far: Option<(u64, u64, Option<usize>)> = None;
    for run in runs {
        if let Some(f) = far.filter(|f| run.0 < f.1 && run.1 > run.0) {
            return match (f.2, run.2) {
                (Some(a), Some(b)) => bad(format!("variables {} and {} overlap", a.min(b), a.max(b))),
                (Some(k), None) | (None, Some(k)) => bad(format!("variable {k} and the records overlap")),
                (None, None) => bad("internal: the records overlap themselves"),
            };
        }
        if far.is_none_or(|f| run.1 > f.1) {
            far = Some(run);
        }
    }
    for l in layouts.iter_mut().filter(|l| l.record) {
        l.shape[0] = records;
    }
    Ok(Placed { layouts, records, recsize, begin_rec, span })
}

/// Why a name is not a node's (spec/zarrify/netcdf3.md §2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#2-the-hierarchy>), or the
/// name.
fn node_name(b: &[u8]) -> Result<String, &'static str> {
    let s = std::str::from_utf8(b).map_err(|_| "name")?;
    if s.is_empty() || s == "." || s == ".." || s.starts_with("__") || s.contains('/') {
        return Err("name");
    }
    if RESERVED.contains(&s) {
        return Err("reserved");
    }
    Ok(s.to_string())
}

/// An attribute's value as JSON (spec/zarrify/netcdf3.md §5
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#5-attributes>).
fn attribute_json(a: &Attr) -> J {
    let Some(t) = ty(a.code) else { return J::Null };
    if a.code == NC_CHAR {
        let end = a.raw.iter().rposition(|&c| c != 0).map_or(0, |i| i + 1);
        return text_json(&a.raw[..end]);
    }
    let values: Vec<J> = a.raw.chunks_exact(t.bytes as usize).map(|b| t.value(b)).collect();
    if values.len() == 1 { values.into_iter().next().unwrap_or(J::Null) } else { J::Array(values) }
}

/// A variable's `_FillValue` in the form xarray's Zarr backend decodes for the variable's data type
/// (spec/zarrify/netcdf3.md §5
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#5-attributes>): of a float
/// variable, the base64 of the value as a little-endian binary64; of an integer variable, the
/// integer; None for any other, which xarray's backend refuses.
fn fill_attribute(a: &Attr, var: &Ty) -> Option<J> {
    let t = ty(a.code).filter(|_| a.count == 1 && a.code != NC_CHAR && !var.zarr.is_empty())?;
    let x = if t.float {
        if t.bytes == 4 { f32::from_bits(int_of(&a.raw, true, false) as u32) as f64 } else { f64::from_bits(int_of(&a.raw, true, false) as u64) }
    } else if var.float {
        int_of(&a.raw, true, t.signed) as f64
    } else {
        return Some(int_json(int_of(&a.raw, true, t.signed)));
    };
    if var.float {
        return Some(json!(crate::mirror::base64(&x.to_le_bytes())));
    }
    // a float `_FillValue` of an integer variable: the integer it is, where it is one
    (x.is_finite() && x.fract() == 0.0 && x.abs() < 2f64.powi(63)).then(|| int_json(x as i128))
}

/// A node's attributes as presented, each one's type, and the reasons for those not presented.
/// `var`: of a variable, its type, for `_FillValue`'s form.
fn presented_attributes(attrs: &[Attr], var: Option<&Ty>) -> (Map<String, J>, Map<String, J>, Map<String, J>) {
    let (mut presented, mut types, mut not) = (Map::new(), Map::new(), Map::new());
    for a in attrs {
        let name = utf8_or_latin1(&a.name);
        if types.contains_key(&name) {
            continue;
        }
        types.insert(name.clone(), json!({"type": ty(a.code).map_or("", |t| t.name), "count": a.count}));
        if RESERVED_ATTRIBUTES.contains(&name.as_str()) {
            not.insert(name, json!("reserved"));
            continue;
        }
        let v = match var {
            Some(t) if name == "_FillValue" => match fill_attribute(a, t) {
                Some(v) => v,
                None => {
                    not.insert(name, json!("fill_value"));
                    continue;
                }
            },
            _ => attribute_json(a),
        };
        presented.insert(name, v);
    }
    (presented, types, not)
}

/// The fill value of a variable of type `t` with attributes `attrs`, and how the stored-array
/// description gives it (spec/zarrify/netcdf3.md §3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#3-variables>).
fn fill_of(t: &Ty, code: u32, attrs: &[Attr]) -> (J, stored::FillAt) {
    let Some(a) = attrs.iter().find(|a| a.name == b"_FillValue") else { return (t.default_fill(), stored::FillAt::Default) };
    if a.code != code || a.count != 1 {
        return (t.default_fill(), stored::FillAt::Other(a.entry));
    }
    let v = if code == NC_CHAR {
        if a.raw.first() == Some(&0) { json!("") } else { json!(crate::mirror::base64(&a.raw)) }
    } else {
        t.value(&a.raw)
    };
    (v, stored::FillAt::Given((a.at, t.bytes)))
}

/// What a finished parse holds: the header, where its variables are, and the facts.
struct Found {
    header: Header,
    placed: Placed,
    fills: Vec<stored::FillAt>,
}

pub struct Netcdf3 {
    size: u64,
    fed: Fed,
    rules: &'static Rules,
    asked: Asked,
    /// the bytes from 0 asked for so far
    read: u64,
    found: Option<Found>,
    facts: J,
}

impl Netcdf3 {
    pub fn new(size: u64) -> Self {
        Netcdf3 { size, fed: Fed::new(Overlap::MergeNew), rules: rules::of(PROFILE), asked: Asked::default(), read: 0, found: None, facts: J::Null }
    }

    pub fn feed(&mut self, offset: u64, data: Vec<u8>) {
        self.fed.feed(offset, data);
    }

    pub fn step(&mut self) -> Result<Step, String> {
        if self.found.is_some() {
            return Ok(Step::Done);
        }
        let budget = self.rules.limit("header_bytes") as u64;
        if self.read == 0 && self.size > 0 {
            self.read = self.size.min(self.rules.limit("first_read") as u64);
            return Ok(self.asked.ask(vec![(0, self.read)]));
        }
        let b = self.fed.get(0, self.read).ok_or("internal: the header's bytes were not fed")?.to_vec();
        match header(&b, self.size, budget).and_then(|h| place(&h, self.size).map(|p| (h, p))) {
            Ok((h, p)) => {
                self.decide(h, p);
                Ok(Step::Done)
            }
            Err(Fail::Bad(m)) => Err(m),
            Err(Fail::Need(end)) => {
                let next = end.max(self.read.saturating_mul(2)).min(self.size).min(budget);
                if next <= self.read {
                    return Err("internal: the header needs no more bytes than were read".into());
                }
                let from = self.read;
                self.read = next;
                Ok(self.asked.ask(vec![(from, next - from)]))
            }
        }
    }

    /// The facts the projection reads (spec/zarrify/netcdf3.md §3
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#3-variables>, §6
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#6-source-metadata>).
    fn decide(&mut self, h: Header, p: Placed) {
        let format = match h.version {
            1 => "classic",
            2 => "64-bit offset",
            _ => "64-bit data",
        };
        let mut arrays = Vec::new();
        let mut unpresented = Vec::new();
        let mut reasons: BTreeMap<&str, u64> = BTreeMap::new();
        let mut names = HashSet::new();
        let mut fills = Vec::with_capacity(h.vars.len());
        for (k, v) in h.vars.iter().enumerate() {
            let Some(t) = ty(v.code) else { continue };
            let (fill, at) = fill_of(&t, v.code, &v.attrs);
            fills.push(at);
            let name = node_name(&v.name).and_then(|n| if names.insert(n.clone()) { Ok(n) } else { Err("duplicate") });
            let name = match name {
                Ok(n) => n,
                Err(why) => {
                    unpresented.push(json!({"name": text_json(&v.name), "index": k, "reason": why}));
                    *reasons.entry(why).or_default() += 1;
                    continue;
                }
            };
            let l = &p.layouts[k];
            let (attributes, types, not) = presented_attributes(&v.attrs, Some(&t));
            let mut own = Map::new();
            own.insert("index".into(), json!(k));
            own.insert("type".into(), json!(t.name));
            own.insert("record".into(), json!(l.record));
            own.insert("begin".into(), json!(v.begin));
            own.insert("vsize".into(), json!(v.vsize));
            if !types.is_empty() {
                own.insert("attributes".into(), J::Object(types));
            }
            if !not.is_empty() {
                own.insert("unpresented_attributes".into(), J::Object(not));
            }
            let store = if l.record {
                json!({"kind": "records", "offset": v.begin, "stride": p.recsize, "length": l.bytes, "records": p.records})
            } else {
                json!({"kind": "fixed", "offset": v.begin, "item": t.bytes})
            };
            let dimension_names: Vec<String> = v.dims.iter().map(|&d| h.dims.get(d).map_or(String::new(), |d| utf8_or_latin1(&d.name))).collect();
            arrays.push(json!({
                "path": name, "index": k, "shape": l.shape, "data_type": t.data_type(), "fill_value": fill,
                "codecs": [crate::out::bytes_codec(t.bytes, "big")], "dimension_names": dimension_names,
                "attributes": attributes, "own": own, "store": store,
            }));
        }
        let (attributes, types, not) = presented_attributes(&h.attrs, None);
        let dims: Vec<J> = h.dims.iter().map(|d| {
            let mut o = json!({"name": text_json(&d.name), "length": d.len});
            if d.len == 0 {
                o["unlimited"] = json!(true);
            }
            o
        }).collect();
        let mut own = Map::new();
        own.insert("format".into(), json!(format));
        own.insert("version".into(), json!(h.version));
        own.insert("numrecs".into(), h.numrecs.map_or(json!("streaming"), |n| json!(n)));
        own.insert("records".into(), json!(p.records));
        own.insert("recsize".into(), json!(p.recsize));
        own.insert("header".into(), json!(h.end));
        own.insert("dimensions".into(), J::Array(dims));
        if !types.is_empty() {
            own.insert("attributes".into(), J::Object(types));
        }
        if !not.is_empty() {
            own.insert("unpresented_attributes".into(), J::Object(not));
        }
        if !unpresented.is_empty() {
            own.insert("unpresented".into(), J::Array(unpresented));
        }
        let summary = json!({
            "format": format, "dimensions": h.dims.len(), "variables": arrays.len(), "unpresented": reasons,
            "records": p.records, "header": h.end,
        });
        self.facts = json!({"root": {"attributes": attributes, "own": own}, "arrays": arrays, "summary": summary});
        self.found = Some(Found { header: h, placed: p, fills });
    }

    /// The finished IR (spec/zarrify/netcdf3.md §6.3
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#63-the-ir-mirror>), with
    /// each variable described (`stored.rs`), and the facts.
    pub fn finish(self) -> Result<(Ir, J), String> {
        let Some(Found { header: h, placed: p, fills }) = self.found else { return Err("internal: the parse is not done".into()) };
        let mut ir = Ir::new(self.size);
        let raw = self.fed.get(0, h.end).ok_or("internal: the header was not read")?;
        let header = ir.value(0, "header", NO_INDEX, &format!("bytes[{}]", h.end), 0, (0, h.end), Some(raw))?;
        let recipe = r#"{"recipe":[["src",0,null]]}"#;
        let mut elements = vec![None; h.vars.len()];
        for (k, v) in h.vars.iter().enumerate() {
            let l = &p.layouts[k];
            if !l.record && l.bytes > 0 {
                elements[k] = Some(ir.data(0, "variables/", k as u64, 0, (v.begin, l.bytes), recipe)?);
            }
        }
        let records = match p.span {
            Some(span) => Some(ir.data(0, "records", NO_INDEX, 0, span, recipe)?),
            None => None,
        };
        let (mut ir, mut facts) = crate::sans::finish(true, ir, self.facts)?;
        let vars: Vec<stored::Var> = h.vars.iter().enumerate().map(|(k, v)| {
            let l = &p.layouts[k];
            stored::Var {
                type_at: v.type_at,
                bits: ty(v.code).map_or(8, |t| t.bytes * 8),
                shape: l.shape.clone(),
                unit: if l.record {
                    records.map(|e| stored::Unit::Records { element: e, offset: v.begin - p.begin_rec, stride: p.recsize, slice: l.bytes })
                } else {
                    elements[k].map(stored::Unit::Whole)
                },
                fill: fills.get(k).copied().unwrap_or(stored::FillAt::Default),
            }
        }).collect();
        let (arrays, chains, map) = stored::described(header, &vars)?;
        ir.describe(arrays, chains, map)?;
        facts["declared_by"] = json!(header);
        facts["copy_threshold"] = json!(crate::out::copy_threshold());
        self.asked.put(&mut facts);
        facts["summary"]["rounds"] = json!(self.asked.rounds);
        Ok((ir, facts))
    }
}

crate::parser!(Netcdf3);

/// The netCDF3 profile's file run (spec/zarrify/netcdf3.md).
pub struct Profile;

impl crate::run::FileProfile for Profile {
    /// `CDF` and the version byte 1, 2 or 5 at byte 0.
    fn detect(&self, head: &[u8]) -> bool {
        detect(head)
    }
    fn parser(&self, size: u64) -> crate::run::AnyParser {
        crate::run::AnyParser::of::<Netcdf3>(size)
    }
    fn project(&self, ir: &Ir, facts: &J, url: &str, copied: &crate::project::Copied) -> Result<crate::out::Out, String> {
        crate::project::netcdf3::project(ir, facts, url, copied)
    }
    fn reads(&self, ir: &Ir, facts: &J) -> Result<Vec<(u64, u64)>, String> {
        crate::project::netcdf3::reads(ir, facts)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The default fill values are netCDF-C's `NC_FILL_*`, as Zarr writes them.
    #[test]
    fn default_fills_are_netcdfs() {
        let fills: Vec<J> = (1..=11).map(|c| ty(c).unwrap().default_fill()).collect();
        assert_eq!(
            fills,
            vec![
                json!(-127), json!(""), json!(-32767), json!(-2147483647), json!(9.969209968386869e36), json!(9.969209968386869e36),
                json!(255), json!(65535), json!(4294967295u64), json!(-9223372036854775806i64), json!(18446744073709551614u64),
            ]
        );
    }

    /// A name is a node's name when Zarr and the hierarchy allow it.
    #[test]
    fn node_names() {
        assert_eq!(node_name(b"temp"), Ok("temp".into()));
        for (b, why) in [(&b""[..], "name"), (b".", "name"), (b"..", "name"), (b"__x", "name"), (b"a/b", "name"), (b"\xff", "name"), (b"refkv_source", "reserved")] {
            assert_eq!(node_name(b), Err(why), "{b:?}");
        }
    }
}
