//! The netCDF3 projection (spec/zarrify/netcdf3.md §2
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#2-the-hierarchy>) from a
//! valid IR and the parser's description of the file (§7.7
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#77-the-description>): the
//! convention's decisions, which variables are nodes, the attributes presented, each array's data
//! type, fill value, codecs and dimension names, are made here. A fixed-size variable's values are
//! cut as conventions §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#7-source-values-as-arrays> cuts
//! contiguous values; a record variable is one chunk per record (§3.2
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#32-chunks>).

use crate::error::Error;

use super::{contiguous, key, Copied, CopiedChunk};
use crate::ir::Ir;
use crate::netcdf3::{named, Ty};
use crate::out::{bytes_codec, copy_threshold, declare, declared_group, grid_chunks, Chunk, Out, Part};
use crate::refs::Refs;
use serde_json::{json, Map, Value as J};
use std::collections::{BTreeMap, HashSet};

const PROFILE: &str = "netcdf3";
/// The chunk limit of a fixed-size variable (spec/zarrify/netcdf3.md §3.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#32-chunks>).
const CHUNK_BYTES: u64 = 1 << 20;

/// The names a variable may not take, and the attribute names the hierarchy writes itself.
const RESERVED: &[&str] = &["refkv_source"];
const RESERVED_ATTRIBUTES: &[&str] = &["zarr_conventions", "refkv_zarrified"];

fn n(v: &J) -> u64 {
    v.as_u64().unwrap_or(0)
}

fn list(v: &J) -> &[J] {
    v.as_array().map_or(&[], |a| a.as_slice())
}

fn shape_of(v: &J) -> Vec<u64> {
    list(v).iter().map(n).collect()
}

/// A text value of the description (conventions §6
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#6-source-metadata-as-json>) as
/// text: its UTF-8, or its ISO 8859-1 characters.
fn text(v: &J) -> String {
    v.as_str().or_else(|| v["latin1"].as_str()).unwrap_or("").to_string()
}

/// The bytes of a description's hexadecimal (`hdf5::file::hex`, which the parser writes it with).
fn unhex(v: &J) -> Vec<u8> {
    crate::hdf5::file::unhex(v.as_str().unwrap_or(""))
}

/// The type named by `v` (`"NC_FLOAT"` and so on).
fn type_of(v: &J) -> Option<Ty> {
    v.as_str().and_then(named)
}

/// A type's Zarr data type (spec/zarrify/netcdf3.md §3.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#31-data-types>).
fn data_type(t: &Ty) -> J {
    let name = match t.name {
        "NC_BYTE" => "int8",
        "NC_SHORT" => "int16",
        "NC_INT" => "int32",
        "NC_FLOAT" => "float32",
        "NC_DOUBLE" => "float64",
        "NC_UBYTE" => "uint8",
        "NC_USHORT" => "uint16",
        "NC_UINT" => "uint32",
        "NC_INT64" => "int64",
        "NC_UINT64" => "uint64",
        _ => return json!({"name": "null_terminated_bytes", "configuration": {"length_bytes": 1}}),
    };
    json!(name)
}

/// A type's default fill value, netCDF-C's `NC_FILL_*`, as Zarr writes it (spec/zarrify/netcdf3.md
/// §3.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#31-data-types>).
fn default_fill(t: &Ty) -> J {
    // its bits as the type's big-endian bytes
    let bits: u64 = match t.name {
        "NC_BYTE" => 0x81,
        "NC_SHORT" => 0x8001,
        "NC_INT" => 0x8000_0001,
        // 15 × 2^119, 9.9692099683868690e36, exact in both
        "NC_FLOAT" => 0x7CF0_0000,
        "NC_DOUBLE" => 0x479E_0000_0000_0000,
        "NC_UBYTE" => 0xFF,
        "NC_USHORT" => 0xFFFF,
        "NC_UINT" => 0xFFFF_FFFF,
        "NC_INT64" => 0x8000_0000_0000_0002,
        "NC_UINT64" => 0xFFFF_FFFF_FFFF_FFFE,
        _ => return json!(""),
    };
    t.value(&bits.to_be_bytes()[8 - t.bytes as usize..])
}

/// Why a name is not a node's (spec/zarrify/netcdf3.md §2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#2-the-hierarchy>), or the
/// name; `name` a text value of the description, which is UTF-8 when it is a string.
fn node_name(name: &J) -> Result<String, &'static str> {
    let s = name.as_str().ok_or("name")?;
    if s.is_empty() || s == "." || s == ".." || s == "zarr.json" || s.starts_with("__") || s.contains('/') {
        return Err("name");
    }
    if RESERVED.contains(&s) {
        return Err("reserved");
    }
    Ok(s.to_string())
}

