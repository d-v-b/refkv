//! The precomputed projection (spec/zarrify/precomputed.md §2
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/precomputed.md#2-the-info-document>–§8
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/precomputed.md#8-other-objects>) from a
//! valid store IR: `info`, read back from the held object, and the listing's keys. Each scale
//! the parser's rules present becomes an array with the codec `refkv.precomputed`, and each of
//! its chunk objects (or shards, the box of chunks each holds being known from its name) a
//! reference to the whole object after a literal header, its position on the chunk grid. Every
//! other object is referenced whole at `refkv_source/objects/<k>`.

use crate::ir::Ir;
use crate::json::{self, Jv};
use crate::out::{omero_channel, omero_json, type_window, Out, Part};
use crate::precomputed::{read_scale, shard_box, Scale, DOCUMENT};
use crate::project::store::{check_key, declare, object_key};
use serde_json::{json, Value as J};
use std::collections::{BTreeSet, HashMap};

const PROFILE: &str = "precomputed";
/// The coding suffixes of a chunk object's name (spec/zarrify/precomputed.md §3.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/precomputed.md#32-chunk-objects>).
const SUFFIXES: [&str; 3] = [".gz", ".br", ".zstd"];

/// One bound of a chunk's name, a decimal without leading zeros or `-0`.
fn bound(s: &str) -> Option<i64> {
    let digits = s.strip_prefix('-').unwrap_or(s);
    if digits.is_empty() || digits.len() > 16 || !digits.bytes().all(|c| c.is_ascii_digit()) {
        return None;
    }
    if digits.len() > 1 && digits.starts_with('0') || s == "-0" {
        return None;
    }
    s.parse().ok()
}

/// `a-b`, where each of `a` and `b` may start with `-`.
fn interval(s: &str) -> Option<(i64, i64)> {
    let start = usize::from(s.starts_with('-'));
    let dash = s.get(start..)?.find('-')? + start;
    Some((bound(&s[..dash])?, bound(&s[dash + 1..])?))
}

/// The chunk position that the name `<x0>-<x1>_<y0>-<y1>_<z0>-<z1>` gives on the scale's grid,
/// when its bounds are that chunk's, clipped to the volume.
fn chunk_of_name(name: &str, s: &Scale) -> Option<[u64; 3]> {
    let name = SUFFIXES.iter().find_map(|x| name.strip_suffix(x)).unwrap_or(name);
    let mut parts = name.split('_');
    let (x, y, z) = (parts.next()?, parts.next()?, parts.next()?);
    if parts.next().is_some() {
        return None;
    }
    let bounds = [interval(x)?, interval(y)?, interval(z)?];
    let grid = s.grid();
    let mut pos = [0u64; 3];
    for a in 0..3 {
        let (lo, hi) = bounds[a];
        let from = lo.checked_sub(s.offset[a])?;
        let c = i64::try_from(s.chunk[a]).ok()?;
        if from < 0 || from % c != 0 {
            return None;
        }
        let i = (from / c) as u64;
        let last = s.offset[a].checked_add(i64::try_from(s.size[a]).ok()?)?;
        if i >= grid[a] || hi != lo.checked_add(c)?.min(last) {
            return None;
        }
        pos[a] = i;
    }
    Some(pos)
}

/// The box a shard's name `<hex>.shard` gives on the grid of boxes, when the name is canonical
/// (lowercase, `ceil(shard_bits / 4)` digits at least one) and the box is within the volume.
fn box_of_name(name: &str, s: &Scale) -> Option<[u64; 3]> {
    let sh = s.sharding.as_ref()?;
    let hex = name.strip_suffix(".shard")?;
    let width = (sh.shard_bits as usize).div_ceil(4).max(1);
    if hex.len() != width || !hex.bytes().all(|c| matches!(c, b'0'..=b'9' | b'a'..=b'f')) {
        return None;
    }
    let number = u64::from_str_radix(hex, 16).ok()?;
    if sh.shard_bits < 64 && number >> sh.shard_bits != 0 {
        return None;
    }
    let b = shard_box(sh, number)?;
    let grid = s.grid();
    (0..3).all(|a| b[a].checked_mul(sh.box_shape[a]).is_some_and(|o| o < grid[a])).then_some(b)
}

