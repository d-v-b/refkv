//! The Zarr v2 convention's decisions (spec/zarrify/zarr2.md §3
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#3-arrays>,
//! §4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#4-source-metadata>), made
//! from the parser's description of the store (§7.3
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#73-the-description>): which
//! nodes it presents and why the others are not (§3.4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#34-arrays-that-are-not-presented>),
//! each array's `zarr.json` (its data type, codecs, fill value and chunk grid), each node's source
//! metadata `S`, and, for a store whose root declares OME-NGFF 0.4, the OME layer's
//! (`ome_zarr.rs`). [`hierarchy`] gives them as the projection (`zarr2.rs`) writes them. A check
//! a node fails rejects the store where the convention says: at the root, or at any group.

use crate::error::Error;

use crate::ir::{Ir, Res};
use crate::json::{self, Jv};
use crate::npy::header::{self, Dtype, Lit};
use crate::rules::{self, Vars};
use crate::expr::V;
use crate::zarr2::{base64, descr, schema};
use serde_json::{json, Value as J};
use std::collections::BTreeMap;

const MAX_SAFE: f64 = 9007199254740991.0;

/// A document of the description, parsed again: Ok(None) where the store has none, the reason
/// where it is not read or does not parse.
fn doc_of(d: &J) -> Res<Option<Jv>> {
    if d.is_null() {
        return Ok(None);
    }
    if let Some(why) = d["unread"].as_str().or(d["error"].as_str()) {
        return Err(Error::rejected(why));
    }
    let text = d["json"].as_str().ok_or_else(|| Error::internal("a document of the description has no JSON"))?;
    Ok(Some(json::parse(text.as_bytes())?.value))
}

/// Why a document is not read, of the description (a document that is empty or too large).
fn unread_of(d: &J) -> Option<String> {
    d["unread"].as_str().map(str::to_string)
}

/// The `.zattrs` check: an object.
fn check_zattrs(key: &str, a: &Jv) -> Res<()> {
    let mut env = Vars::default();
    env.insert("key", rules::text(key));
    env.insert("object", rules::flag(matches!(a, Jv::Obj(_))));
    schema().check("zattrs", &mut env)
}

/// An array node and its attributes, or the reason it is not presented (§3.4
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#34-arrays-that-are-not-presented>):
/// a document not read first, then one that does not parse, then the checks.
fn presented(shown: &str, node: &J) -> Res<(Array, Option<Jv>)> {
    let (d, a) = (&node["document"], &node["attributes"]);
    if let Some(why) = unread_of(d).or_else(|| unread_of(a)) {
        return Err(Error::Rejected(why));
    }
    let doc = doc_of(d)?.ok_or_else(|| Error::internal("a node without its document"))?;
    let attrs = doc_of(a)?;
    if let Some(v) = &attrs {
        check_zattrs(a["key"].as_str().unwrap_or(""), v)?;
    }
    Ok((array(shown, &doc)?, attrs))
}