/// A variable's `_FillValue` in the form xarray's Zarr backend decodes for the variable's data type
/// (spec/zarrify/netcdf3.md §5
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#5-attributes>): of a float
/// variable, the base64 of the value as a little-endian binary64; of an integer variable, the
/// integer; None for any other, which xarray's backend refuses.
fn fill_attribute(a: &J, var: &Ty) -> Option<J> {
    let t = type_of(&a["type"]).filter(|t| n(&a["count"]) == 1 && t.name != "NC_CHAR" && var.name != "NC_CHAR")?;
    let raw = unhex(&a["data"]);
    let int = |signed| crate::hdf5::types::int_of(&raw, true, signed);
    let x = if t.float {
        if t.bytes == 4 { f32::from_bits(int(false) as u32) as f64 } else { f64::from_bits(int(false) as u64) }
    } else if var.float {
        int(t.signed) as f64
    } else {
        return Some(crate::out::int_json(int(t.signed)));
    };
    if var.float {
        return Some(json!(crate::mirror::base64(&x.to_le_bytes())));
    }
    // a float `_FillValue` of an integer variable: the integer it is, where it is one
    (x.is_finite() && x.fract() == 0.0 && x.abs() < 2f64.powi(63)).then(|| crate::out::int_json(x as i128))
}

/// A node's attributes as presented, each one's type, and the reasons for those not presented
/// (spec/zarrify/netcdf3.md §5
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#5-attributes>). `var`: of a
/// variable, its type, for `_FillValue`'s form.
fn presented_attributes(attrs: &[J], var: Option<&Ty>) -> (Map<String, J>, Map<String, J>, Map<String, J>) {
    let (mut presented, mut types, mut not) = (Map::new(), Map::new(), Map::new());
    for a in attrs {
        let name = text(&a["name"]);
        if types.contains_key(&name) {
            continue;
        }
        types.insert(name.clone(), json!({"type": a["type"], "count": a["count"]}));
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
            _ => a["value"].clone(),
        };
        presented.insert(name, v);
    }
    (presented, types, not)
}

/// The fill value of a variable of type `t` with attributes `attrs` (spec/zarrify/netcdf3.md §3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#3-variables>): its first
/// `_FillValue`, when that is one value of its type, else the type's default.
fn fill_of(t: &Ty, attrs: &[J]) -> J {
    let Some(a) = attrs.iter().find(|a| a["name"] == "_FillValue") else { return default_fill(t) };
    if a["type"] != t.name || n(&a["count"]) != 1 {
        return default_fill(t);
    }
    let raw = unhex(&a["data"]);
    if t.name == "NC_CHAR" {
        if raw.first() == Some(&0) { json!("") } else { json!(crate::mirror::base64(&raw)) }
    } else {
        t.value(&raw)
    }
}

