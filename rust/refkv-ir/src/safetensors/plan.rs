//! refkv's plan of a safetensors file or store (spec/zarrify/safetensors.md §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#4-the-hierarchy>, §9.1
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#91-the-hierarchy>,
//! §9.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#92-source-metadata>):
//! the convention's decisions, made from the parser's description alone (§8.5
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#85-the-description>, §9.7
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#97-the-description>):
//! which tensors are presented, at which paths, and why the others are not; the groups their paths
//! make; each presented tensor's Zarr data type, fill value and element bits; the source metadata;
//! and, of a store, which shards are presented, the groups of their keys, whether each index agrees
//! with the shards, and which objects are referenced whole. The projection (`project/safetensors.rs`)
//! writes the hierarchy from it; a layout that wants another hierarchy makes its own from the
//! description.

use crate::rules::{self, Rules};
use serde_json::{json, Map, Value as J};
use std::collections::{BTreeMap, BTreeSet, HashMap};

fn rules() -> &'static Rules {
    rules::of("safetensors")
}

fn list(v: &J) -> &[J] {
    v.as_array().map_or(&[], Vec::as_slice)
}

fn n(v: &J) -> u64 {
    v.as_u64().unwrap_or(0)
}

/// A tensor's path, its name with `/` between node names, or why it has none (§4.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#41-paths>).
pub fn tensor_path(name: &str) -> Result<String, &'static str> {
    let parts: Vec<&str> = name.split('/').collect();
    if parts.iter().any(|p| p.is_empty() || *p == "." || *p == ".." || p.starts_with("__") || *p == "zarr.json") {
        return Err("name");
    }
    if parts[0] == crate::out::SOURCE_NODE {
        return Err("reserved");
    }
    Ok(name.to_string())
}

/// The proper prefixes of a path that end before one of its `/`: the groups that hold it.
fn groups_of(path: &str) -> impl Iterator<Item = &str> {
    path.match_indices('/').map(move |(i, _)| &path[..i])
}

/// The plan of a file (or of a shard) whose header the description `header` gives (§8.5
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#85-the-description>), with
/// its `elements`: `own`, the source metadata, each tensor with its `path` or its `reason`;
/// `arrays`, each presented tensor's `path`, `index` (its position in the header), `data` (its data
/// element), `offset` and `length` (its bytes, from the file's start), `dtype`, `data_type`, `bits`,
/// `fill` and `shape`; and `groups`, the groups the paths make.
pub fn plan(header: &J) -> Result<J, String> {
    let r = rules();
    let n_header = n(&header["header_length"]);
    let tensors = list(&header["tensors"]);
    let data = list(&header["elements"]["tensors"]);
    let mut paths: Vec<Result<String, &'static str>> = tensors.iter().map(|t| tensor_path(t["name"].as_str().unwrap_or(""))).collect();
    // a path that is another's group is no array's
    let held: BTreeSet<String> = paths.iter().filter_map(|p| p.as_deref().ok()).flat_map(groups_of).map(str::to_string).collect();
    for p in paths.iter_mut() {
        if p.as_ref().is_ok_and(|p| held.contains(p)) {
            *p = Err("path");
        }
    }
    let groups: BTreeSet<&str> = paths.iter().filter_map(|p| p.as_deref().ok()).flat_map(groups_of).collect();
    let mut arrays = Vec::new();
    let mut listed = Vec::new();
    for (i, (t, p)) in tensors.iter().zip(&paths).enumerate() {
        let mut j = Map::new();
        for k in ["name", "dtype", "shape", "data_offsets", "extra"] {
            if let Some(v) = t.get(k) {
                j.insert(k.into(), v.clone());
            }
        }
        match p {
            Ok(p) => {
                j.insert("path".into(), json!(p));
                let dtype = t["dtype"].as_str().unwrap_or("");
                let ty = r.entry("types", dtype).ok_or_else(|| format!("internal: an unknown data type {dtype}"))?;
                let (begin, end) = (n(&t["data_offsets"][0]), n(&t["data_offsets"][1]));
                arrays.push(json!({
                    "path": p, "index": i, "data": data.get(i).cloned().unwrap_or(J::Null), "offset": 8 + n_header + begin,
                    "length": end - begin, "dtype": dtype, "data_type": ty["dtype"], "bits": ty["bits"], "fill": ty["fill"],
                    "shape": t["shape"],
                }));
            }
            Err(why) => {
                j.insert("reason".into(), json!(why));
            }
        }
        listed.push(J::Object(j));
    }
    let mut own = Map::new();
    own.insert("header_length".into(), json!(n_header));
    if !header["metadata"].is_null() {
        own.insert("metadata".into(), header["metadata"].clone());
    }
    own.insert("tensors".into(), J::Array(listed));
    Ok(json!({"own": own, "arrays": arrays, "groups": groups}))
}