/// The plan the hierarchy is written from (`project/zarr2.rs`), of the store's description:
/// each node presented, with its `zarr.json` and source metadata, the arrays not presented and
/// why, and the OME layer's, of a store whose root declares OME-NGFF 0.4.
pub fn hierarchy(ir: &Ir, description: &J) -> Res<J> {
    let s = schema();
    let ome = description["profile"] == "ome-zarr";
    // each node: its plan, its attributes (an explicit group's, for the OME layer) and M
    let mut found: Vec<(J, Option<Jv>, Jv)> = Vec::new();
    let mut arrays: BTreeMap<String, crate::ome_zarr::ArrayInfo> = BTreeMap::new();
    // the arrays left out, and why
    let mut left_out: BTreeMap<String, String> = BTreeMap::new();
    // the arrays that are not levels of an image (spec/zarrify/ome-zarr.md
    // §2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ome-zarr.md#2-hierarchy>)
    let mut no_level: BTreeMap<String, String> = BTreeMap::new();
    let mut implicit = Vec::new();
    for n in description["nodes"].as_array().into_iter().flatten() {
        let path = n["path"].as_str().unwrap_or("").to_string();
        let shown = if path.is_empty() { "/" } else { path.as_str() };
        match n["kind"].as_str() {
            Some("array") => match presented(shown, n) {
                Ok((a, attrs)) => {
                    let dims = a.grid.len();
                    let data_type = a.doc.get("data_type").and_then(|v| v.as_str()).unwrap_or("").to_string();
                    arrays.insert(path.clone(), crate::ome_zarr::ArrayInfo { dims, data_type });
                    if a.trailing {
                        no_level.insert(path.clone(), "it has a trailing axis of bytes".into());
                    }
                    let mut node = json!({"path": path, "kind": "array", "zarr": a.doc.text(), "sep": a.sep, "grid": a.grid});
                    if a.trailing {
                        node["trailing"] = json!(true);
                    }
                    found.push((node, attrs, a.meta));
                }
                Err(reason) if path.is_empty() => return Err(reason),
                Err(reason) => {
                    left_out.insert(path, reason.to_string());
                }
            },
            Some("group") => {
                let key = n["document"]["key"].as_str().unwrap_or("");
                let doc = doc_of(&n["document"])?.ok_or_else(|| Error::internal("a node without its document"))?;
                let attrs = doc_of(&n["attributes"])?;
                let mut env = Vars::default();
                env.insert("key", rules::text(key));
                env.insert("object", rules::flag(matches!(doc, Jv::Obj(_))));
                env.insert("zarr_format", given(&doc, "zarr_format"));
                s.check("zgroup", &mut env)?;
                let meta = Jv::Obj(doc.members().iter().filter(|(k, _)| k.text != "zarr_format").cloned().collect());
                if let Some(a) = &attrs {
                    check_zattrs(n["attributes"]["key"].as_str().unwrap_or(""), a)?;
                }
                found.push((json!({"path": path, "kind": "group"}), attrs, meta));
            }
            _ => implicit.push(path),
        }
    }
    // the OME layer (spec/zarrify/ome-zarr.md
    // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ome-zarr.md>): its checks, and what
    // it writes in place of the Zarr v2 convention's
    let mut not_ome: BTreeMap<String, String> = BTreeMap::new();
    let layer = if ome {
        let mut groups: BTreeMap<String, Jv> = found.iter().filter(|(n, _, _)| n["kind"] == "group")
            .map(|(n, a, _)| (n["path"].as_str().unwrap_or("").to_string(), a.clone().unwrap_or(Jv::Obj(Vec::new())))).collect();
        no_level.extend(left_out.iter().map(|(p, r)| (p.clone(), format!("it is not presented: {r}"))));
        not_ome = crate::ome_zarr::not_ome(&mut groups, &no_level);
        let keys: Vec<(String, u64)> = ir.sources().objects.iter().map(|&o| (ir.key(o), ir.len[o as usize])).collect();
        Some(crate::ome_zarr::layer(&groups, &arrays, &keys)?)
    } else {
        None
    };
    let mut nodes = Vec::new();
    for (mut node, a, meta) in found {
        let path = node["path"].as_str().unwrap_or("").to_string();
        let mut own = Jv::obj(vec![]);
        // a group that is not OME keeps its whole `.zattrs`, which the layer read without its
        // OME members
        let g = layer.as_ref().and_then(|l| l.groups.get(&path)).filter(|_| !not_ome.contains_key(&path));
        let attributes = match g {
            Some(g) => Some(g.attributes.clone()),
            None => a,
        };
        if let Some(a) = attributes.filter(|a| !a.is_empty_object()) {
            own.set("attributes", a);
        }
        if !meta.is_empty_object() {
            own.set("metadata", meta);
        }
        if path.is_empty() {
            // only a group has arrays below it, and a root array that is not presented rejects
            // the store
            for (member, record) in [("not_presented", &left_out), ("not_ome", &not_ome)] {
                if !record.is_empty() {
                    own.set(member, Jv::Arr(record.iter().map(|(p, r)| Jv::obj(vec![("path", Jv::str(p)), ("reason", Jv::str(r))])).collect()));
                }
            }
        }
        if let Some(g) = g {
            if !g.unversioned.is_empty() {
                own.set("unversioned", Jv::Arr(g.unversioned.iter().map(|u| Jv::str(u)).collect()));
            }
            if let Some(o) = &g.ome {
                node["ome"] = json!(o.text());
            }
        }
        if let Some(d) = layer.as_ref().and_then(|l| l.dims.get(&path)) {
            node["dims"] = json!(d.text());
        }
        if !own.is_empty_object() {
            node["s"] = json!(own.text());
        }
        nodes.push(node);
    }
    for p in implicit {
        nodes.push(json!({"path": p, "kind": "implicit"}));
    }
    let mut planned = json!({"nodes": nodes, "documents": description["documents"], "profile": description["profile"]});
    if !left_out.is_empty() {
        planned["notPresented"] = json!(left_out.len());
    }
    if !not_ome.is_empty() {
        planned["notOme"] = json!(not_ome.len());
    }
    if let Some(l) = layer {
        planned["xml"] = json!(l.xml);
        planned["ome"] = l.summary;
    }
    Ok(planned)
}

