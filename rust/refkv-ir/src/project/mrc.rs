//! The MRC image projection (spec/virtualize/mrc.md §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mrc.md#4-the-image>, and the root's
//! source metadata of §5
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mrc.md#5-source-metadata>) from a valid
//! IR: the header is the IR's `header` value, and the array's chunks are cut from its
//! `data` element.

use super::{header_json, json_text, n, Copied};
use crate::ir::Ir;
use crate::mrc::header;
use crate::record::Header;
use crate::out::{array_json, declare, grid_chunks, group_doc, image_ome, omero_channel, omero_json, omero_window, root_json, Chunk, Out, MAX_PAYLOAD};
use crate::refs::Refs;
use serde_json::{json, Map, Value as J};

/// The most bytes of a chunk of the image (spec/virtualize/mrc.md §4.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mrc.md#41-the-array>).
const CHUNK_BYTES: u64 = 1 << 20;
/// The space group from which a file is a volume stack (MRC2014).
const VOLUME_STACK: i64 = 401;
/// The dataset layout's node (spec/virtualize/mrc-dataset.md §1
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mrc-dataset.md#1-declaration>), and
/// the most values of a chunk of its coordinate variables (conventions §9
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#9-dataset-layouts>).
const DATASET: &str = "dataset";
const COORD_CHUNK: u64 = 1 << 17;
/// The gzip level of a coordinate chunk (conventions §9
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#9-dataset-layouts>).
const GZIP_LEVEL: u8 = 6;

/// The kind of file the space group and the section count make it (§4.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mrc.md#41-the-array>).
fn kind(ispg: i64, nz: i64) -> &'static str {
    if ispg >= VOLUME_STACK {
        "volume_stack"
    } else if ispg == 0 && nz == 1 {
        "image"
    } else if ispg == 0 {
        "image_stack"
    } else {
        "volume"
    }
}

/// The array's axes, the name of each of the file's dimensions (sections, rows, columns)
/// in the stored order, whether the axis map gave them, its shape and its cut (§4.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mrc.md#41-the-array>).
struct Cut {
    kind: &'static str,
    axes: Vec<&'static str>,
    /// the sections', the rows' and the columns' axis names
    names: Vec<&'static str>,
    mapped: bool,
    shape: Vec<u64>,
    item: u64,
    grid: Vec<u64>,
    chunks: Vec<(Vec<u64>, Chunk)>,
}

fn cut(h: &Header, offset: u64, facts: &J) -> Result<Cut, String> {
    let (nx, ny, nz) = (h.get("nx").i() as u64, h.get("ny").i() as u64, h.get("nz").i() as u64);
    let (ispg, mz) = (h.get("ispg").i(), h.get("mz").i());
    let spec = &crate::rules::of("mrc").table("modes")[h.get("mode").i().to_string()];
    let item = n(&spec["bytes"]);
    let kind = kind(ispg, nz as i64);
    // the axis map: which of x, y and z the sections, rows and columns run along
    let map = [h.get("maps").i(), h.get("mapr").i(), h.get("mapc").i()];
    let mapped = map.iter().all(|&m| (1..=3).contains(&m)) && map[0] != map[1] && map[1] != map[2] && map[0] != map[2];
    let name = |m: i64| match m {
        1 => "x",
        2 => "y",
        _ => "z",
    };
    let named: Vec<&'static str> = if mapped { map.iter().map(|&m| name(m)).collect() } else { vec!["z", "y", "x"] };
    let (axes, shape): (Vec<&'static str>, Vec<u64>) = match kind {
        "image" => (named[1..].to_vec(), vec![ny, nx]),
        "volume_stack" => (
            std::iter::once("t").chain(named.iter().copied()).collect(),
            vec![nz / mz as u64, mz as u64, ny, nx],
        ),
        _ => (named.clone(), vec![nz, ny, nx]),
    };
    let threshold = facts["copy_threshold"].as_u64().map_or(MAX_PAYLOAD, |t| t as usize);
    let (grid, chunks) = grid_chunks(offset, &shape, item, CHUNK_BYTES, threshold).ok_or("internal: the data cannot be cut into chunks")?;
    Ok(Cut { kind, axes, names: named, mapped, shape, item, grid, chunks })
}

/// The header and the data's element.
fn parts<'a>(ir: &'a Ir, facts: &J) -> Result<(Header<'a>, u32), String> {
    let raw = ir.value_bytes(n(&facts["header"]) as u32).ok_or("internal: the header is not held")?;
    Ok((header(raw), n(&facts["data"]) as u32))
}

/// The source's ranges of the edge chunks the projection copies (§8.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mrc.md#82-chunks>).
pub fn reads(ir: &Ir, facts: &J) -> Result<Vec<(u64, u64)>, String> {
    let (h, data) = parts(ir, facts)?;
    let c = cut(&h, ir.start[data as usize], facts)?;
    Ok(c.chunks.iter().filter_map(|(_, k)| match k {
        Chunk::Copy(o, n, _) => Some((*o, *n)),
        _ => None,
    }).collect())
}

