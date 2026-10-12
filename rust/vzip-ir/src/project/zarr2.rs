//! The Zarr v2 projection (spec/virtualize/zarr2.md
//! §2 <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/zarr2.md#2-nodes>,
//! §3 <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/zarr2.md#3-arrays>,
//! §5 <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/zarr2.md#5-other-objects>,
//! §10.2 <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/zarr2.md#102-chunks-and-other-objects>):
//! a valid store IR and the facts of `zarr2.rs` into the hierarchy. A `zarr.json` per node (the
//! convention declared on the root and on every node with source metadata), an entry per
//! present chunk referencing its whole object, and every other object of a length referenced
//! whole under `vzip_source/objects/`; the ignored keys the host recorded are the member
//! `ignored` of `vzip_source`'s source metadata, beside the mirror's `ir` ([`record_ignored`]).
//! The IR's objects are the listing; the projection reads no object.

use crate::ir::Ir;
use crate::json::{self, Jv};
use crate::out::{convention, root_property, Out, Part, CONVENTION_KEY};
use serde_json::{json, Value as J};
use std::collections::{HashMap, HashSet};

pub const OBJECTS: &str = "vzip_source/objects/";

/// The hierarchy's key of the other object `key`: under `vzip_source/objects/`, with `~`
/// appended to a last segment that is `zarr.json`, `.zarray` or `.zgroup` followed by any
/// number of `~`, so that no Zarr reader opens a node there (spec/virtualize/zarr2.md
/// §5 <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/zarr2.md#5-other-objects>).
pub fn object_key(key: &str) -> String {
    let last = key.rsplit('/').next().unwrap_or("");
    let base = last.trim_end_matches('~');
    let tilde = matches!(base, "zarr.json" | ".zarray" | ".zgroup");
    format!("{OBJECTS}{key}{}", if tilde { "~" } else { "" })
}

/// A node's attributes: the convention declared when the node is the root (`url`) or has
/// source metadata `s` (conventions
/// §2 <https://github.com/d-v-b/vzip/blob/main/spec/conventions.md#2-attributes>).
pub fn declare(profile: &str, url: Option<&str>, s: Option<Jv>) -> Jv {
    let mut value = match url {
        Some(u) => Jv::from_j(&J::Object(root_property(profile, u))),
        None => Jv::obj(vec![]),
    };
    if let Some(s) = s.filter(|s| !s.is_empty_object()) {
        value.set(profile, s);
    }
    if value.is_empty_object() {
        return value;
    }
    Jv::obj(vec![("zarr_conventions", Jv::Arr(vec![Jv::from_j(&convention(profile))])), (CONVENTION_KEY, value)])
}

fn group(attributes: Jv) -> Jv {
    Jv::obj(vec![("zarr_format", Jv::Int("3".into())), ("node_type", Jv::str("group")), ("attributes", attributes)])
}

fn doc_key(path: &str) -> String {
    if path.is_empty() { "zarr.json".into() } else { format!("{path}/zarr.json") }
}

/// Is `rest` (a key below an array) one of the array's chunk keys (spec/virtualize/zarr2.md
/// §3.2 <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/zarr2.md#32-chunks>)?
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

/// A decimal integer without leading zeros, below `limit`.
pub fn canonical_index(s: &str, limit: u64) -> bool {
    !s.is_empty() && s.len() <= 17 && s.bytes().all(|c| c.is_ascii_digit()) && (s == "0" || !s.starts_with('0'))
        && s.parse::<u64>().is_ok_and(|v| v < limit)
}

/// Rejects the output keys spec/archive.md does not allow.
fn check_key(key: &str) -> Result<(), String> {
    if key.len() > 65535 {
        return Err("an output key is longer than 65535 bytes".into());
    }
    if key.starts_with("__vz__/") {
        return Err(format!("output key {key:?} is in the reserved __vz__/ space"));
    }
    Ok(())
}

pub fn project(ir: &Ir, facts: &J, url: &str, profile: &str) -> Result<Out, String> {
    let mut out = Out::new(url);
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
        let attributes = declare(profile, path.is_empty().then_some(url), s);
        let doc = match n["kind"].as_str() {
            Some("array") => {
                narrays += 1;
                let grid = n["grid"].as_array().into_iter().flatten().filter_map(|x| x.as_u64()).collect();
                arrays.insert(path.to_string(), (grid, n["sep"].as_str().unwrap_or(".").to_string()));
                let mut d = json::parse(n["zarr"].as_str().unwrap_or("").as_bytes())?.value;
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
    // vzip_source/objects/
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
        if chunk {
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
    Ok(out)
}

/// After the mirror: the recorded ignored keys (spec/virtualize.md §1.4
/// <https://github.com/d-v-b/vzip/blob/main/spec/virtualize.md#14-store-inputs>), in the order of
/// their UTF-8 bytes, as the member `ignored` of `vzip_source`'s source metadata, beside the
/// mirror's `ir` (spec/virtualize/zarr2.md §5
/// <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/zarr2.md#5-other-objects>); the group
/// is made when there is no mirror (tests). Nothing when there are none.
pub fn record_ignored(out: &mut Out, facts: &J, profile: &str) -> Result<(), String> {
    let mut ignored: Vec<&str> = facts["ignored"].as_array().into_iter().flatten().filter_map(J::as_str).collect();
    if ignored.is_empty() {
        return Ok(());
    }
    ignored.sort_by(|a, b| a.as_bytes().cmp(b.as_bytes()));
    let key = format!("{}/zarr.json", crate::out::SOURCE_NODE);
    let Some(e) = out.entries.iter().find(|e| e.0 == key) else {
        // no mirror: a group of its own, whose source metadata is the ignored keys alone
        let attributes = crate::out::declare(serde_json::Map::new(), profile, None, Some(json!({"ignored": ignored})));
        out.json(&key, &crate::out::group_doc(attributes));
        return Ok(());
    };
    let mut doc: J = serde_json::from_slice(&e.1).map_err(|e| format!("internal: {e}"))?;
    let own = doc["attributes"][CONVENTION_KEY][profile].as_object_mut().ok_or("internal: vzip_source declares no mirror")?;
    own.insert("ignored".into(), json!(ignored));
    out.json(&key, &doc);
    Ok(())
}