/// The given value of a member (absent when absent or null).
fn given(z: &Jv, k: &str) -> V {
    z.get(k).map(|v| v.to_v()).unwrap_or(V::Absent)
}

/// What the projection needs of an array's `.zarray`.
pub struct Array {
    /// its `zarr.json`, without its attributes
    pub doc: Jv,
    /// its dimension separator
    pub sep: String,
    /// its chunks along each of the dimensions of its `.zarray`
    pub grid: Vec<u64>,
    /// its metadata `M`
    pub meta: Jv,
    /// whether a `|S<n>` or `|V<n>` element's bytes are its last axis
    pub trailing: bool,
}

/// A `.zarray`'s data type (spec/zarrify/zarr2.md
/// §3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#3-arrays>): NPY's table
/// (spec/zarrify/npy.md §3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md#3-data-types>),
/// and `|O` when its one filter is `vlen-utf8`.
fn data_type(z: &Jv) -> crate::error::Res<Dtype> {
    let Some(d) = z.get("dtype") else { return Err(Error::rejected("it has none")) };
    if d.as_str() == Some("|O") {
        let vlen = matches!(z.get("filters"), Some(Jv::Arr(f)) if f.len() == 1 && f[0].get("id").and_then(|i| i.as_str()) == Some("vlen-utf8"));
        return if vlen {
            Ok(Dtype { zarr: json!("string"), item: 1, little: None, trailing: None, fill: json!("") })
        } else {
            Err(Error::rejected("an object array has no data type without the one filter vlen-utf8"))
        };
    }
    header::dtype(rules::of("npy"), &descr(d))
}

/// A float fill value as Zarr v3 writes it, given the type's size: a negative zero, which JSON
/// writers do not all keep, as the hex fill of its bits; an integer literal beyond 2^53 − 1 as
/// its binary64 value; any other as it is.
fn float_fill(v: &Jv, b: u64) -> Jv {
    match v {
        Jv::Int(d) => match d.parse::<f64>() {
            Ok(x) if x.abs() > MAX_SAFE => Jv::Float(x),
            _ => v.clone(),
        },
        Jv::Float(x) if *x == 0.0 && x.is_sign_negative() => Jv::str(match b { 2 => "0x8000", 4 => "0x80000000", _ => "0x8000000000000000" }),
        other => other.clone(),
    }
}

/// An integer literal beyond 2^53 − 1, which [`float_fill`] does not write as it is.
fn beyond_safe(v: &Jv) -> bool {
    matches!(v, Jv::Int(d) if d.parse::<f64>().is_ok_and(|x| x.abs() > MAX_SAFE))
}

/// Whether a part of a complex fill value is one a float of `b` bytes takes.
fn float_part(v: &Jv, b: u64) -> bool {
    match v {
        Jv::Str(s) => ["NaN", "Infinity", "-Infinity"].contains(&s.text.as_str()),
        Jv::Int(_) | Jv::Float(_) => b == 8 || v.num().is_some_and(|x| x.abs() <= 3.4028234663852886e38),
        _ => false,
    }
}

