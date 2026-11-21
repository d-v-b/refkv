//! The HDF5 convention's decisions (spec/zarrify/hdf5.md §2
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#2-the-hierarchy>), made from the
//! parser's description of the file (§8.7
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#87-the-description>): which
//! objects are nodes and why the others are not, each array's Zarr data type, codecs, fill value,
//! dimension names and storage, each node's attributes within their budget, and its source
//! metadata. The projection (`super::hdf5`) writes the nodes; the H5J parser reads its channels from
//! them.

use super::arr;
use crate::hdf5::types::{self, describe, present, Dt, Field, FieldKind, Kind, Present};
use crate::hdf5::{unhex, BITSHUFFLE, BLOSC, CHECKSUM, DEFLATE, FLETCHER32, LZ4, LZF, SHUFFLE, ZSTD};
use crate::rules::{self, num};
use serde_json::{json, Map, Value as J};
use std::collections::{BTreeMap, HashMap, HashSet};

/// The names a node of the hierarchy may not take at the root (the mirror's node).
const RESERVED_ROOT: &[&str] = &["refkv_source"];
/// The attribute names the hierarchy writes itself (spec/zarrify/hdf5.md §2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#2-the-hierarchy>).
const RESERVED_ATTRIBUTES: &[&str] = &["zarr_conventions", "refkv_zarrified", "_LINKS", "_DTYPE", "_REFERENCE_FIELDS"];
/// The filters the profile itself undoes, to read strings, references and records.
const READABLE: [u64; 3] = [DEFLATE, SHUFFLE, FLETCHER32];
/// Blosc's compressors by their codes, as its filter's seventh value gives them.
const BLOSC_NAMES: [&str; 6] = ["blosclz", "lz4", "lz4hc", "snappy", "zlib", "zstd"];
/// Bitshuffle's compression values (fifth): none, and `BSHUF_H5_COMPRESS_LZ4`.
const BITSHUFFLE_LZ4: u32 = 2;
/// The start of the `NAME` of a netCDF-4 dimension scale that is no variable.
const NOT_A_VARIABLE: &str = "This is a netCDF dimension but not a netCDF variable";
/// netCDF-4's prefix of a variable named as a dimension it is not the coordinate of.
const NON_COORD_PREFIX: &str = "_nc4_non_coord_";

fn n(v: &J) -> u64 {
    v.as_u64().unwrap_or(0)
}

fn u64s(v: &J) -> Vec<u64> {
    arr(v).iter().map(n).collect()
}

/// The product of `dims`, saturating.
fn product(dims: &[u64]) -> u128 {
    dims.iter().fold(1u128, |p, &d| p.saturating_mul(d as u128))
}

/// A segment of an object's path as the description gives it: its text, and whether its bytes
/// are UTF-8.
fn segment(v: &J) -> (String, bool) {
    match v {
        J::String(s) => (s.clone(), true),
        _ => (v["latin1"].as_str().unwrap_or("").to_string(), false),
    }
}

/// An object's path, its segments joined by `/`.
fn key_of(segs: &[J]) -> String {
    segs.iter().map(|s| segment(s).0).collect::<Vec<_>>().join("/")
}

/// Whether a link's name can be a node's (spec/zarrify/hdf5.md §2.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#22-paths>).
pub fn node_name(s: &str) -> bool {
    !s.is_empty() && s != "." && s != ".." && !s.starts_with("__") && !s.contains('/')
}

/// A dataset as the description gives it, its datatype decoded.
struct Ds<'a> {
    dt: Dt,
    dims: Vec<u64>,
    chunk: Vec<u64>,
    filters: Vec<u64>,
    client_data: Vec<Vec<u32>>,
    fill: Option<Vec<u8>>,
    layout: &'a str,
}

impl<'a> Ds<'a> {
    fn of(o: &'a J) -> Result<Ds<'a>, String> {
        let dt = types::parse(&unhex(o["type"].as_str().unwrap_or(""))).map_err(|_| "internal: a dataset's datatype does not read again".to_string())?;
        let filters = arr(&o["filters"]);
        Ok(Ds {
            dt,
            dims: u64s(&o["dims"]),
            chunk: u64s(&o["chunk"]),
            filters: filters.iter().map(|f| n(&f["id"])).collect(),
            client_data: filters.iter().map(|f| arr(&f["values"]).iter().map(|v| n(v) as u32).collect()).collect(),
            fill: o["fill"].as_str().map(unhex),
            layout: o["layout"].as_str().unwrap_or(""),
        })
    }
}

/// What the hierarchy holds of an object.
enum Decision<'a> {
    Group,
    Dataset(Box<Ds<'a>>, Present),
    /// the object is not a node: why
    Not(&'static str),
}

/// The text of the fixed-length string attribute `name` that the dataset's dimension scale
/// structures give, to its first NUL.
fn scale_text<'a>(o: &'a J, name: &str) -> Option<&'a str> {
    o["dimension_scale"][name].as_str()
}

/// Whether the dataset is the dimension scale netCDF-4 writes for a dimension with no variable,
/// whose `NAME` says so (spec/zarrify/hdf5.md §3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#3-datasets>).
fn dimension_only(o: &J) -> bool {
    scale_text(o, "NAME").is_some_and(|t| t.starts_with(NOT_A_VARIABLE))
}

/// Whether the dataset is a dimension scale (its `CLASS` is `DIMENSION_SCALE`).
fn is_scale(o: &J) -> bool {
    scale_text(o, "CLASS") == Some("DIMENSION_SCALE")
}

