//! The Zarr v3 projection (spec/zarrify/zarr3.md
//! §2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr3.md#2-nodes>–§5
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr3.md#5-other-objects>, the
//! profile's §7.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr3.md#73-chunks-and-other-objects>):
//! a valid IR and the facts of `zarr3.rs` into the hierarchy, of either form. A `zarr.json` per
//! node, the store's own with the convention declared on the root and on every node with source
//! metadata; an entry per present chunk referencing its whole object (a shard whole, for a
//! sharded array); and, in the store form, every other object of a length referenced whole
//! under `refkv_source/objects/`. In the zip form a chunk is its entry's data in the zip file,
//! and the other objects are the mirror's, which rebuilds the zip file. The projection reads
//! no object.

use crate::ir::Ir;
use crate::json::{self, Jv};
use crate::out::{convention, Out, Part, CONVENTION_KEY};
use super::store::{check_key, declare, object_key};
use super::zarr2;
use serde_json::{json, Value as J};
use std::collections::{HashMap, HashSet};

const PROFILE: &str = "zarr3";

/// Is `rest` (a key below an array) one of the array's chunk keys (spec/zarrify/zarr3.md
/// §3.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr3.md#31-chunk-grids-and-chunk-keys>)?
/// `v2` for the `v2` chunk key encoding (Zarr v2's keys), else the `default` one (`c`, then
/// the indexes, each after the separator).
pub fn is_chunk(rest: &str, v2: bool, sep: &str, counts: &[u64]) -> bool {
    if v2 {
        return zarr2::is_chunk(rest, counts, sep);
    }
    if counts.is_empty() {
        return rest == "c";
    }
    rest.strip_prefix('c').and_then(|r| r.strip_prefix(sep)).is_some_and(|r| zarr2::is_chunk(r, counts, sep))
}

/// A node's attributes: those kept in place, then the declaration, when the node is the root
/// (`url`) or has source metadata `s` (conventions
/// §2 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#2-attributes>), whose
/// `zarr_conventions` is the convention's CMO followed by the node's own, if it has them in place.
fn attributes(mut kept: Jv, url: Option<&str>, s: Option<Jv>) -> Jv {
    let declaration = declare(PROFILE, url, s);
    if declaration.is_empty_object() {
        return kept;
    }
    let mut cmos = vec![Jv::from_j(&convention(PROFILE))];
    if let Some(Jv::Arr(own)) = kept.get("zarr_conventions") {
        cmos.extend(own.iter().cloned());
    }
    kept.set("zarr_conventions", Jv::Arr(cmos));
    if let Some(p) = declaration.get(CONVENTION_KEY) {
        kept.set(CONVENTION_KEY, p.clone());
    }
    kept
}

fn doc_key(path: &str) -> String {
    if path.is_empty() { "zarr.json".into() } else { format!("{path}/zarr.json") }
}

/// An array's chunk keys, from its facts.
struct Keys {
    v2: bool,
    sep: String,
    counts: Vec<u64>,
}