/// The fill value `F` of an array of a family the table of limits does not cover, and whether
/// `M` keeps the member as written (spec/zarrify/zarr2.md
/// §3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#3-arrays>,
/// §4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#4-source-metadata>); None
/// for a value the family does not take.
fn other_fill(family: &str, t: &Dtype, fv: Option<&Jv>) -> Option<(Jv, bool)> {
    let zero = Jv::from_j(&t.fill);
    let Some(v) = fv else { return Some((zero, false)) };
    let string = match v {
        Jv::Str(s) if s.lone.is_empty() => Some(s.text.as_str()),
        _ => None,
    };
    match family {
        "complex" => {
            let b = t.item / 2;
            match v {
                Jv::Arr(a) if a.len() == 2 && a.iter().all(|x| float_part(x, b)) => {
                    Some((Jv::Arr(a.iter().map(|x| float_fill(x, b)).collect()), a.iter().any(beyond_safe)))
                }
                _ if float_part(v, b) => Some((Jv::Arr(vec![float_fill(v, b), Jv::Int("0".into())]), true)),
                _ => None,
            }
        }
        "time" => match v {
            // NaT is the least int64, past what a JSON number holds exactly
            Jv::Int(d) => d.parse::<i64>().ok().map(|i| (Jv::Int(i.to_string()), false)),
            Jv::Float(_) => v.int(i64::MIN, i64::MAX).map(|i| (Jv::Int(i.to_string()), false)),
            _ if string == Some("NaT") => Some((v.clone(), false)),
            _ => None,
        },
        "text" => string.filter(|x| x.chars().count() as u64 * 4 <= t.item).map(|_| (v.clone(), false)),
        "bytes" => {
            let n = t.trailing.unwrap_or(0) as usize;
            let b = base64(string?)?;
            if b.len() > n {
                return None;
            }
            // shorter than the element, it ends in zeros, as NumPy pads it
            let first = if b.len() == n { *b.first()? } else { 0 };
            b.iter().all(|&x| x == first).then(|| (Jv::Int(first.to_string()), true))
        }
        "struct" => {
            let b = base64(string?)?;
            (b.len() as u64 <= t.item && b.iter().all(|&x| x == 0)).then_some((zero, true))
        }
        "string" => match v {
            _ if string.is_some() => Some((v.clone(), false)),
            // what zarr-python 2 writes for an object array, whose fill it does not use
            _ if v.int(0, 0).is_some() => Some((zero, true)),
            _ => None,
        },
        _ => None,
    }
}

/// An array's filters' codecs (spec/zarrify/zarr2.md
/// §3.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#33-filters>), and the
/// size and byte order of what their last gives the `bytes` codec.
fn filters(path: &str, z: &Jv, t: &Dtype) -> Res<(Vec<Jv>, u64, Option<bool>)> {
    let s = schema();
    let (mut size, mut little) = (if t.trailing.is_some() { 1 } else { t.item }, t.little);
    let given = z.get("filters").filter(|f| **f != Jv::Null);
    let list = given.is_none_or(|f| matches!(f, Jv::Arr(a) if a.iter().all(|x| x.get("id").is_some_and(|i| i.as_str().is_some()))));
    s.check("filters", &mut rules::env([("path", rules::text(path)), ("list", rules::flag(list))]))?;
    let mut before = z.get("dtype").and_then(|d| d.as_str()).unwrap_or("").to_string();
    let fs = match given {
        Some(Jv::Arr(a)) => a.as_slice(),
        _ => &[],
    };
    let mut codecs = Vec::new();
    for f in fs {
        let id = f.get("id").and_then(|i| i.as_str()).unwrap_or("");
        let table = s.table("filters");
        let takes: Vec<&str> = table[id].as_array().into_iter().flatten().filter_map(|x| x.as_str()).collect();
        let members = f.members().iter().all(|(k, _)| takes.contains(&k.text.as_str()));
        let (dtype, astype) = (f.get("dtype").and_then(|d| d.as_str()), f.get("astype").filter(|a| **a != Jv::Null).and_then(|a| a.as_str()));
        // the numeric types a filter takes: integers and floats, and for quantize floats alone
        let number = |text: Option<&str>| -> Option<Dtype> { header::dtype(rules::of("npy"), &Lit::Str(text?.to_string())).ok() };
        let is = |t: &Dtype, kinds: &[&str]| t.zarr.as_str().is_some_and(|n| kinds.iter().any(|k| n.starts_with(k)));
        let result = number(astype.or(dtype));
        let kinds: &[&str] = if id == "quantize" { &["float"] } else { &["int", "uint", "float"] };
        let dtype_kind = id == "vlen-utf8" || number(dtype).is_some_and(|t| is(&t, kinds));
        let astype_ok = id == "vlen-utf8" || (result.as_ref().is_some_and(|t| is(t, kinds)) && (id != "quantize" || astype.is_none() || astype == dtype));
        let finite = |k: &str| f.get(k).and_then(Jv::num).is_some_and(f64::is_finite);
        let numbers = match id {
            "fixedscaleoffset" => finite("scale") && finite("offset"),
            "quantize" => f.get("digits").and_then(|d| d.int(i64::MIN, i64::MAX)).is_some(),
            _ => true,
        };
        s.check("filter", &mut rules::env([
            ("path", rules::text(path)), ("id", rules::text(id)), ("members", rules::flag(members)),
            ("alone", rules::flag(id != "vlen-utf8" || (fs.len() == 1 && before == "|O"))), ("numbers", rules::flag(numbers)),
            ("dtype", rules::text(dtype.unwrap_or(""))), ("before", rules::text(&before)),
            ("dtype_ok", rules::flag(id == "vlen-utf8" || dtype == Some(before.as_str()))), ("dtype_kind", rules::flag(dtype_kind)),
            ("astype", rules::text(astype.unwrap_or(""))), ("astype_ok", rules::flag(astype_ok)),
        ]))?;
        let name = if id == "vlen-utf8" { id.to_string() } else { format!("numcodecs.{id}") };
        codecs.push(if id == "vlen-utf8" {
            Jv::obj(vec![("name", Jv::str(&name))])
        } else {
            Jv::obj(vec![("name", Jv::str(&name)), ("configuration", Jv::Obj(f.without(&["id"])))])
        });
        if let Some(r) = result.filter(|_| id != "vlen-utf8") {
            size = r.item;
            little = r.little;
            before = astype.or(dtype).unwrap_or("").to_string();
        }
    }
    Ok((codecs, size, little))
}