/// Whether the object has no `DIMENSION_LIST`, or one that attaches no scale to any axis.
fn attaches_none(o: &J) -> bool {
    match o.get("dimension_list") {
        None => true,
        Some(l) => l["counts"].as_array().is_some_and(|c| c.iter().all(|v| n(v) == 0)),
    }
}

/// The dimension name of a scale's link name: netCDF-4's name of the variable, without the
/// prefix it gives one that is not its dimension's coordinate.
fn dimension_name(link: &J) -> J {
    let (t, _) = segment(link);
    json!(t.strip_prefix(NON_COORD_PREFIX).unwrap_or(&t))
}

/// What the hierarchy holds of the object `o`, whose name is `name` (None for the root); an error
/// where the dataset's description records one.
fn decide<'a>(o: &'a J, name: Option<&J>) -> Result<Decision<'a>, String> {
    if let Some(name) = name {
        let (t, utf8) = segment(name);
        if !utf8 || !node_name(&t) {
            return Ok(Decision::Not("name"));
        }
    }
    match o["kind"].as_str() {
        Some("group") => return Ok(Decision::Group),
        Some("dataset") => {}
        Some("named_datatype") => return Ok(Decision::Not("named_datatype")),
        _ => return Ok(Decision::Not("object")),
    }
    if let Some(m) = o["error"].as_str() {
        return Err(m.to_string());
    }
    let ds = Ds::of(o)?;
    if let Some(external) = o.get("external") {
        let presentable = matches!(present(&ds.dt), Ok(Present::Fixed { .. } | Present::Bytes { .. })) && ds.layout == "contiguous";
        if !presentable || external.is_null() {
            return Ok(Decision::Not("external_storage"));
        }
    }
    match ds.layout {
        "null" => return Ok(Decision::Not("null_dataspace")),
        "virtual" => return Ok(Decision::Not("layout")),
        _ => {}
    }
    if dimension_only(o) {
        return Ok(Decision::Not("dimension_only"));
    }
    let p = match present(&ds.dt) {
        Ok(p) => p,
        Err(why) => return Ok(Decision::Not(why)),
    };
    if ds.layout == "chunked" {
        // strings, references and records are decoded by the parser, by each chunk's mask, so only
        // with the filters it undoes; the others are the readers'
        let decoded = matches!(p, Present::Strings | Present::References | Present::Records(_));
        if !presented_filters(&ds, decoded) {
            return Ok(Decision::Not("filter"));
        }
        if o["raw_edges"].as_bool().unwrap_or(false) {
            return Ok(Decision::Not("edge_chunks"));
        }
        if !decoded && !chunk_parts_of(&ds, &o["storage"]).is_some_and(|c| c.iter().all(|p| p.1.is_some())) {
            return Ok(Decision::Not("filter_mask"));
        }
    }
    let bytes = product(&ds.dims).saturating_mul(ds.dt.size as u128);
    let storage = &o["storage"];
    match ds.layout {
        "contiguous" if !storage["address"].is_null() && n(&storage["length"]) as u128 != bytes => return Ok(Decision::Not("size")),
        "compact" if n(&storage["length"]) as u128 != bytes => return Ok(Decision::Not("size")),
        _ => {}
    }
    if let Some(f) = &ds.fill {
        if fill_json(&ds.dt, &p, f).is_none() {
            return Ok(Decision::Not("fill_value"));
        }
    }
    Ok(Decision::Dataset(Box::new(ds), p))
}

/// Whether the dataset's filters are ones a reader's codecs undo, in an order they can: Fletcher32
/// only last, as its checksum is then the chunk's last bytes; when `decoded`, only the filters the
/// profile undoes itself.
fn presented_filters(ds: &Ds, decoded: bool) -> bool {
    let k = ds.filters.len();
    ds.filters.iter().enumerate().all(|(i, &f)| f != FLETCHER32 || i + 1 == k)
        && ds.filters.iter().zip(&ds.client_data).all(|(&f, cd)| {
            if decoded {
                READABLE.contains(&f)
            } else {
                f == FLETCHER32 || filter_codec(f, cd, ds.dt.size).is_some()
            }
        })
}

/// How a chunk's reference reads it: the `stored` bytes before any checksum, in the literal framing
/// of the filter `wrap`, the one that skipped it, if any.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct ChunkPart {
    pub stored: u64,
    pub wrap: Option<u64>,
}

/// The reference of each chunk (its size and filter mask) of a dataset of `filters` whose chunks
/// hold `bytes` bytes (spec/zarrify/hdf5.md §3.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#32-chunks>), None for a chunk that
/// has none; None for all when the chunks deflate skipped, which are read, pass `budget` bytes.
pub fn chunk_parts(filters: &[u64], bytes: u128, chunks: impl Iterator<Item = (u64, u32)>, budget: u64) -> Option<Vec<Option<ChunkPart>>> {
    let bit = |id: u64| filters.iter().position(|&f| f == id).map_or(0u32, |k| 1 << k);
    let checksum = bit(FLETCHER32);
    let every = 1u32.checked_shl(filters.len() as u32).map_or(u32::MAX, |b| b - 1);
    let mut read = 0u64;
    let mut out = Vec::new();
    for (size, mask) in chunks {
        let trim = if checksum != 0 && mask & checksum == 0 { CHECKSUM } else { 0 };
        let rest = mask & every & !checksum;
        let part = if size <= trim {
            None
        } else if rest == 0 {
            Some(ChunkPart { stored: size - trim, wrap: None })
        } else {
            // one filter skipped, of those with a literal form, and all the chunk's bytes are there
            let skipped = filters.iter().copied().find(|&f| bit(f) == rest).filter(|f| super::hdf5::framed(*f, size - trim));
            match skipped {
                Some(f) if (size - trim) as u128 == bytes => {
                    if f == DEFLATE {
                        read = read.saturating_add(size - trim);
                    }
                    Some(ChunkPart { stored: size - trim, wrap: Some(f) })
                }
                _ => None,
            }
        };
        out.push(part);
    }
    (read <= budget).then_some(out)
}