/// The directory of a key: its prefix before its last `/` (the root's, empty).
fn dir_of(key: &str) -> &str {
    key.rfind('/').map_or("", |i| &key[..i])
}

/// The key of a sharded checkpoint's index.
fn is_index(key: &str) -> bool {
    key.ends_with(".safetensors.index.json")
}

/// The path of a shard's group, its key, or why it has none: a key segment that is not a Zarr
/// node name, or a first segment `refkv_source` (§9.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#91-the-hierarchy>).
fn shard_path(key: &str) -> Result<String, String> {
    if key.split('/').any(|p| p.starts_with("__") || p == "zarr.json") {
        return Err("name: a segment of its key is not a Zarr node name".into());
    }
    if key.split('/').next() == Some(crate::out::SOURCE_NODE) {
        return Err(format!("reserved: its key starts with {}", crate::out::SOURCE_NODE));
    }
    Ok(key.to_string())
}

/// The plan of a store whose description is `description` (§9.7
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#97-the-description>) and
/// whose listing is `listing` (each key and size): `own`, the store's source metadata (§9.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#92-source-metadata>);
/// `shards`, each presented shard's `key`, `path` and `planned`, its file plan; `dirs`, the groups
/// above them; and `others`, each object referenced whole, `[key, size]`.
pub fn plan_store(description: &J, listing: &[(String, u64)]) -> Result<J, String> {
    let shards = list(&description["shards"]);
    // a shard whose key is another shard's directory has no group (S3 allows `a` and `a/b`)
    let shard_dirs: BTreeSet<&str> = shards.iter().filter_map(|s| s["key"].as_str()).flat_map(groups_of).collect();
    let mut files = Vec::new();
    let mut presented = Vec::new();
    let mut tensors_of: HashMap<String, BTreeSet<String>> = HashMap::new();
    for s in shards {
        let key = s["key"].as_str().unwrap_or("");
        let path = match s["error"].as_str() {
            Some(why) => Err(why.to_string()),
            None if shard_dirs.contains(key) => Err("path: its key is another shard's directory".to_string()),
            None => shard_path(key),
        };
        match path {
            Ok(path) => {
                let planned = plan(&s["header"])?;
                let names = list(&s["header"]["tensors"]).iter().filter_map(|t| t["name"].as_str().map(str::to_string)).collect();
                tensors_of.insert(key.to_string(), names);
                files.push(json!({"key": key, "path": path}));
                presented.push(json!({"key": key, "path": path, "planned": planned}));
            }
            Err(why) => files.push(json!({"key": key, "reason": why})),
        }
    }
    let mut indexes = Vec::new();
    let mut configs = Vec::new();
    let mut held = BTreeSet::new();
    for d in list(&description["documents"]) {
        let key = d["key"].as_str().unwrap_or("");
        let Some(text) = d["json"].as_str() else {
            let record = json!({"key": key, "problem": d["error"]});
            if is_index(key) { indexes.push(record) } else { configs.push(record) }
            continue;
        };
        held.insert(key);
        let value: J = serde_json::from_str(text).unwrap_or(J::Null);
        if is_index(key) {
            indexes.push(index_record(key, &value, &tensors_of));
        } else {
            configs.push(json!({"key": key, "config": value}));
        }
    }
    let presented_keys: BTreeSet<&str> = presented.iter().filter_map(|p| p["key"].as_str()).collect();
    let others: Vec<J> = listing.iter()
        .filter(|(k, size)| !held.contains(k.as_str()) && !presented_keys.contains(k.as_str()) && *size > 0)
        .map(|(k, size)| json!([k, size]))
        .collect();
    let dirs: BTreeSet<&str> = presented.iter().filter_map(|p| p["path"].as_str()).flat_map(groups_of).collect();
    let mut own = Map::new();
    own.insert("files".into(), J::Array(files));
    if !indexes.is_empty() {
        own.insert("indexes".into(), J::Array(indexes));
    }
    if !configs.is_empty() {
        own.insert("configs".into(), J::Array(configs));
    }
    Ok(json!({"own": own, "shards": presented, "dirs": dirs, "others": others}))
}