/// An array's `zarr.json` without its attributes, its separator, its chunk grid and its
/// metadata `M` (spec/zarrify/zarr2.md
/// §3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#3-arrays>,
/// §4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#4-source-metadata>).
pub fn array(path: &str, z: &Jv) -> Res<Array> {
    let s = schema();
    let t = data_type(z);
    let mut env = Vars::default();
    env.insert("path", rules::text(path));
    env.insert("object", rules::flag(matches!(z, Jv::Obj(_))));
    for k in ["zarr_format", "shape", "chunks", "dtype", "order", "dimension_separator"] {
        env.insert(k, given(z, k));
    }
    env.insert("dtype_problem", t.as_ref().err().map_or(V::Absent, |e| rules::text(&e.to_string())));
    s.check("zarray", &mut env)?;
    let t = t?;
    let trailing = t.trailing;
    let ints = |k: &str| -> Vec<i64> {
        match z.get(k) {
            Some(Jv::Arr(a)) => a.iter().map(|x| x.int(i64::MIN, i64::MAX).unwrap_or(0)).collect(),
            _ => Vec::new(),
        }
    };
    let (mut shape, mut chunks) = (ints("shape"), ints("chunks"));
    let n = shape.len();
    let sep = z.get("dimension_separator").and_then(|v| v.as_str()).unwrap_or(".").to_string();
    // the fill value F
    let fv = z.get("fill_value").filter(|v| **v != Jv::Null);
    let name = t.zarr.as_str().or(t.zarr["name"].as_str()).unwrap_or("").to_string();
    let family = match name.as_str() {
        _ if trailing.is_some() => "bytes",
        "bool" => "bool",
        "string" => "string",
        "struct" => "struct",
        "fixed_length_utf32" => "text",
        "numpy.datetime64" | "numpy.timedelta64" => "time",
        n if n.starts_with("float") => "float",
        n if n.starts_with("complex") => "complex",
        _ => "int",
    };
    let other = other_fill(family, &t, fv);
    let mut env = Vars::default();
    env.insert("path", rules::text(path));
    env.insert("fill", fv.map(|v| v.to_v()).unwrap_or(V::Absent));
    // what a message names: a trailing axis's array is a uint8 only to Zarr
    let dtype_text = z.get("dtype").and_then(|v| v.as_str()).unwrap_or("");
    env.insert("data_type", rules::text(if trailing.is_some() { dtype_text } else { &name }));
    env.insert("b", rules::num(t.item as f64));
    env.insert("family", rules::text(family));
    env.insert("ok", rules::flag(other.is_some()));
    s.check("fill", &mut env)?;
    let mut keep_fill = false;
    let fill = match (family, fv) {
        ("bool" | "int" | "float", None) if name == "bool" => Jv::Bool(false),
        ("bool" | "int" | "float", None) => Jv::Int("0".into()),
        ("bool", Some(v)) => v.clone(),
        ("int", Some(v)) => Jv::Int(v.int(i64::MIN, i64::MAX).unwrap_or(0).to_string()),
        ("float", Some(v)) => float_fill(v, t.item),
        _ => {
            let (f, keep) = got_fill(other)?;
            keep_fill = keep;
            f
        }
    };
    let (filter_codecs, size, little) = filters(path, z, &t)?;
    let mut codecs = Vec::new();
    if z.get("order").and_then(|v| v.as_str()) == Some("F") && n >= 2 {
        // an element's bytes stay the last axis
        let order = (0..n).rev().chain(trailing.map(|_| n)).map(|i| Jv::Int(i.to_string())).collect();
        codecs.push(Jv::obj(vec![("name", Jv::str("transpose")), ("configuration", Jv::obj(vec![("order", Jv::Arr(order))]))]));
    }
    let vlen = name == "string";
    codecs.extend(filter_codecs);
    if !vlen {
        codecs.push(match little {
            None => Jv::obj(vec![("name", Jv::str("bytes"))]),
            Some(l) => Jv::obj(vec![("name", Jv::str("bytes")), ("configuration", Jv::obj(vec![("endian", Jv::str(if l { "little" } else { "big" }))]))]),
        });
    }
    // numcodecs' automatic shuffle goes by the element, and blosc's typesize by the bytes it is given
    let (codec, extra) = compressor(path, z.get("compressor").filter(|v| **v != Jv::Null), size, if trailing.is_some() { t.item } else { size })?;
    codecs.extend(codec);
    // M (§4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#4-source-metadata>): the members §3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#3-arrays> does not
    // read, as written; null fill values; a float type's integer
    // fill beyond 2^53 − 1; a fill value `F` does not give back; a trailing axis's dtype; the
    // compressor's members its codec does not carry
    let read: Vec<&str> = s.table("zarray_read").as_array().into_iter().flatten().filter_map(|x| x.as_str()).collect();
    let mut meta = Jv::Obj(z.members().iter().filter(|(k, _)| !read.contains(&k.text.as_str())).cloned().collect());
    if trailing.is_some() {
        meta.set("dtype", Jv::str(dtype_text));
    }
    match fv {
        None => meta.set("fill_value", Jv::Null),
        Some(v) if keep_fill || (family == "float" && beyond_safe(v)) => meta.set("fill_value", v.clone()),
        _ => {}
    }
    if !extra.is_empty_object() {
        meta.set("compressor", extra);
    }
    let grid = shape.iter().zip(&chunks).map(|(&s, &c)| (s as u64).div_ceil(c.max(1) as u64)).collect();
    if let Some(e) = trailing {
        shape.push(e as i64);
        chunks.push(e as i64);
    }
    let as_ints = |v: &[i64]| Jv::Arr(v.iter().map(|x| Jv::Int(x.to_string())).collect());
    let doc = Jv::obj(vec![
        ("zarr_format", Jv::Int("3".into())),
        ("node_type", Jv::str("array")),
        ("shape", as_ints(&shape)),
        ("data_type", Jv::from_j(&t.zarr)),
        ("chunk_grid", Jv::obj(vec![("name", Jv::str("regular")), ("configuration", Jv::obj(vec![("chunk_shape", as_ints(&chunks))]))])),
        ("chunk_key_encoding", Jv::obj(vec![("name", Jv::str("v2")), ("configuration", Jv::obj(vec![("separator", Jv::str(&sep))]))])),
        ("fill_value", fill),
        ("codecs", Jv::Arr(codecs)),
    ]);
    Ok(Array { doc, sep, grid, meta, trailing: trailing.is_some() })
}