/// The deflate budget of the chunks a filter skipped (the bytes the parser reads of them).
fn storage_budget() -> u64 {
    rules::of("hdf5").limit("max_storage_bytes") as u64
}

/// Each masked chunk of a chunked dataset's description with its reference, by its grid index;
/// None when one of its chunks has no reference: a masked one, or, with Fletcher32, one no longer
/// than its checksum.
fn chunk_parts_of<'a>(ds: &Ds, storage: &'a J) -> Option<Vec<(&'a J, Option<ChunkPart>)>> {
    let masked = arr(&storage["masked"]);
    let bytes = product(&ds.chunk).saturating_mul(ds.dt.size as u128);
    let parts = chunk_parts(&ds.filters, bytes, masked.iter().map(|m| (n(&m["size"]), n(&m["mask"]) as u32)), storage_budget())?;
    let short = ds.filters.contains(&FLETCHER32) && storage["shortest"].as_u64().is_some_and(|s| s <= CHECKSUM);
    (!short).then(|| masked.iter().zip(parts).collect())
}

/// The JSON fill value of a dataset of `dt` presented as `p`, from the fill value message's
/// bytes `f`; None when the data type has no JSON form of it.
fn fill_json(dt: &Dt, p: &Present, f: &[u8]) -> Option<J> {
    struct NoLookup;
    impl types::Lookup for NoLookup {
        fn heap(&self, _: u64, _: u64) -> Option<Vec<u8>> {
            None
        }
        fn path(&self, _: u64) -> Option<String> {
            None
        }
    }
    match p {
        Present::Strings | Present::References | Present::Records(_) => Some(json!("")),
        Present::Bytes { .. } => f.iter().all(|&b| b == f[0]).then(|| json!(f.first().copied().unwrap_or(0))),
        // an integer's exact value whatever its magnitude, as Zarr v3 writes one, at the top
        // level and as an enumeration's or a compound's member alike (spec/zarrify/hdf5.md §6
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#6-attributes>)
        Present::Fixed { .. } => types::value_json(dt, f, &NoLookup),
    }
}

/// The fill value 0 of a Zarr data type: `false`, `0.0`, `""`, or a struct's object of its
/// fields' zeros.
fn zero_of(t: &J) -> J {
    match t {
        J::String(s) if s == "bool" => json!(false),
        J::String(s) if s.starts_with("float") => json!(0.0),
        J::String(s) if s == "string" => json!(""),
        J::Object(o) if o.get("name") == Some(&json!("fixed_length_utf32")) => json!(""),
        J::Object(_) => {
            let mut m = Map::new();
            for f in t["configuration"]["fields"].as_array().into_iter().flatten() {
                m.insert(f["name"].as_str().unwrap_or("").to_string(), zero_of(&f["data_type"]));
            }
            J::Object(m)
        }
        _ => json!(0),
    }
}

/// The codec of a filter in a pipeline of elements of `size` bytes, from its client data values;
/// None for Fletcher32, whose checksum the chunk's reference leaves out, or a filter with values
/// no reader's codec has a form of.
fn filter_codec(f: u64, cd: &[u32], size: u64) -> Option<J> {
    match f {
        DEFLATE => Some(json!({"name": "numcodecs.zlib", "configuration": {"level": 1}})),
        SHUFFLE => Some(json!({"name": "numcodecs.shuffle", "configuration": {"elementsize": size}})),
        LZF => Some(json!({"name": "imagecodecs_lzf", "configuration": {"header": false}})),
        LZ4 => Some(json!({"name": "imagecodecs_lz4h5"})),
        ZSTD => Some(json!({"name": "zstd", "configuration": {"level": cd.first().copied().unwrap_or(0), "checksum": false}})),
        BLOSC => {
            // the values 0 to 3 are the filter's own; a pipeline without the others has its defaults
            let cname = BLOSC_NAMES.get(cd.get(6).copied().unwrap_or(0) as usize)?;
            let shuffle = ["noshuffle", "shuffle", "bitshuffle"].get(cd.get(5).copied().unwrap_or(1) as usize)?;
            Some(json!({"name": "blosc", "configuration": {
                "cname": cname, "clevel": cd.get(4).copied().unwrap_or(5), "shuffle": shuffle, "typesize": size, "blocksize": 0
            }}))
        }
        BITSHUFFLE => {
            let compression = match cd.get(4).copied().unwrap_or(0) {
                0 => "none",
                BITSHUFFLE_LZ4 => "lz4",
                _ => return None,
            };
            let block = match cd.get(3).copied().unwrap_or(0) as u64 {
                0 => crate::h5_bitshuffle::default_block(size),
                b => b,
            };
            Some(json!({"name": "refkv.h5_bitshuffle", "configuration": {"elementsize": size, "block_size": block, "compression": compression}}))
        }
        _ => None,
    }
}