/// A presented scale's array document (spec/zarrify/precomputed.md §3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/precomputed.md#3-scales>).
fn array_doc(s: &Scale, data_type: &str, channels: u64, own: Jv) -> Jv {
    let ints = |v: &[u64]| Jv::Arr(v.iter().map(|&x| Jv::integer(x as i64)).collect());
    let [sx, sy, sz] = s.size;
    let held = s.sharding.as_ref().map_or([1; 3], |h| h.box_shape);
    let grid = [0, 1, 2].map(|a| s.chunk[a] * held[a]);
    let c = channels > 1;
    let with_c = |v: [u64; 3]| -> Vec<u64> {
        let (x, y, z) = (v[0], v[1], v[2]);
        if c { vec![channels, z, y, x] } else { vec![z, y, x] }
    };
    let mut config = vec![
        ("encoding", Jv::str(&s.encoding)),
        ("size", ints(&s.size)),
        ("chunk_size", ints(&s.chunk)),
    ];
    if s.encoding == "compressed_segmentation" {
        config.push(("compressed_segmentation_block_size", ints(&s.block.unwrap_or([0; 3]))));
    }
    if let Some(h) = &s.sharding {
        config.push(("sharding", Jv::obj(vec![
            ("preshift_bits", Jv::integer(h.preshift_bits as i64)),
            ("minishard_bits", Jv::integer(h.minishard_bits as i64)),
            ("minishard_index_encoding", Jv::str(&h.minishard_index_encoding)),
            ("data_encoding", Jv::str(&h.data_encoding)),
        ])));
    }
    let names: &[&str] = if c { &["c", "z", "y", "x"] } else { &["z", "y", "x"] };
    Jv::obj(vec![
        ("zarr_format", Jv::integer(3)),
        ("node_type", Jv::str("array")),
        ("shape", ints(&with_c([sx, sy, sz]))),
        ("data_type", Jv::str(data_type)),
        ("chunk_grid", Jv::obj(vec![("name", Jv::str("regular")),
                                    ("configuration", Jv::obj(vec![("chunk_shape", ints(&with_c(grid)))]))])),
        ("chunk_key_encoding", Jv::obj(vec![("name", Jv::str("default")),
                                            ("configuration", Jv::obj(vec![("separator", Jv::str("/"))]))])),
        ("fill_value", Jv::integer(0)),
        ("codecs", Jv::Arr(vec![Jv::obj(vec![("name", Jv::str("refkv.precomputed")),
                                              ("configuration", Jv::obj(config))])])),
        ("dimension_names", Jv::Arr(names.iter().map(|n| Jv::str(n)).collect())),
        ("attributes", declare(PROFILE, None, Some(own))),
    ])
}

/// The OME-NGFF object of the presented scales, full resolution first (spec/zarrify/precomputed.md
/// §5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/precomputed.md#5-the-image>).
/// A voxel `i` covers `[i, i + 1) × resolution`, and OME-NGFF places its center.
fn ome(scales: &[Scale], data_type: &str, channels: u64, image: bool) -> J {
    let c = channels > 1;
    let mut axes = Vec::new();
    if c {
        axes.push(json!({"name": "c", "type": "channel"}));
    }
    for a in ["z", "y", "x"] {
        axes.push(json!({"name": a, "type": "space", "unit": "nanometer"}));
    }
    let datasets: Vec<J> = scales.iter().enumerate().map(|(i, s)| {
        let [rx, ry, rz] = s.resolution;
        let at = |a: usize| (s.offset[a] as f64 + 0.5) * s.resolution[a];
        let (scale, translation) = if c {
            (json!([1.0, rz, ry, rx]), json!([0.0, at(2), at(1), at(0)]))
        } else {
            (json!([rz, ry, rx]), json!([at(2), at(1), at(0)]))
        };
        json!({"path": i.to_string(), "coordinateTransformations": [
            {"type": "scale", "scale": scale}, {"type": "translation", "translation": translation}]})
    }).collect();
    let mut m = json!({"version": "0.5", "multiscales": [{"axes": axes, "datasets": datasets}]});
    if image {
        let window = type_window(data_type);
        let color = |i: u64| match (channels, i) {
            (3, 0) => "FF0000",
            (3, 1) => "00FF00",
            (3, 2) => "0000FF",
            _ => "FFFFFF",
        };
        let chans = (0..channels).map(|i| omero_channel(&i.to_string(), color(i), window.as_ref())).collect();
        m["omero"] = omero_json(chans);
    }
    m
}