/// A fill value the `fill` role checked just before: its absence is the schema's and the code's
/// disagreeing, a rejection rather than a panic.
fn got_fill(v: Option<(Jv, bool)>) -> Res<(Jv, bool)> {
    v.ok_or_else(|| Error::internal("a fill value the schema checked is not there"))
}

/// The compressor's codec (spec/zarrify/zarr2.md
/// §3.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#31-compressors>), and
/// its members the codec does not carry. `b` is the size of what it compresses, `e` the size of
/// the element numcodecs' automatic shuffle went by.
fn compressor(path: &str, c: Option<&Jv>, b: u64, e: u64) -> Res<(Option<Jv>, Jv)> {
    let s = schema();
    let mut env = Vars::default();
    env.insert("path", rules::text(path));
    env.insert("compressor", c.map(|v| v.to_v()).unwrap_or(V::Absent));
    for (k, has) in [("level", "has_level"), ("checksum", "has_checksum"), ("blocksize", "has_blocksize"),
                     ("acceleration", "has_acceleration"), ("format", "has_format"), ("check", "has_check"), ("preset", "has_preset")] {
        env.insert(has, rules::flag(c.is_some_and(|c| c.get(k).is_some())));
    }
    s.check("compressor", &mut env)?;
    let Some(c) = c else { return Ok((None, Jv::obj(vec![]))) };
    let id = c.get("id").and_then(|v| v.as_str()).unwrap_or("");
    let table = &s.table("compressors")[id];
    let mut carried: Vec<&str> = table["carried"].as_array().into_iter().flatten().filter_map(|x| x.as_str()).collect();
    let level = || -> i64 {
        match c.get("level") {
            None => table["default"].as_i64().unwrap_or(0),
            Some(v) => match v.int(i64::MIN, i64::MAX).unwrap_or(0) {
                -1 if id != "zstd" => 6, // zlib's default level, which it stands for
                l => l,
            },
        }
    };
    let int = |l: i64| Jv::Int(l.to_string());
    let codec = match id {
        // zarr-python's name for numcodecs' Zlib, which a Zarr v2 `zlib` compressor is
        "zlib" => Jv::obj(vec![("name", Jv::str("numcodecs.zlib")), ("configuration", Jv::obj(vec![("level", int(level()))]))]),
        "gzip" => Jv::obj(vec![("name", Jv::str("gzip")), ("configuration", Jv::obj(vec![("level", int(level()))]))]),
        "bz2" => Jv::obj(vec![("name", Jv::str("numcodecs.bz2")), ("configuration", Jv::obj(vec![("level", int(level()))]))]),
        "lz4" => {
            let acceleration = c.get("acceleration").cloned().unwrap_or(int(1));
            Jv::obj(vec![("name", Jv::str("numcodecs.lz4")), ("configuration", Jv::obj(vec![("acceleration", acceleration)]))])
        }
        "lzma" => {
            let member = |k: &str, default: Jv| c.get(k).cloned().unwrap_or(default);
            Jv::obj(vec![("name", Jv::str("numcodecs.lzma")), ("configuration", Jv::obj(vec![
                ("format", member("format", int(1))),
                ("check", member("check", int(-1))),
                ("preset", member("preset", Jv::Null)),
                ("filters", member("filters", Jv::Null)),
            ]))])
        }
        "zstd" => {
            let checksum = c.get("checksum").cloned().unwrap_or(Jv::Bool(false));
            Jv::obj(vec![("name", Jv::str("zstd")), ("configuration", Jv::obj(vec![("level", int(level())), ("checksum", checksum)]))])
        }
        _ => {
            let shuffle = c.get("shuffle").and_then(|v| v.int(i64::MIN, i64::MAX)).unwrap_or(0);
            let h = match shuffle {
                0 => "noshuffle",
                1 => "shuffle",
                2 => "bitshuffle",
                _ if e == 1 => "bitshuffle",
                _ => "shuffle",
            };
            if shuffle == -1 {
                carried.retain(|k| *k != "shuffle"); // -1 (automatic) is resolved: keep it
            }
            let blocksize = c.get("blocksize").and_then(|v| v.int(i64::MIN, i64::MAX)).unwrap_or(0);
            Jv::obj(vec![("name", Jv::str("blosc")), ("configuration", Jv::obj(vec![
                ("cname", c.get("cname").cloned().unwrap_or(Jv::Null)),
                ("clevel", int(c.get("clevel").and_then(|v| v.int(0, 9)).unwrap_or(0))),
                ("shuffle", Jv::str(h)),
                ("typesize", int(b as i64)),
                ("blocksize", int(blocksize)),
            ]))])
        }
    };
    let extra = Jv::Obj(c.members().iter().filter(|(k, _)| !carried.contains(&k.text.as_str())).cloned().collect());
    Ok((Some(codec), extra))
}