fn valid(v: f64) -> bool {
    v.is_finite() && v > 0.0
}

pub fn project(ir: &Ir, facts: &J, url: &str, copied: &Copied) -> Result<Out, String> {
    let (h, data) = parts(ir, facts)?;
    let (offset, form) = (ir.start[data as usize], ir.form_id(data).ok_or("internal: the data has no form")?);
    let length = ir.len[data as usize];
    let mode = h.get("mode").i();
    let data_type = facts["data_type"].as_str().unwrap_or("int8");
    let f = |name: &str| h.get(name).f();

    // voxel size and origin (§4.2 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mrc.md#42-scales-units-and-position>)
    let cella: Vec<f64> = h.values("cella").iter().map(|v| v.f()).collect();
    let m = [h.get("mx").i(), h.get("my").i(), h.get("mz").i()];
    let voxel: Vec<Option<f64>> = (0..3)
        .map(|k| {
            if m[k] >= 1 && valid(cella[k]) {
                Some(cella[k] / m[k] as f64).filter(|v| valid(*v))
            } else {
                None
            }
        })
        .collect();
    // the origin places a volume, in angstroms; an image's is a phase origin in pixels
    let origin: Vec<f64> = h.values("origin").iter().map(|v| v.f()).collect();
    let placed = h.get("ispg").i() != 0 && origin.iter().all(|v| v.is_finite()) && origin.iter().any(|&v| v != 0.0);
    let index = |a: &str| match a {
        "x" => 0,
        "y" => 1,
        _ => 2,
    };

    // the array (§4.1 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mrc.md#41-the-array>)
    let Cut { kind, axes, names, mapped, shape, item, grid, chunks } = cut(&h, offset, facts)?;
    let mut units: Map<String, J> = Map::new();
    let scales: Vec<J> = axes
        .iter()
        .map(|a| match *a {
            "t" => json!(1),
            a => match voxel[index(a)] {
                Some(v) => {
                    units.insert(a.to_string(), json!("angstrom"));
                    json!(v)
                }
                None => json!(1),
            },
        })
        .collect();
    let translation = placed.then(|| {
        J::Array(axes.iter().map(|a| if *a == "t" { json!(0) } else { json!(origin[index(a)]) }).collect())
    });
    let trs = translation.clone().map(|t| vec![t]);
    let mut ome = image_ome(&axes, &units, &[J::Array(scales)], None, trs.as_deref())?;

    // the display window (§4.3 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mrc.md#43-channels>)
    let (lo, hi) = (f("dmin"), f("dmax"));
    let window = mode != 4 && lo.is_finite() && hi.is_finite() && hi > lo;
    if window {
        let window = omero_window(json!(lo), json!(hi), json!(lo), json!(hi));
        ome["omero"] = omero_json(vec![omero_channel("C0", "FFFFFF", Some(&window))]);
    }

    let endian = if h.little { "little" } else { "big" };
    let nsymbt = h.get("nsymbt").i();
    let mut own = Map::new();
    own.insert("byte_order".into(), json!(endian));
    own.insert("kind".into(), json!(kind));
    own.insert("data_type".into(), json!(data_type));
    own.insert("header".into(), header_json(&h));
    own.insert("axis_map".into(), json!({"applied": mapped}));
    if voxel.iter().all(|v| v.is_some()) {
        own.insert("voxel_size".into(), json!({"x": voxel[0], "y": voxel[1], "z": voxel[2]}));
    }
    let mut out = Out::new(url);
    let mut root = root_json(ome, "mrc", url, Some(J::Object(own.clone())));
    // the root names the dataset layout (conventions §2
    // <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#2-attributes>), after `source`
    root["attributes"]["refkv_virtualized"] = insert_after(&root["attributes"]["refkv_virtualized"], "source", "layouts", json!({DATASET: DATASET}));
    out.json("zarr.json", &root);
    let codecs = vec![if item > 1 { json!({"name": "bytes", "configuration": {"endian": endian}}) } else { json!({"name": "bytes"}) }];
    let mut array = array_json(&shape, data_type, json!(grid), J::Array(codecs), &axes);
    if data_type == "complex64" {
        // a complex fill value is its real and imaginary parts (Zarr v3)
        array["fill_value"] = json!([0, 0]);
    }
    out.json("0/zarr.json", &array);
    // the dataset layout's data variable: the same array under the dataset's dimension names
    // (spec/virtualize/mrc-dataset.md §2
    // <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mrc-dataset.md#2-the-data-variable>)
    let dims: Vec<&str> = match kind {
        "image" => names[1..].to_vec(),
        "image_stack" => std::iter::once("section").chain(names[1..].iter().copied()).collect(),
        "volume_stack" => std::iter::once("volume").chain(names.iter().copied()).collect(),
        _ => names.clone(),
    };
    let mut data = array.clone();
    data["dimension_names"] = json!(dims);
    if window {
        data["attributes"]["actual_range"] = json!([lo, hi]);
    }
    out.json(&format!("{DATASET}/zarr.json"), &group_doc(declare(Map::new(), "mrc-dataset", Some(url), Some(J::Object(own)))));
    out.json(&format!("{DATASET}/data/zarr.json"), &data);
    let count = chunks.len();
    let mut refs = Refs::new(ir, out);
    for (piece, (coords, chunk)) in chunks.into_iter().enumerate() {
        let coords = coords.iter().map(|c| c.to_string()).collect::<Vec<_>>().join("/");
        let (key, dkey) = (format!("0/c/{coords}"), format!("{DATASET}/data/c/{coords}"));
        match chunk {
            Chunk::Ranges(parts) => {
                let same = parts.clone();
                refs.chunk(key, offset, length, form, Some(parts), piece as u64)?;
                refs.out.refs(&dkey, same);
            }
            // an edge chunk whose padding does not fit in a reference: its bytes, then the padding
            Chunk::Copy(o, len, pad) => {
                let mut b = copied
                    .get(&(o, len))
                    .ok_or("a copied chunk's bytes were not read: project a run's IR with the bytes its last batch read")?
                    .clone();
                b.resize(b.len() + pad as usize, 0);
                refs.out.bytes(&key, b.clone());
                refs.out.bytes(&dkey, b);
            }
            Chunk::Bytes(b) => {
                refs.out.bytes(&key, b.clone());
                refs.out.bytes(&dkey, b);
            }
        }
    }
    let mut out = refs.out;
    // the coordinates (spec/virtualize/mrc-dataset.md §3
    // <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mrc-dataset.md#3-coordinates>): the
    // index along each spatial dimension with a voxel size, which CF's scale_factor and
    // add_offset make o + i * s; delta-coded and deflated (conventions §9
    // <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#9-dataset-layouts>)
    for (k, a) in dims.iter().enumerate() {
        let Some(s) = ["x", "y", "z"].contains(a).then(|| voxel[index(a)]).flatten() else { continue };
        let o = if placed { origin[index(a)] } else { 0.0 };
        let n = shape[k];
        let c = n.min(COORD_CHUNK);
        let codecs = json!([{"name": "numcodecs.delta", "configuration": {"dtype": "<u4"}},
                            {"name": "bytes", "configuration": {"endian": "little"}},
                            {"name": "gzip", "configuration": {"level": GZIP_LEVEL}}]);
        let mut doc = array_json(&[n], "uint32", json!([c]), codecs, &[a]);
        doc["attributes"] = json!({"units": "angstrom", "scale_factor": s, "add_offset": o});
        out.json(&format!("{DATASET}/{a}/zarr.json"), &doc);
        for q in 0..n.div_ceil(c) {
            out.bytes(&format!("{DATASET}/{a}/c/{q}"), index_chunk(q * c, c));
        }
    }
    let sizes: Map<String, J> = axes.iter().zip(&shape).map(|(a, s)| (a.to_string(), json!(s))).collect();
    out.summary = json!({
        "kind": kind, "byteOrder": endian, "mode": mode, "dataType": data_type, "sizes": sizes,
        "chunkShape": grid, "chunks": count,
        "voxelSize": if voxel.iter().all(|v| v.is_some()) { json!(voxel) } else { J::Null },
        "translation": placed, "axisMap": mapped, "extendedHeader": nsymbt,
        "extendedType": json_text(h.bytes("exttyp")), "layouts": [DATASET],
    });
    Ok(out)
}