/// An array's Zarr data type, codecs, fill value, and trailing axis (a fixed-length string's
/// bytes) (spec/zarrify/hdf5.md §3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#3-datasets>).
fn array_type(ds: &Ds, p: &Present) -> (J, J, J, Option<u64>) {
    let fill = ds.fill.as_ref().and_then(|f| fill_json(&ds.dt, p, f));
    let filters = |size: u64| -> Vec<J> { ds.filters.iter().zip(&ds.client_data).filter_map(|(&f, cd)| filter_codec(f, cd, size)).collect() };
    match p {
        Present::Strings | Present::References => (json!("string"), json!([{"name": "vlen-utf8"}]), json!(""), None),
        // the data type and fill value are the records' (`records_of`)
        Present::Records(_) => (J::Null, json!([{"name": "bytes", "configuration": {"endian": "little"}}]), J::Null, None),
        Present::Bytes { n } => {
            let mut codecs = vec![json!({"name": "bytes"})];
            codecs.extend(filters(*n));
            (json!("uint8"), J::Array(codecs), fill.unwrap_or(json!(0)), Some(*n))
        }
        Present::Fixed { data_type, big, one_byte } => {
            let mut codecs = vec![if *one_byte && !data_type.is_object() {
                json!({"name": "bytes"})
            } else {
                json!({"name": "bytes", "configuration": {"endian": if *big { "big" } else { "little" }}})
            }];
            codecs.extend(filters(ds.dt.size));
            let fill = fill.unwrap_or_else(|| zero_of(data_type));
            (data_type.clone(), J::Array(codecs), fill, None)
        }
    }
}

/// A dataset of records as hdmf-zarr writes it (spec/zarrify/hdf5.md §3.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#33-compounds>): its `struct`
/// data type, whose strings and references are fixed-length UTF-32 of the longest one's code points
/// (at least 1), its fill value, its elements' bytes (little-endian), and an element's size; from
/// the elements' bytes `b` and, for each string or reference member, its texts.
fn records_of(fields: &[Field], b: &[u8], texts: &J, size: u64) -> (J, J, Vec<u8>, u64) {
    let size = size as usize;
    let count = if size == 0 { 0 } else { b.len() / size };
    // each string and reference member's texts, as code points
    let texts: Vec<Vec<Vec<char>>> = fields
        .iter()
        .enumerate()
        .map(|(j, f)| match f.kind {
            FieldKind::Fixed { .. } => Vec::new(),
            _ => arr(&texts[j]).iter().map(|t| t.as_str().unwrap_or("").chars().collect()).collect(),
        })
        .collect();
    let widths: Vec<usize> = texts.iter().map(|t| t.iter().map(|c| c.len()).max().unwrap_or(0).max(1)).collect();
    let mut out_fields = Vec::with_capacity(fields.len());
    let mut fill = Map::new();
    let mut item = 0u64;
    for (j, f) in fields.iter().enumerate() {
        let t = match &f.kind {
            FieldKind::Fixed { data_type, size, .. } => {
                item += size;
                fill.insert(f.name.clone(), zero_of(data_type));
                data_type.clone()
            }
            _ => {
                item += 4 * widths[j] as u64;
                fill.insert(f.name.clone(), json!(""));
                json!({"name": "fixed_length_utf32", "configuration": {"length_bytes": 4 * widths[j]}})
            }
        };
        out_fields.push(json!({"name": f.name, "data_type": t}));
    }
    let mut bytes = Vec::with_capacity(count * item as usize);
    for k in 0..count {
        for (j, f) in fields.iter().enumerate() {
            match &f.kind {
                FieldKind::Fixed { size: w, big, .. } => {
                    let at = k * size + f.offset as usize;
                    let v = &b[at..at + *w as usize];
                    if *big {
                        bytes.extend(v.iter().rev());
                    } else {
                        bytes.extend_from_slice(v);
                    }
                }
                _ => {
                    let t = texts[j].get(k).map_or(&[][..], |t| t.as_slice());
                    for c in t {
                        bytes.extend_from_slice(&(*c as u32).to_le_bytes());
                    }
                    bytes.resize(bytes.len() + 4 * (widths[j] - t.len()), 0);
                }
            }
        }
    }
    (json!({"name": "struct", "configuration": {"fields": out_fields}}), J::Object(fill), bytes, item)
}

/// The attributes of an object as the hierarchy presents them (spec/zarrify/hdf5.md §6
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#6-attributes>): those presented,
/// their datatypes, and those not presented, each with why; an error where an attribute the
/// hierarchy reads does not read.
fn attributes_of(o: &J) -> Result<(Map<String, J>, Map<String, J>, Map<String, J>), String> {
    let mut values: Vec<(String, J)> = Vec::new();
    let (mut types_of, mut not) = (Map::new(), Map::new());
    for a in arr(&o["attributes"]) {
        let name = a["name"].as_str().unwrap_or("").to_string();
        if RESERVED_ATTRIBUTES.contains(&name.as_str()) {
            not.insert(name, json!("reserved"));
            continue;
        }
        if let Some(m) = a["error"].as_str() {
            return Err(m.to_string());
        }
        if a["shared"] == true {
            not.insert(name, json!("shared"));
            continue;
        }
        types_of.insert(name.clone(), a["datatype"].clone());
        match a.get("value") {
            Some(v) => values.push((name, v.clone())),
            None => {
                not.insert(name, json!("datatype"));
            }
        }
    }
    // the budget: the largest attributes left out first, until the rest fit
    let budget = rules::of("hdf5").limit("attribute_bytes") as usize;
    let mut sizes: Vec<(usize, usize)> = values.iter().enumerate().map(|(k, (n, v))| (crate::out::json_size(v) + n.len() + 4, k)).collect();
    let mut total: usize = sizes.iter().map(|s| s.0).sum();
    sizes.sort_by(|a, b| b.0.cmp(&a.0).then(a.1.cmp(&b.1)));
    let mut dropped = HashSet::new();
    for (s, k) in sizes {
        if total <= budget {
            break;
        }
        total -= s;
        dropped.insert(k);
    }
    let mut out = Map::new();
    for (k, (n, v)) in values.into_iter().enumerate() {
        if dropped.contains(&k) {
            not.insert(n, json!("budget"));
        } else {
            out.insert(n, v);
        }
    }
    Ok((out, types_of, not))
}