/// The hierarchy's nodes, the root's first, then a variable's array each, in the header's order:
/// each node's `path`, `attributes` as presented and `own`, its source metadata, and an array's
/// `array`, its `shape`, `data_type`, `fill_value`, `codecs`, `dimension_names` and `store`, where
/// its chunks are; and the summary, but for the chunks and the rounds.
fn nodes(description: &J) -> crate::error::Res<(Vec<J>, J)> {
    let dims = list(&description["dimensions"]);
    let (records, recsize) = (n(&description["records"]), n(&description["recsize"]));
    let mut arrays = Vec::new();
    let mut unpresented = Vec::new();
    let mut reasons: BTreeMap<&str, u64> = BTreeMap::new();
    let mut names = HashSet::new();
    for (k, v) in list(&description["variables"]).iter().enumerate() {
        let t = type_of(&v["type"]).ok_or_else(|| Error::internal(format!("variable {k} has no type")))?;
        let name = node_name(&v["name"]).and_then(|n| if names.insert(n.clone()) { Ok(n) } else { Err("duplicate") });
        let name = match name {
            Ok(n) => n,
            Err(why) => {
                unpresented.push(json!({"name": v["name"], "index": k, "reason": why}));
                *reasons.entry(why).or_default() += 1;
                continue;
            }
        };
        let attrs = list(&v["attributes"]);
        let (attributes, types, not) = presented_attributes(attrs, Some(&t));
        let record = v["record"].as_bool() == Some(true);
        let shape = shape_of(&v["shape"]);
        let mut own = Map::new();
        own.insert("index".into(), json!(k));
        own.insert("type".into(), json!(t.name));
        own.insert("record".into(), json!(record));
        own.insert("begin".into(), v["begin"].clone());
        own.insert("vsize".into(), v["vsize"].clone());
        if !types.is_empty() {
            own.insert("attributes".into(), J::Object(types));
        }
        if !not.is_empty() {
            own.insert("unpresented_attributes".into(), J::Object(not));
        }
        let begin = n(&v["begin"]);
        let store = if record {
            // a record's values of it, its slice (spec/zarrify/netcdf3.md §7.2
            // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#72-reading>)
            let slice = shape.iter().skip(1).try_fold(t.bytes, |a, &d| a.checked_mul(d)).ok_or_else(|| Error::internal("a slice past 2^64 bytes"))?;
            json!({"kind": "records", "offset": begin, "stride": recsize, "length": slice, "records": records})
        } else {
            json!({"kind": "fixed", "offset": begin, "item": t.bytes})
        };
        let dimension_names: Vec<String> = list(&v["dimensions"]).iter().map(|d| dims.get(n(d) as usize).map_or(String::new(), |d| text(&d["name"]))).collect();
        arrays.push(json!({
            "path": name, "attributes": attributes, "own": own,
            "array": {
                "shape": shape, "data_type": data_type(&t), "fill_value": fill_of(&t, attrs),
                "codecs": [bytes_codec(t.bytes, "big")], "dimension_names": dimension_names, "store": store,
            },
        }));
    }
    let format = match n(&description["version"]) {
        1 => "classic",
        2 => "64-bit offset",
        _ => "64-bit data",
    };
    let (attributes, types, not) = presented_attributes(list(&description["attributes"]), None);
    let dimensions: Vec<J> = dims.iter().map(|d| {
        let mut o = json!({"name": d["name"], "length": d["length"]});
        if n(&d["length"]) == 0 {
            o["unlimited"] = json!(true);
        }
        o
    }).collect();
    let mut own = Map::new();
    own.insert("format".into(), json!(format));
    own.insert("version".into(), description["version"].clone());
    own.insert("numrecs".into(), if description["numrecs"].is_null() { json!("streaming") } else { description["numrecs"].clone() });
    own.insert("records".into(), json!(records));
    own.insert("recsize".into(), json!(recsize));
    own.insert("header".into(), description["header"].clone());
    own.insert("dimensions".into(), J::Array(dimensions));
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
        "format": format, "dimensions": dims.len(), "variables": arrays.len(), "unpresented": reasons,
        "records": records, "header": description["header"],
    });
    let mut nodes = vec![json!({"path": "", "attributes": attributes, "own": own})];
    nodes.extend(arrays);
    Ok((nodes, summary))
}

/// A chunk shape, and each chunk with its grid coordinates.
type Cut = (Vec<u64>, Vec<(Vec<u64>, Chunk)>);

/// The contiguous cut of a fixed-size variable's array; None for a record variable's, or the root.
fn cut(node: &J, threshold: usize) -> crate::error::Res<Option<Cut>> {
    let a = &node["array"];
    let store = &a["store"];
    if store["kind"] != "fixed" {
        return Ok(None);
    }
    grid_chunks(n(&store["offset"]), &shape_of(&a["shape"]), n(&store["item"]), CHUNK_BYTES, threshold)
        .map(Some)
        .ok_or_else(|| Error::internal("the data cannot be cut into chunks"))
}

/// The prefix of the chunk keys of the array at `path` of rank `rank`: `<path>/c` alone for a
/// scalar.
fn prefix(path: &str, rank: usize) -> String {
    if rank == 0 { format!("{path}/c") } else { format!("{path}/c/") }
}

/// The source's ranges of the edge chunks the projection copies.
pub fn reads(_ir: &Ir, description: &J) -> crate::error::Res<Vec<(u64, u64)>> {
    let threshold = copy_threshold();
    let mut out = Vec::new();
    for node in nodes(description)?.0 {
        if let Some((_, chunks)) = cut(&node, threshold)? {
            out.extend(contiguous::copy_reads(&chunks));
        }
    }
    Ok(out)
}

pub fn project(ir: &Ir, description: &J, url: &str, copied: &Copied) -> crate::error::Res<Out> {
    let (nodes, summary) = nodes(description)?;
    build(ir, &nodes, summary, url, copied, copy_threshold(), None)
}

/// The netCDF3 convention's hierarchy of a file's description (spec/zarrify/netcdf3.md §7.7
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#77-the-description>): its
/// nodes (`nodes`), and its documents and chunks as the projection writes them, but for the edge
/// chunks it copies, which it gives apart, as ranges to copy: it reads nothing. The routine a
/// layout calls for the convention's hierarchy (`refkv.build.netcdf3_hierarchy`).
pub fn hierarchy(ir: &Ir, description: &J) -> crate::error::Res<(Vec<J>, Out, Vec<CopiedChunk>)> {
    let (nodes, summary) = nodes(description)?;
    let mut copies = Vec::new();
    let out = build(ir, &nodes, summary, "", &Copied::new(), copy_threshold(), Some(&mut copies))?;
    Ok((nodes, out, copies))
}

