//! What the store projections share (conventions §8.9
//! <https://github.com/d-v-b/vzip/blob/main/spec/conventions.md#89-stores>): the key of an other
//! object under `vzip_source/objects/`, a node's declaration, the canonical decimal of a chunk
//! index, the output keys the archive allows, and the ignored keys recorded beside the mirror.
//! Zarr v2 (`zarr2.rs`) uses them, and so do the other store formats.

use crate::json::Jv;
use crate::out::{convention, root_property, Out, CONVENTION_KEY};
use serde_json::{json, Value as J};

/// The prefix of the other objects' keys.
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

/// A decimal integer without leading zeros, below `limit`.
pub fn canonical_index(s: &str, limit: u64) -> bool {
    !s.is_empty() && s.len() <= 17 && s.bytes().all(|c| c.is_ascii_digit()) && (s == "0" || !s.starts_with('0'))
        && s.parse::<u64>().is_ok_and(|v| v < limit)
}

/// Rejects the output keys spec/archive.md does not allow.
pub fn check_key(key: &str) -> Result<(), String> {
    if key.len() > 65535 {
        return Err("an output key is longer than 65535 bytes".into());
    }
    if key.starts_with("__vz__/") {
        return Err(format!("output key {key:?} is in the reserved __vz__/ space"));
    }
    Ok(())
}

/// The members of `vzip_source`'s source metadata that the store conventions keep beside the
/// mirror's `ir`, outside the IR (conventions §8.9
/// <https://github.com/d-v-b/vzip/blob/main/spec/conventions.md#89-stores>), by profile: Zarr v2's
/// ignored keys (spec/virtualize/zarr2.md §5
/// <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/zarr2.md#5-other-objects>), and so
/// OME-Zarr's (spec/virtualize/ome-zarr.md §8.2
/// <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/ome-zarr.md#82-the-store-ir>), and SAFE's
/// empty directories and ignored keys, in both its forms (spec/virtualize/safe.md §6.3
/// <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/safe.md#63-the-source-metadata-node>).
/// The mirror check sets these aside, and only these.
pub fn kept(profile: &str) -> &'static [&'static str] {
    match profile {
        "zarr2" | "ome-zarr" => &["ignored"],
        "safe" => &["empty_dirs", "ignored"],
        _ => &[],
    }
}

/// After the mirror: the members [`kept`] beside the mirror's `ir` in `vzip_source`'s source
/// metadata, from the facts, each only when it is not empty: a SAFE product's empty
/// directories (`empty_dirs`, as its parser sorted them), then the recorded ignored keys
/// (spec/virtualize.md §1.4
/// <https://github.com/d-v-b/vzip/blob/main/spec/virtualize.md#14-store-inputs>; `ignored`), in
/// the order of their UTF-8 bytes (spec/virtualize/zarr2.md §5
/// <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/zarr2.md#5-other-objects>,
/// spec/virtualize/safe.md §6.3
/// <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/safe.md#63-the-source-metadata-node>).
/// Nothing when there are none. An output made without its mirror (`mirror` false, which no
/// virtualizer does) has no `vzip_source` source metadata at all, and so does not record them
/// either: like the mirror, they are what such an output leaves out.
pub fn record_ignored(out: &mut Out, facts: &J, profile: &str) -> Result<(), String> {
    let strings = |m: &str| -> Vec<&str> { facts[m].as_array().into_iter().flatten().filter_map(J::as_str).collect() };
    // a member the facts carry that the profile does not keep would be lost: refuse it
    for m in ["empty_dirs", "ignored"] {
        if !kept(profile).contains(&m) && !strings(m).is_empty() {
            return Err(format!("internal: the {profile} convention keeps no {m}"));
        }
    }
    let mut members: Vec<(&str, Vec<&str>)> = Vec::new();
    for &m in kept(profile) {
        let mut v = strings(m);
        if m == "ignored" {
            v.sort_by(|a, b| a.as_bytes().cmp(b.as_bytes()));
        }
        if !v.is_empty() {
            members.push((m, v));
        }
    }
    if members.is_empty() {
        return Ok(());
    }
    let key = format!("{}/zarr.json", crate::out::SOURCE_NODE);
    let Some(e) = out.entries.iter().find(|e| e.0 == key) else { return Ok(()) };
    let mut doc: J = serde_json::from_slice(&e.1).map_err(|e| format!("internal: {e}"))?;
    let own = doc["attributes"][CONVENTION_KEY][profile].as_object_mut().ok_or("internal: vzip_source declares no mirror")?;
    for (m, v) in members {
        own.insert(m.into(), json!(v));
    }
    out.json(&key, &doc);
    Ok(())
}