/// The value of the `_FillValue` attribute, of one number, of a dataset of the numeric datatype
/// `of`, as xarray's Zarr backend reads the member of that name (spec/zarrify/hdf5.md §6
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#6-attributes>): for an integer
/// dataset the integer, exactly, whatever its magnitude (one the dataset's type does not have
/// too: xarray reads the number, where it refuses the attribute's list); a float's the Base64 of
/// its 8 little-endian bytes as a float64.
fn xarray_fill(o: &J, of: &Dt) -> Option<J> {
    let a = arr(&o["attributes"]).iter().find(|a| a["name"] == "_FillValue")?;
    let dt = types::parse(&unhex(a["type"].as_str()?)).ok()?;
    let data = unhex(a["data"].as_str()?);
    if data.len() as u64 != dt.size {
        return None;
    }
    match of.kind {
        Kind::Float { .. } => Some(json!(crate::mirror::base64(&types::as_f64(&dt, &data)?.to_le_bytes()))),
        Kind::Int { .. } if types::number(of).is_some() => {
            // the attribute's integer, read exactly (an integer attribute's), or a float's value
            // when it is an integer, exactly (its decimal digits, every one, however large)
            match dt.kind {
                Kind::Int { big, signed, .. } if types::number(&dt).is_some() => Some(crate::out::int_json(types::int_of(&data, big, signed))),
                Kind::Float { .. } => {
                    let x = types::as_f64(&dt, &data)?;
                    // + 0.0: -0.0 as 0
                    (x.is_finite() && x.fract() == 0.0).then(|| J::Number(format!("{:.0}", x + 0.0).parse().expect("an integer's digits")))
                }
                _ => None,
            }
        }
        _ => None,
    }
}

/// The dimension names of the array of the dataset `o`, of link path `segs`, without a trailing
/// axis: the first scale `DIMENSION_LIST` attaches to each axis, by link name; a dimension scale
/// of rank 1 without one is its own (spec/zarrify/hdf5.md §3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#3-datasets>). None when no axis
/// has a name.
fn dimension_names(o: &J, rank: usize, segs: &[J], segs_of: &HashMap<u64, &[J]>) -> Option<Vec<J>> {
    let mut names = vec![J::Null; rank];
    match o.get("dimension_list").filter(|l| l.get("unread").is_none()) {
        Some(list) => {
            let scales = list["scales"].as_array()?;
            for (name, scale) in names.iter_mut().zip(scales) {
                let first = scale.as_array().and_then(|s| s.first()).and_then(J::as_u64);
                if let Some(segs) = first.and_then(|a| segs_of.get(&a)) {
                    *name = segs.last().map_or(J::Null, dimension_name);
                }
            }
        }
        None if rank == 1 && is_scale(o) => names[0] = segs.last().map_or(J::Null, dimension_name),
        None => {}
    }
    names.iter().any(|n| !n.is_null()).then_some(names)
}

/// What each dataset of rank 1 or more counts as among its group's dimensions, by address
/// ([`name_axes`]): `{"scale": [name, length]}` for a dimension scale, `{"unscaled": sizes}`
/// for a dataset no `DIMENSION_LIST` attaches a scale to.
fn axes_hints(objects: &[J]) -> HashMap<u64, J> {
    let mut out = HashMap::new();
    for o in objects {
        let (Some(name), "dataset") = (arr(&o["segments"]).last(), o["kind"].as_str().unwrap_or("")) else { continue };
        let dims = u64s(&o["dims"]);
        if dims.is_empty() {
            continue;
        }
        let hint = if is_scale(o) {
            json!({"scale": [dimension_name(name), dims[0]]})
        } else if attaches_none(o) {
            json!({"unscaled": dims})
        } else {
            continue;
        };
        out.insert(n(&o["address"]), hint);
    }
    out
}

/// The hierarchy's nodes, as the projection writes them, and the summary of what the hierarchy
/// holds.
pub struct Nodes {
    pub nodes: Vec<J>,
    pub summary: J,
}