/// The precomputed hierarchy of a valid store IR, outside `refkv_source`, and its references.
pub fn project(ir: &Ir, _facts: &J, url: &str) -> Result<Out, String> {
    let src = ir.sources();
    let listing: Vec<(String, u64)> = src.objects.iter().map(|&o| (ir.key(o), ir.len[o as usize])).collect();
    let info_o = src.objects.iter().copied().find(|&o| ir.key(o) == DOCUMENT).ok_or("internal: no info")?;
    let b = ir.value_bytes(info_o).ok_or("internal: info is not held")?;
    let info = json::parse(b).map_err(|e| format!("internal: info: {e}"))?.value;
    let data_type = info.get("data_type").and_then(Jv::as_str).unwrap_or("").to_string();
    let channels = info.get("num_channels").and_then(|v| v.int(1, 65535)).unwrap_or(1);
    let image = info.get("type").and_then(Jv::as_str) == Some("image");

    let mut scales: Vec<(Scale, Jv, usize)> = Vec::new();
    let mut seen: BTreeSet<String> = BTreeSet::new();
    let mut not_presented: Vec<Jv> = Vec::new();
    let empty = Vec::new();
    for (i, v) in info.get("scales").and_then(Jv::items).map(<[Jv]>::to_vec).unwrap_or(empty).into_iter().enumerate() {
        match read_scale(&v, &data_type, channels) {
            Ok(s) if seen.insert(s.key.clone()) => scales.push((s, v, i)),
            Ok(s) => not_presented.push(Jv::obj(vec![("index", Jv::integer(i as i64)),
                ("reason", Jv::str(&format!("its key {:?} is an earlier scale's", s.key)))])),
            Err(reason) => not_presented.push(Jv::obj(vec![("index", Jv::integer(i as i64)), ("reason", Jv::str(&reason))])),
        }
    }
    // full resolution first, scales of one resolution in the document's order
    let volume = |s: &Scale| s.resolution.iter().product::<f64>();
    scales.sort_by(|a, b| volume(&a.0).total_cmp(&volume(&b.0)).then(a.2.cmp(&b.2)));
    let plain: Vec<Scale> = scales.iter().map(|s| s.0.clone()).collect();

    let mut out = Out::new(url);
    let mut own = Jv::obj(vec![("info", info.clone())]);
    if !not_presented.is_empty() {
        own.set("not_presented", Jv::Arr(not_presented.clone()));
    }
    let mut attrs = declare(PROFILE, Some(url), Some(own));
    if !plain.is_empty() {
        attrs.set("ome", Jv::from_j(&ome(&plain, &data_type, channels as u64, image)));
    }
    let root = Jv::obj(vec![("zarr_format", Jv::integer(3)), ("node_type", Jv::str("group")), ("attributes", attrs)]);
    out.bytes("zarr.json", root.text().into_bytes());
    for (n, (s, v, i)) in scales.iter().enumerate() {
        let own = Jv::obj(vec![("index", Jv::integer(*i as i64)), ("scale", v.clone())]);
        out.bytes(&format!("{n}/zarr.json"), array_doc(s, &data_type, channels as u64, own).text().into_bytes());
    }

    // the objects of each presented scale's directory
    let dirs: HashMap<&str, usize> = plain.iter().enumerate().map(|(n, s)| (s.key.as_str(), n)).collect();
    let mut refs: Vec<(String, Vec<Part>)> = Vec::new();
    let mut taken: BTreeSet<String> = BTreeSet::new();
    let (mut chunks, mut empty_chunks, mut others) = (0u64, 0u64, 0u64);
    for (k, n) in &listing {
        if k == DOCUMENT {
            continue;
        }
        let at = k.rfind('/').and_then(|i| dirs.get(&k[..i]).map(|&s| (s, &k[i + 1..])));
        let placed = at.and_then(|(si, name)| {
            let s = &plain[si];
            let (pos, position) = match &s.sharding {
                None => {
                    let p = chunk_of_name(name, s)?;
                    (p, p)
                }
                Some(h) => {
                    let b = box_of_name(name, s)?;
                    (b, [0, 1, 2].map(|a| b[a] * h.box_shape[a]))
                }
            };
            let [x, y, z] = pos;
            let c = if channels > 1 { "0/" } else { "" };
            Some((format!("{si}/c/{c}{z}/{y}/{x}"), position))
        });
        match placed {
            // a second object of a chunk (`x` and `x.gz`): the first by key answers for it
            Some((entry, position)) if taken.insert(entry.clone()) => {
                if *n == 0 {
                    empty_chunks += 1;
                    continue;
                }
                let mut header = Vec::with_capacity(12);
                for p in position {
                    header.extend_from_slice(&(p as u32).to_le_bytes());
                }
                chunks += 1;
                refs.push((entry, vec![Part::Lit(header), Part::Obj(k.clone(), 0, *n)]));
            }
            _ if *n > 0 => {
                others += 1;
                refs.push((object_key(k), vec![Part::Obj(k.clone(), 0, *n)]));
            }
            _ => {}
        }
    }
    for (k, _) in &refs {
        check_key(k)?;
    }
    for (k, parts) in refs {
        out.refs(&k, parts);
    }
    out.summary = json!({
        "arrays": plain.len(), "notPresented": not_presented.len(), "sharded": plain.iter().filter(|s| s.sharding.is_some()).count(),
        "chunks": chunks, "emptyChunks": empty_chunks, "objects": listing.len(), "otherObjects": others,
    });
    Ok(out)
}

/// The projection of the profile's store run (`StoreFormat`; crate::store).
pub fn store_project(ir: &Ir, facts: &J, url: &str) -> Result<Out, String> {
    project(ir, facts, url)
}