/// A chunk of an index coordinate (conventions §9
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#9-dataset-layouts>): the indexes
/// `first .. first + count` (the ramp goes on past the array's end, into the padding), as
/// numcodecs' delta codes them (the first as it is, then each less the one before), as
/// little-endian u32, then gzip at level 6.
fn index_chunk(first: u64, count: u64) -> Vec<u8> {
    let mut raw = Vec::with_capacity((count * 4) as usize);
    raw.extend_from_slice(&(first as u32).to_le_bytes());
    for _ in 1..count {
        raw.extend_from_slice(&1u32.to_le_bytes());
    }
    gzip(&raw)
}

/// `raw` as a gzip member (RFC 1952): a header with no name, time or extra field, the
/// deflate stream, the CRC-32 and the length.
pub(super) fn gzip(raw: &[u8]) -> Vec<u8> {
    let mut out = vec![0x1f, 0x8b, 8, 0, 0, 0, 0, 0, 0, 255];
    out.extend(miniz_oxide::deflate::compress_to_vec(raw, GZIP_LEVEL));
    out.extend_from_slice(&crate::safe::zip::crc32(raw).to_le_bytes());
    out.extend_from_slice(&(raw.len() as u32).to_le_bytes());
    out
}

/// `object` with the member `key: value` inserted after the member `after` (at the end when
/// there is none), in order: the hosts write documents' members in insertion order.
fn insert_after(object: &J, after: &str, key: &str, value: J) -> J {
    let mut out = Map::new();
    let mut done = false;
    for (k, v) in object.as_object().into_iter().flatten() {
        out.insert(k.clone(), v.clone());
        if k == after {
            out.insert(key.into(), value.clone());
            done = true;
        }
    }
    if !done {
        out.insert(key.into(), value);
    }
    J::Object(out)
}