/// An index's record (§9.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#92-source-metadata>): its
/// `metadata`, and how its `weight_map` disagrees with the presented shards beside it, whose
/// headers decide.
fn index_record(key: &str, value: &J, tensors_of: &HashMap<String, BTreeSet<String>>) -> J {
    let dir = dir_of(key);
    let mut problems = Vec::new();
    let mut listed: BTreeMap<&str, BTreeSet<&str>> = BTreeMap::new();
    match value["weight_map"].as_object() {
        None => problems.push("it has no weight_map object".to_string()),
        Some(map) => {
            for (tensor, file) in map {
                let Some(file) = file.as_str() else {
                    problems.push(format!("{tensor:?} names no file"));
                    continue;
                };
                let shard = if dir.is_empty() { file.to_string() } else { format!("{dir}/{file}") };
                match tensors_of.get(&shard) {
                    None => problems.push(format!("{tensor:?} is in {file:?}, which is not a presented shard")),
                    Some(names) if !names.contains(tensor) => problems.push(format!("{tensor:?} is not in {file:?}")),
                    Some(_) => {
                        listed.entry(file).or_default().insert(tensor.as_str());
                    }
                }
            }
            for (file, set) in &listed {
                let shard = if dir.is_empty() { file.to_string() } else { format!("{dir}/{file}") };
                if let Some(names) = tensors_of.get(&shard) {
                    let missing = names.iter().filter(|n| !set.contains(n.as_str())).count();
                    if missing > 0 {
                        problems.push(format!("{missing} tensors of {file:?} are not in its weight_map"));
                    }
                }
            }
        }
    }
    let count = problems.len();
    problems.truncate(20);
    let mut r = json!({"key": key, "agrees": count == 0});
    if let Some(m) = value.get("metadata") {
        r["metadata"] = m.clone();
    }
    if count > 0 {
        r["problems"] = json!(problems);
        r["problem_count"] = json!(count);
    }
    r
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A name is a tensor's path when its segments are Zarr node names and its first is not
    /// `refkv_source`.
    #[test]
    fn tensor_paths() {
        assert_eq!(tensor_path("a/b.c"), Ok("a/b.c".into()));
        for (name, why) in [("", "name"), ("a//b", "name"), ("./a", "name"), ("__x", "name"), ("a/zarr.json", "name"), ("refkv_source/x", "reserved")] {
            assert_eq!(tensor_path(name), Err(why), "{name}");
        }
    }

    /// A tensor whose path is another's group is not presented, and a shard whose key is another
    /// shard's directory neither.
    #[test]
    fn a_path_that_is_a_group_is_no_arrays() {
        let t = |name: &str| json!({"name": name, "dtype": "U8", "shape": [1], "data_offsets": [0, 1]});
        let header = json!({"header_length": 2, "metadata": null, "tensors": [t("a"), t("a/b")], "elements": {"tensors": [5, 6]}});
        let p = plan(&header).unwrap();
        assert_eq!(p["own"]["tensors"][0]["reason"], "path");
        assert_eq!(p["arrays"].as_array().unwrap().len(), 1);
        assert_eq!(p["groups"], json!(["a"]));
        let store = json!({"shards": [{"key": "a", "object": 1, "header": header}, {"key": "a/b", "object": 2, "error": "short"}], "documents": []});
        let s = plan_store(&store, &[("a".into(), 9), ("a/b".into(), 3)]).unwrap();
        assert_eq!(s["own"]["files"][0]["reason"], "path: its key is another shard's directory");
        assert_eq!(s["own"]["files"][1]["reason"], "short");
        assert_eq!(s["others"], json!([["a", 9], ["a/b", 3]]));
    }
}