/// The output of `nodes`; `defer`: the edge chunks to copy are given there, not written.
fn build(ir: &Ir, nodes: &[J], mut summary: J, url: &str, copied: &Copied, threshold: usize,
         mut defer: Option<&mut Vec<CopiedChunk>>) -> crate::error::Res<Out> {
    let mut out = Out::new(url);
    let mut cuts = Vec::with_capacity(nodes.len());
    for node in nodes {
        cuts.push(cut(node, threshold)?);
    }
    for (node, c) in nodes.iter().zip(&cuts) {
        let path = node["path"].as_str().unwrap_or("");
        let attributes: Map<String, J> = node["attributes"].as_object().cloned().unwrap_or_default();
        let a = &node["array"];
        if a.is_null() {
            out.json("zarr.json", &declared_group(attributes, PROFILE, Some(url), Some(node["own"].clone())));
            continue;
        }
        let shape = shape_of(&a["shape"]);
        let chunk_shape: Vec<u64> = match c {
            Some((grid, _)) => grid.clone(),
            // one record a chunk, every other axis whole
            None => shape.iter().enumerate().map(|(k, &v)| if k == 0 { 1 } else { v.max(1) }).collect(),
        };
        let doc = json!({
            "zarr_format": 3,
            "node_type": "array",
            "shape": shape,
            "data_type": a["data_type"],
            "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": chunk_shape}},
            "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
            "fill_value": a["fill_value"],
            "codecs": a["codecs"],
            "attributes": declare(attributes, PROFILE, None, Some(node["own"].clone())),
            "dimension_names": a["dimension_names"],
        });
        out.json(&format!("{path}/zarr.json"), &doc);
    }
    let mut zeros = contiguous::Zeros::of(cuts.iter().flatten().flat_map(|c| &c.1));
    let mut refs = Refs::new(ir, out);
    let mut count = 0u64;
    for (node, c) in nodes.iter().zip(cuts) {
        let a = &node["array"];
        if a.is_null() {
            continue;
        }
        let path = node["path"].as_str().unwrap_or("");
        let shape = shape_of(&a["shape"]);
        let p = prefix(path, shape.len());
        match c {
            Some((_, mut chunks)) => {
                if let Some(d) = defer.as_deref_mut() {
                    chunks.retain(|(c, chunk)| match chunk {
                        Chunk::Copy(o, len, pad) => {
                            d.push((key(&p, c.iter().copied()), *o, *len, *pad));
                            false
                        }
                        _ => true,
                    });
                }
                count += chunks.len() as u64;
                contiguous::emit(&mut refs, chunks, (0, 0, 0), copied, &[&p], |c| c, &mut zeros)?;
            }
            None => {
                let store = &a["store"];
                let (offset, stride, length) = (n(&store["offset"]), n(&store["stride"]), n(&store["length"]));
                for r in 0..n(&store["records"]) {
                    let at = r.checked_mul(stride).and_then(|o| o.checked_add(offset)).ok_or_else(|| Error::internal("a record past 2^64 bytes"))?;
                    let coords = std::iter::once(r).chain(shape.iter().skip(1).map(|_| 0));
                    refs.chunk(key(&p, coords), 0, 0, 0, Some(vec![Part::Src(at, length)]))?;
                    count += 1;
                }
            }
        }
    }
    let mut out = refs.out;
    summary["chunk_entries"] = json!(count);
    out.summary = summary;
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The default fill values are netCDF-C's `NC_FILL_*`, as Zarr writes them.
    #[test]
    fn default_fills_are_netcdfs() {
        let names = ["NC_BYTE", "NC_CHAR", "NC_SHORT", "NC_INT", "NC_FLOAT", "NC_DOUBLE", "NC_UBYTE", "NC_USHORT", "NC_UINT", "NC_INT64", "NC_UINT64"];
        let fills: Vec<J> = names.iter().map(|t| default_fill(&named(t).unwrap())).collect();
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
        assert_eq!(node_name(&json!("temp")), Ok("temp".into()));
        assert_eq!(node_name(&json!("zarr.json.x")), Ok("zarr.json.x".into()));
        let names = [json!(""), json!("."), json!(".."), json!("__x"), json!("a/b"), json!("zarr.json"), json!({"latin1": "ÿ"})];
        for name in names {
            assert_eq!(node_name(&name), Err("name"), "{name}");
        }
        assert_eq!(node_name(&json!("refkv_source")), Err("reserved"));
    }
}