/// The nodes of the HDF5 file `description` describes (spec/zarrify/hdf5.md §2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#2-the-hierarchy>); an error, the
/// input rejected, where a dataset the hierarchy reads, or a structure of a node, does not read.
pub fn nodes(description: &J) -> Result<Nodes, String> {
    let objects = arr(&description["objects"]);
    let path = |o: &J| format!("/{}", key_of(arr(&o["segments"])));
    let segs_of: HashMap<u64, &[J]> = objects.iter().map(|o| (n(&o["address"]), arr(&o["segments"]))).collect();
    let path_of: HashMap<u64, String> = objects.iter().map(|o| (n(&o["address"]), path(o))).collect();
    // the datasets the hierarchy reads the elements of, each read and within the budget of strings
    // (spec/zarrify/hdf5.md §8.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#83-checks>)
    let rules = rules::of("hdf5");
    let mut strings = 0u64;
    for o in objects {
        let at = |m: &str| format!("{}: {m}", path(o));
        let Decision::Dataset(_, p) = decide(o, arr(&o["segments"]).last()).map_err(|m| at(&m))? else { continue };
        if let Some(m) = o["unread"].as_str() {
            return Err(at(m));
        }
        if matches!(p, Present::Strings | Present::References | Present::Records(_)) && o.get("values").is_none() {
            return Err(at("internal: the elements of a dataset the hierarchy reads were not read"));
        }
        if p == Present::Strings {
            strings += n(&o["values"]["count"]);
            rules.check("strings", rules::env([("strings", num(strings as f64))])).map_err(|m| at(&m))?;
        }
    }
    let mut nodes = Vec::new();
    let mut counts: BTreeMap<&'static str, u64> = BTreeMap::new();
    let mut not_counts: BTreeMap<String, u64> = BTreeMap::new();
    let (mut n_strings, mut n_refs, mut chunks_total) = (0u64, 0u64, 0u64);
    let mut link_counts = [0u64; 3];
    let mut hints = axes_hints(objects);
    let mut unpresented_axes: HashMap<String, Vec<J>> = HashMap::new();
    // the presented objects by their segments, as JSON: a name that is not UTF-8 is never one
    // with UTF-8 text of the same characters
    let mut presented_paths: HashSet<String> = HashSet::new();
    let segments_key = |segs: &[J]| J::Array(segs.to_vec()).to_string();
    let mut unpresented_of: HashMap<String, Map<String, J>> = HashMap::new();
    for o in objects {
        let segs = arr(&o["segments"]);
        let key = key_of(segs);
        let parent = key_of(&segs[..segs.len().saturating_sub(1)]);
        if segs.len() > 1 && !presented_paths.contains(&segments_key(&segs[..segs.len() - 1])) {
            continue;
        }
        let at = n(&o["address"]);
        let mut d = decide(o, segs.last())?;
        if segs.len() == 1 && RESERVED_ROOT.contains(&key.as_str()) {
            d = Decision::Not("reserved");
        }
        if let Decision::Not(why) = d {
            let name = segs.last().map_or(String::new(), |s| segment(s).0);
            if let Some(hint) = hints.remove(&at) {
                unpresented_axes.entry(parent.clone()).or_default().push(json!([name, hint]));
            }
            unpresented_of.entry(parent).or_default().insert(name, json!(why));
            *not_counts.entry(why.to_string()).or_default() += 1;
            continue;
        }
        presented_paths.insert(segments_key(segs));
        let (attributes, attr_types, attr_not) = attributes_of(o)?;
        let mut own = Map::new();
        own.insert("address".into(), json!(at));
        match d {
            Decision::Group => {
                *counts.entry("groups").or_default() += 1;
                let (mut links_json, mut external) = (Vec::new(), Vec::new());
                for l in arr(&o["links"]) {
                    let name = segment(&l["name"]).0;
                    if let Some(p) = l.get("soft") {
                        link_counts[0] += 1;
                        links_json.push(json!({"name": name, "source": ".", "path": p}));
                    } else if let Some(a) = l["hard"].as_u64() {
                        let mut child = segs.to_vec();
                        child.push(l["name"].clone());
                        if segs_of.get(&a).is_none_or(|s| *s != child.as_slice()) {
                            link_counts[1] += 1;
                            let target = path_of.get(&a).cloned().unwrap_or_else(|| format!("object at {a}"));
                            links_json.push(json!({"name": name, "source": ".", "path": target}));
                        }
                    } else {
                        link_counts[2] += 1;
                        external.push(json!({"name": name, "type": l["type"], "file": l["file"], "path": l["path"]}));
                    }
                }
                if !attr_types.is_empty() {
                    own.insert("attributes".into(), J::Object(attr_types));
                }
                if !attr_not.is_empty() {
                    own.insert("unpresented_attributes".into(), J::Object(attr_not));
                }
                if !external.is_empty() {
                    own.insert("external_links".into(), J::Array(external));
                }
                if segs.is_empty() {
                    own.insert("superblock".into(), description["superblock"].clone());
                    if n(&description["user_block"]) > 0 {
                        own.insert("user_block".into(), description["user_block"].clone());
                    }
                }
                let mut attrs = attributes;
                if !links_json.is_empty() {
                    attrs.insert("_LINKS".into(), J::Array(links_json));
                }
                let mut node = json!({"path": key, "segments": segs.len(), "attributes": attrs, "own": own});
                if let Some(order) = o.get("link_order") {
                    node["link_order"] = order.clone();
                }
                nodes.push(node);
            }
            Decision::Dataset(ds, p) => {
                *counts.entry("arrays").or_default() += 1;
                let mut attrs = attributes;
                let (mut data_type, codecs, mut fill, trailing) = array_type(&ds, &p);
                let mut record_type: Option<(J, J)> = None;
                let storage = &o["storage"];
                let values = &o["values"];
                let invalid = || values["invalid"].as_str().map(str::to_string);
                let item = if trailing.is_some() { 1 } else { ds.dt.size };
                let mut store = match (&p, ds.layout) {
                    (Present::Strings, _) => {
                        if let Some(m) = invalid() {
                            return Err(m);
                        }
                        n_strings += n(&values["count"]);
                        json!({"kind": "strings", "spans": values["spans"]})
                    }
                    (Present::Records(fields), _) => {
                        if let Some(m) = invalid() {
                            return Err(m);
                        }
                        let b = unhex(values["elements"].as_str().unwrap_or(""));
                        let (data_type, fill, bytes, item) = records_of(fields, &b, &values["texts"], ds.dt.size);
                        let refs: Vec<&str> = fields.iter().filter(|f| f.kind == FieldKind::Reference).map(|f| f.name.as_str()).collect();
                        if !refs.is_empty() {
                            attrs.insert("_REFERENCE_FIELDS".into(), json!(refs));
                        }
                        record_type = Some((data_type, fill));
                        json!({"kind": "records", "hex": crate::hdf5::hex(&bytes), "item": item})
                    }
                    (Present::References, _) => {
                        let paths = arr(&values["paths"]);
                        n_refs += paths.len() as u64;
                        attrs.insert("_DTYPE".into(), json!("object_reference"));
                        json!({"kind": "references", "paths": paths})
                    }
                    (_, "compact") => json!({"kind": "compact", "offset": storage["offset"], "length": storage["length"]}),
                    (_, "contiguous") if o.get("external").is_some() => {
                        let ranges = o["external"].clone();
                        own.insert("external_storage".into(), ranges.clone());
                        json!({"kind": "external", "address": at, "ranges": ranges, "item": item})
                    }
                    (_, "contiguous") => match storage["address"].as_u64() {
                        Some(_) if n(&storage["length"]) > 0 => {
                            if storage["outside"] == true {
                                return Err(format!("{}: its data lies outside the file", path(o)));
                            }
                            json!({"kind": "contiguous", "at": at, "offset": storage["offset"], "length": storage["length"], "item": item, "element": storage["element"]})
                        }
                        _ => json!({"kind": "none"}),
                    },
                    (_, _) => {
                        let count = n(&storage["count"]);
                        chunks_total += count;
                        let mut chunk = ds.chunk.clone();
                        if let Some(t) = trailing {
                            chunk.push(t);
                        }
                        if count == 0 {
                            json!({"kind": "none", "chunk": chunk})
                        } else {
                            let parts = chunk_parts_of(&ds, storage).ok_or("internal: a chunk's filter mask")?;
                            let mut masked = Vec::new();
                            for (m, part) in parts {
                                let part = part.ok_or("internal: a chunk's filter mask")?;
                                masked.push(json!({"index": m["index"], "stored": part.stored, "wrap": part.wrap, "adler": m.get("adler").cloned().unwrap_or(J::Null), "item": ds.dt.size}));
                            }
                            let trim = if ds.filters.contains(&FLETCHER32) { CHECKSUM } else { 0 };
                            json!({"kind": "chunked", "at": at, "grid": o["grid"], "chunk": chunk, "trailing": trailing.is_some(), "trim": trim, "masked": masked, "element": storage["element"]})
                        }
                    }
                };
                if let Some(object) = storage.get("object") {
                    store["object"] = object.clone();
                }
                if let Some((t, f)) = record_type {
                    data_type = t;
                    fill = f;
                }
                let mut dimension_names = dimension_names(o, ds.dims.len(), segs, &segs_of);
                if dimension_names.is_some() {
                    for v in attrs.values_mut() {
                        if let J::Array(values) = v {
                            if values.len() == 1 && !values[0].is_array() {
                                *v = values.remove(0);
                            }
                        }
                    }
                }
                if let Some(fill) = xarray_fill(o, &ds.dt) {
                    attrs.insert("_FillValue".into(), fill);
                }
                let mut shape = ds.dims.clone();
                if let Some(t) = trailing {
                    shape.push(t);
                    if ds.dims.is_empty() {
                        dimension_names = Some(Vec::new());
                    }
                    if let Some(names) = &mut dimension_names {
                        names.push(json!(format!("string{t}")));
                    }
                }
                own.insert("datatype".into(), describe(&ds.dt));
                if let Some(m) = o.get("maxdims") {
                    own.insert("maxshape".into(), m.clone());
                }
                own.insert("layout".into(), json!(ds.layout));
                if ds.layout == "chunked" {
                    own.insert("chunks".into(), json!(ds.chunk));
                    own.insert("index".into(), o["index"].clone());
                }
                if !ds.filters.is_empty() {
                    own.insert("filters".into(), json!(ds.filters));
                }
                if let Some(f) = &ds.fill {
                    own.insert("fill_value".into(), json!(crate::mirror::base64(f)));
                }
                if !attr_types.is_empty() {
                    own.insert("attributes".into(), J::Object(attr_types));
                }
                if !attr_not.is_empty() {
                    own.insert("unpresented_attributes".into(), J::Object(attr_not));
                }
                nodes.push(json!({"path": key, "segments": segs.len(), "attributes": attrs, "own": own,
                                  "array": {"shape": shape, "data_type": data_type, "fill_value": fill, "codecs": codecs, "store": store,
                                            "dimension_names": dimension_names}}));
                if let Some(hint) = hints.remove(&at) {
                    nodes.last_mut().unwrap()["axes"] = hint;
                }
            }
            Decision::Not(_) => unreachable!(),
        }
    }
    // each group's children that are not nodes, by the group's path
    for node in nodes.iter_mut() {
        if node.get("array").is_some() {
            continue;
        }
        let key = node["path"].as_str().unwrap_or("").to_string();
        if let Some(m) = unpresented_of.get(&key) {
            node["own"]["unpresented"] = J::Object(m.clone());
        }
        if let Some(a) = unpresented_axes.get(&key) {
            node["axes"] = J::Array(a.clone());
        }
    }
    let root_attrs = nodes.first().map(|n| n["attributes"].clone()).unwrap_or(J::Null);
    let mut summary = json!({
        "objects": objects.len(),
        "groups": counts.get("groups").copied().unwrap_or(0),
        "arrays": counts.get("arrays").copied().unwrap_or(0),
        "unpresented": not_counts,
        "strings": n_strings,
        "references": n_refs,
        "links": {"soft": link_counts[0], "hard": link_counts[1], "external": link_counts[2]},
        "chunks": chunks_total,
    });
    if root_attrs["neurodata_type"] == json!("NWBFile") {
        let mut namespaces = Map::new();
        if let Some(J::Object(r)) = root_attrs.get(".specloc") {
            if let Some(p) = r.get("_REFERENCE").and_then(|x| x["path"].as_str()) {
                let pre: Vec<&str> = p.trim_start_matches('/').split('/').filter(|s| !s.is_empty()).collect();
                for o in objects {
                    let key: Vec<String> = arr(&o["segments"]).iter().map(|s| segment(s).0).collect();
                    if key.len() == pre.len() + 2 && key[..pre.len()] == pre[..] {
                        let e = namespaces.entry(key[pre.len()].clone()).or_insert(json!([]));
                        e.as_array_mut().unwrap().push(json!(key[pre.len() + 1]));
                    }
                }
            }
        }
        summary["nwb"] = json!({"version": root_attrs["nwb_version"], "namespaces": namespaces});
    }
    name_axes(&mut nodes);
    Ok(Nodes { nodes, summary })
}