/// The hierarchy of a Zarr v3 store, listed (`form` "store") or zipped (`form` "zip").
pub fn project(ir: &Ir, facts: &J, url: &str) -> Result<Out, String> {
    let zip = facts["form"] == "zip";
    let mut out = Out::new(url);
    let nodes = facts["nodes"].as_array().cloned().unwrap_or_default();
    let documents: HashSet<&str> = facts["documents"].as_array().into_iter().flatten().filter_map(J::as_str).collect();
    let mut arrays: HashMap<String, Keys> = HashMap::new();
    let (mut groups, mut narrays, mut sharded) = (0u64, 0u64, 0u64);
    for n in &nodes {
        let path = n["path"].as_str().unwrap_or("");
        let s = match n["s"].as_str() {
            // S nests a document's attributes one level deeper than the document
            Some(t) => Some(json::parse_within(t.as_bytes(), json::MAX_DEPTH + 1)?.value),
            None => None,
        };
        let url = path.is_empty().then_some(url);
        let doc = match n["kind"].as_str() {
            Some(kind @ ("array" | "group")) => {
                let mut d = json::parse(n["doc"].as_str().unwrap_or("").as_bytes())?.value;
                let kept = d.get("attributes").cloned().unwrap_or(Jv::obj(vec![]));
                d.set("attributes", attributes(kept, url, s));
                if kind == "array" {
                    narrays += 1;
                    sharded += u64::from(n["sharded"].as_bool().unwrap_or(false));
                    let g = &n["grid"];
                    let counts = g["counts"].as_array().into_iter().flatten().filter_map(J::as_u64).collect();
                    arrays.insert(path.to_string(), Keys { v2: g["v2"].as_bool().unwrap_or(false), sep: g["sep"].as_str().unwrap_or("/").to_string(), counts });
                } else {
                    groups += 1;
                }
                d
            }
            // an implicit group
            _ => {
                groups += 1;
                Jv::obj(vec![("zarr_format", Jv::Int("3".into())), ("node_type", Jv::str("group")), ("attributes", attributes(Jv::obj(vec![]), url, s))])
            }
        };
        let key = doc_key(path);
        check_key(&key)?;
        out.bytes(&key, doc.text().into_bytes());
    }
    // the array a key is below (the nearest), and whether the key is one of its chunk keys
    let chunk = |key: &str| -> bool {
        let mut owner = arrays.get("").map(|k| (key, k));
        for (at, _) in key.match_indices('/') {
            if let Some(k) = arrays.get(&key[..at]) {
                owner = Some((&key[at + 1..], k));
            }
        }
        owner.is_some_and(|(rest, k)| is_chunk(rest, k.v2, &k.sep, &k.counts))
    };
    let (mut chunks, mut empty_chunks, mut others) = (0u64, 0u64, 0u64);
    if zip {
        // the stored objects but the documents held: (key, data start, size)
        for o in facts["objects"].as_array().into_iter().flatten() {
            let (key, ds, us) = (o[0].as_str().unwrap_or(""), o[1].as_u64().unwrap_or(0), o[2].as_u64().unwrap_or(0));
            if !chunk(key) {
                continue;
            }
            if us == 0 {
                empty_chunks += 1;
                continue;
            }
            chunks += 1;
            check_key(key)?;
            out.refs(key, vec![Part::Src(ds, us)]);
        }
    } else {
        // the objects, in id order: chunks at their keys, the other objects of a length under
        // refkv_source/objects/
        for o in ir.sources().objects {
            let key = ir.key(o);
            let n = ir.len[o as usize];
            if documents.contains(key.as_str()) {
                continue;
            }
            if chunk(&key) {
                if n == 0 {
                    empty_chunks += 1;
                    continue;
                }
                chunks += 1;
                check_key(&key)?;
                out.refs(&key, vec![Part::Obj(key.clone(), 0, n)]);
            } else if n > 0 {
                others += 1;
                let k = object_key(&key);
                check_key(&k)?;
                out.refs(&k, vec![Part::Obj(key.clone(), 0, n)]);
            }
        }
    }
    out.summary = json!({"groups": groups, "arrays": narrays, "shardedArrays": sharded, "chunks": chunks, "emptyChunks": empty_chunks});
    if zip {
        out.summary["form"] = json!("zip");
        for (k, v) in facts["counts"].as_object().into_iter().flatten() {
            out.summary[k] = v.clone();
        }
    } else {
        out.summary["objects"] = json!(ir.objects);
        out.summary["otherObjects"] = json!(others);
    }
    if !facts["notPresented"].is_null() {
        out.summary["notPresented"] = facts["notPresented"].clone();
    }
    Ok(out)
}

/// The projection of the profile's store run (`StoreFormat`; crate::store).
pub fn store_project(ir: &Ir, facts: &J, url: &str) -> Result<Out, String> {
    project(ir, facts, url)
}
