//! The Zarr v2 projection (spec/zarrify/zarr2.md
//! §2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#2-nodes>,
//! §3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#3-arrays>,
//! §5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#5-other-objects>,
//! §7.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#72-chunks-and-other-objects>):
//! a valid store IR and the facts of `zarr2.rs` into the hierarchy. A `zarr.json` per node (the
//! convention declared on the root and on every node with source metadata), an entry per
//! present chunk referencing its whole object, and every other object of a length referenced
//! whole under `refkv_source/objects/`; the ignored keys the host recorded are the member
//! `ignored` of `refkv_source`'s source metadata, beside the mirror's `ir` ([`super::store::record_ignored`]).
//! The IR's objects are the listing; the projection reads no object.

use crate::ir::Ir;
use crate::json::{self, Jv};
use crate::out::{Out, Part};
use super::store::{canonical_index, check_key, declare, object_key};
use serde_json::{json, Value as J};
use std::collections::{HashMap, HashSet};

fn group(attributes: Jv) -> Jv {
    Jv::obj(vec![("zarr_format", Jv::Int("3".into())), ("node_type", Jv::str("group")), ("attributes", attributes)])
}

fn doc_key(path: &str) -> String {
    if path.is_empty() { "zarr.json".into() } else { format!("{path}/zarr.json") }
}

/// Is `rest` (a key below an array) one of the array's chunk keys (spec/zarrify/zarr2.md
/// §3.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#32-chunks>)?
pub fn is_chunk(rest: &str, grid: &[u64], sep: &str) -> bool {
    if grid.is_empty() {
        return rest == "0";
    }
    if sep == "." && rest.contains('/') {
        return false;
    }
    let parts: Vec<&str> = rest.split(sep).collect();
    parts.len() == grid.len() && parts.iter().zip(grid).all(|(s, &n)| canonical_index(s, n))
}

/// The hierarchy of a Zarr v2 store, or, when the facts say the root declares OME-NGFF 0.4
/// (`profile` "ome-zarr"), of an OME-Zarr one (spec/zarrify/ome-zarr.md
/// §5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ome-zarr.md#5-group-attributes>–§8
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ome-zarr.md#8-source-metadata>): its
/// groups' `ome`, its levels' `dimension_names` and its OME-XML at its own key, and the
/// OME-Zarr convention declared in place of the Zarr v2 one.
pub fn project(ir: &Ir, facts: &J, url: &str) -> Result<Out, String> {
    let profile = facts["profile"].as_str().unwrap_or("zarr2");
    let mut out = Out::new(url);
    let xml: HashSet<&str> = facts["xml"].as_array().into_iter().flatten().filter_map(|x| x.as_str()).collect();
    let nodes = facts["nodes"].as_array().cloned().unwrap_or_default();
    let documents: HashSet<&str> = facts["documents"].as_array().into_iter().flatten().filter_map(|x| x.as_str()).collect();
    let mut arrays: HashMap<String, (Vec<u64>, String)> = HashMap::new();
    let (mut groups, mut narrays) = (0u64, 0u64);
    for n in &nodes {
        let path = n["path"].as_str().unwrap_or("");
        let s = match n["s"].as_str() {
            // S nests a document one level deeper than the document
            Some(t) => Some(json::parse_within(t.as_bytes(), json::MAX_DEPTH + 1)?.value),
            None => None,
        };
        let mut attributes = declare(profile, path.is_empty().then_some(url), s);
        if let Some(o) = n["ome"].as_str() {
            // an OME group's `ome` comes first, then the convention
            let mut a = Jv::obj(vec![("ome", json::parse_within(o.as_bytes(), json::MAX_DEPTH + 1)?.value)]);
            for (k, v) in attributes.members() {
                a.set(&k.text, v.clone());
            }
            attributes = a;
        }
        let doc = match n["kind"].as_str() {
            Some("array") => {
                narrays += 1;
                let grid = n["grid"].as_array().into_iter().flatten().filter_map(|x| x.as_u64()).collect();
                arrays.insert(path.to_string(), (grid, n["sep"].as_str().unwrap_or(".").to_string()));
                let mut d = json::parse(n["zarr"].as_str().unwrap_or("").as_bytes())?.value;
                if let Some(names) = n["dims"].as_str() {
                    d.set("dimension_names", json::parse(names.as_bytes())?.value);
                }
                d.set("attributes", attributes);
                d
            }
            _ => {
                groups += 1;
                group(attributes)
            }
        };
        let key = doc_key(path);
        check_key(&key)?;
        out.bytes(&key, doc.text().into_bytes());
    }
    // the objects, in id order: chunks at their keys, the other objects of a length under
    // refkv_source/objects/
    let (mut chunks, mut empty_chunks, mut others) = (0u64, 0u64, 0u64);
    for o in ir.sources().objects {
        let ou = o as usize;
        let key = ir.key(o);
        let n = ir.len[ou];
        if documents.contains(key.as_str()) {
            continue;
        }
        // the array that is the key's nearest proper ancestor, if any
        let mut owner = None;
        if let Some((g, sep)) = arrays.get("") {
            owner = Some((key.as_str(), g, sep));
        }
        for (at, _) in key.match_indices('/') {
            if let Some((g, sep)) = arrays.get(&key[..at]) {
                owner = Some((&key[at + 1..], g, sep));
            }
        }
        let chunk = owner.is_some_and(|(rest, g, sep)| is_chunk(rest, g, sep));
        if xml.contains(key.as_str()) && !chunk {
            // an OME-XML object of a collection, at its own key (spec/zarrify/ome-zarr.md
            // §7 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ome-zarr.md#7-chunks-and-ome-xml>)
            if n > 0 {
                check_key(&key)?;
                out.refs(&key, vec![Part::Obj(key.clone(), 0, n)]);
            }
        } else if chunk {
            if n > 0 {
                chunks += 1;
                check_key(&key)?;
                out.refs(&key, vec![Part::Obj(key.clone(), 0, n)]);
            } else {
                empty_chunks += 1;
            }
        } else if n > 0 {
            others += 1;
            let k = object_key(&key);
            check_key(&k)?;
            out.refs(&k, vec![Part::Obj(key.clone(), 0, n)]);
        }
    }
    out.summary = json!({"groups": groups, "arrays": narrays, "chunks": chunks, "emptyChunks": empty_chunks,
                         "objects": ir.objects, "otherObjects": others});
    for (k, v) in facts["ome"].as_object().into_iter().flatten() {
        out.summary[k] = v.clone();
    }
    Ok(out)
}