/// Names the axes of each unscaled array of `nodes` as h5netcdf's `phony_dims="sort"` does
/// (spec/zarrify/hdf5.md §3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#3-datasets>), from the
/// `axes` of each array and group ([`axes_hints`]). A group's members and subgroups are
/// taken in the creation order of their links when its `link_order` records it, as h5py iterates
/// them, else in ascending byte order of their names, and groups depth first in that order; a
/// group's dimensions are its dimension scales, then as many
/// `phony_dim_<n>` of each length as one unscaled dataset of it has axes of that length, `n`
/// counting the hierarchy's dimensions before it; an unscaled array's axes take its group's
/// dimensions of their lengths in turn.
pub fn name_axes(nodes: &mut [J]) {
    let segments = |p: &str| -> Vec<String> { if p.is_empty() { Vec::new() } else { p.split('/').map(str::to_string).collect() } };
    // each tracking group's links' creation order, by its path
    let created: HashMap<String, Map<String, J>> = nodes
        .iter()
        .filter_map(|n| Some((n["path"].as_str()?.to_string(), n.get("link_order")?.as_object()?.clone())))
        .collect();
    let rank = |group: &str, name: &str| -> (u64, String) {
        (created.get(group).and_then(|m| m.get(name)).and_then(J::as_u64).unwrap_or(0), name.to_string())
    };
    // each group's members, by its path: (link name, hint, the array's node when presented)
    let mut members: HashMap<String, Vec<(String, J, Option<usize>)>> = HashMap::new();
    let mut groups: Vec<Vec<(u64, String)>> = Vec::new();
    for (k, node) in nodes.iter().enumerate() {
        let path = node["path"].as_str().unwrap_or("");
        if node.get("array").is_none() {
            let segs = segments(path);
            groups.push(segs.iter().enumerate().map(|(k, s)| rank(&segs[..k].join("/"), s)).collect());
            for m in node["axes"].as_array().into_iter().flatten() {
                members.entry(path.to_string()).or_default().push((m[0].as_str().unwrap_or("").to_string(), m[1].clone(), None));
            }
        } else if let Some(hint) = node.get("axes") {
            let (parent, name) = path.rsplit_once('/').unwrap_or(("", path));
            members.entry(parent.to_string()).or_default().push((name.to_string(), hint.clone(), Some(k)));
        }
    }
    groups.sort();
    let lengths = |v: &J| -> Vec<u64> { v.as_array().into_iter().flatten().filter_map(J::as_u64).collect() };
    let mut next = 0u64;
    for g in groups {
        let path = g.iter().map(|s| s.1.as_str()).collect::<Vec<_>>().join("/");
        let Some(mut list) = members.remove(&path) else { continue };
        list.sort_by_cached_key(|m| rank(&path, &m.0));
        // the group's dimensions of each length: its scales', then its phony ones
        let mut of_length: HashMap<u64, Vec<J>> = HashMap::new();
        for (_, hint, _) in &list {
            if let Some(s) = hint.get("scale") {
                of_length.entry(s[1].as_u64().unwrap_or(0)).or_default().push(s[0].clone());
                next += 1;
            }
        }
        // the most axes of each length one unscaled dataset has, in order of first appearance
        let mut most: Vec<(u64, usize)> = Vec::new();
        for (_, hint, _) in &list {
            let dims = lengths(&hint["unscaled"]);
            for &n in &dims {
                let c = dims.iter().filter(|&&d| d == n).count();
                match most.iter_mut().find(|e| e.0 == n) {
                    Some(e) => e.1 = e.1.max(c),
                    None => most.push((n, c)),
                }
            }
        }
        for (n, c) in most {
            let names = of_length.entry(n).or_default();
            while names.len() < c {
                names.push(json!(format!("phony_dim_{next}")));
                next += 1;
            }
        }
        for (_, hint, k) in list {
            let (Some(k), Some(dims)) = (k, hint.get("unscaled").map(lengths)) else { continue };
            let mut taken: HashMap<u64, usize> = HashMap::new();
            let mut names: Vec<J> = dims
                .iter()
                .map(|n| {
                    let t = taken.entry(*n).or_default();
                    *t += 1;
                    of_length[n][*t - 1].clone()
                })
                .collect();
            let array = &mut nodes[k]["array"];
            // the trailing axis of a fixed-length string
            if let Some(n) = array["shape"].get(dims.len()).and_then(J::as_u64) {
                names.push(json!(format!("string{n}")));
            }
            array["dimension_names"] = J::Array(names);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A group under a group that is not presented is not presented, whatever its parent's name
    /// reads as: here a name not UTF-8 whose Latin-1 text is a presented group's name.
    #[test]
    fn a_child_of_a_group_not_presented_is_not_a_node() {
        let group = |segments: J| json!({"address": 0, "segments": segments, "kind": "group", "attributes": [], "links": []});
        let description = json!({"superblock": 2, "user_block": 0, "objects": [
            group(json!([])), group(json!(["é"])), group(json!([{"latin1": "é"}])), group(json!([{"latin1": "é"}, "kid"])),
        ]});
        let paths: Vec<J> = nodes(&description).unwrap().nodes.iter().map(|n| n["path"].clone()).collect();
        assert_eq!(paths, [json!(""), json!("é")]);
    }
}
