//! The MRC image projection (spec/virtualize/mrc.md §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mrc.md#4-the-image>, and the root's
//! source metadata of §5
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mrc.md#5-source-metadata>) from a valid
//! IR: the header is the IR's `header` value, and the array's chunks are cut from its
//! `data` element.

use super::{base64, float_json, json_text, n, Copied};
use crate::ir::Ir;
use crate::mrc::{Header, FIELDS};
use crate::nifti::Num;
use crate::out::{array_json, grid_chunks, image_ome, root_json, Chunk, Out, MAX_PAYLOAD};
use crate::refs::Refs;
use serde_json::{json, Map, Value as J};

/// The most bytes of a chunk of the image (spec/virtualize/mrc.md §4.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mrc.md#41-the-array>).
const CHUNK_BYTES: u64 = 1 << 20;
const MAX_SAFE: i64 = (1 << 53) - 1;
/// The space group from which a file is a volume stack (MRC2014).
const VOLUME_STACK: i64 = 401;

/// A header value as the root's source metadata writes it (spec/virtualize/mrc.md §5
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mrc.md#5-source-metadata>): an
/// integer exactly; a float as a number, "NaN", "Infinity" or "-Infinity", or {"bits": hex}
/// for a negative zero or a NaN other than the canonical one.
fn value_json(h: &Header, at: usize, kind: &str) -> J {
    match h.number(at, kind) {
        Num::Int(v) if (-MAX_SAFE..=MAX_SAFE).contains(&v) => json!(v),
        Num::Int(v) => json!(v.to_string()),
        Num::Float(v) => {
            let mut raw = h.raw[at..at + 4].to_vec();
            if h.little {
                raw.reverse();
            }
            float_json(raw.iter().fold(0u64, |a, &b| a << 8 | b as u64), 4, v)
        }
    }
}

/// Every header field, in the standard's order: `header` of the root's source metadata.
pub fn header_json(h: &Header) -> J {
    let mut out = Map::new();
    for &(name, at, kind, count) in FIELDS.iter() {
        let v = match kind {
            "c" => json_text(h.bytes(name)),
            "b" => json!(base64(h.bytes(name))),
            "l" => J::Array(h.bytes(name).chunks(80).map(json_text).collect()),
            _ if count == 1 => value_json(h, at, kind),
            _ => {
                let w = crate::nifti::width(kind);
                J::Array((0..count).map(|k| value_json(h, at + k * w, kind)).collect())
            }
        };
        out.insert(name.into(), v);
    }
    J::Object(out)
}

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
    mapped: bool,
    shape: Vec<u64>,
    item: u64,
    grid: Vec<u64>,
    chunks: Vec<(Vec<u64>, Chunk)>,
}

fn cut(h: &Header, offset: u64, facts: &J) -> Result<Cut, String> {
    let (nx, ny, nz) = (h.get("nx").i() as u64, h.get("ny").i() as u64, h.get("nz").i() as u64);
    let (ispg, mz) = (h.get("ispg").i(), h.get("mz").i());
    let spec = &crate::rules::mrc().table("modes")[h.get("mode").i().to_string()];
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
    Ok(Cut { kind, axes, mapped, shape, item, grid, chunks })
}

/// The header and the data's element.
fn parts<'a>(ir: &'a Ir, facts: &J) -> Result<(Header<'a>, u32), String> {
    let raw = ir.value_bytes(n(&facts["header"]) as u32).ok_or("internal: the header is not held")?;
    Ok((Header::new(raw), n(&facts["data"]) as u32))
}

/// The source's ranges of the edge chunks the projection copies (§14.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mrc.md#142-chunks>).
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
    let spec = &crate::rules::mrc().table("modes")[mode.to_string()];
    let data_type = spec["dtype"].as_str().unwrap_or("int8");
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
    let origin: Vec<f64> = h.values("origin").iter().map(|v| v.f()).collect();
    let placed = origin.iter().all(|v| v.is_finite()) && origin.iter().any(|&v| v != 0.0);
    let index = |a: &str| match a {
        "x" => 0,
        "y" => 1,
        _ => 2,
    };

    // the array (§4.1 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mrc.md#41-the-array>)
    let Cut { kind, axes, mapped, shape, item, grid, chunks } = cut(&h, offset, facts)?;
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
        ome["omero"] = json!({"channels": [{"label": "C0", "color": "FFFFFF", "active": true,
                                            "window": {"min": lo, "max": hi, "start": lo, "end": hi}}]});
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
    out.json("zarr.json", &root_json(ome, "mrc", url, Some(J::Object(own))));
    let codecs = vec![if item > 1 { json!({"name": "bytes", "configuration": {"endian": endian}}) } else { json!({"name": "bytes"}) }];
    let mut array = array_json(&shape, data_type, json!(grid), J::Array(codecs), &axes);
    if data_type == "complex64" {
        // a complex fill value is its real and imaginary parts (Zarr v3)
        array["fill_value"] = json!([0, 0]);
    }
    out.json("0/zarr.json", &array);
    let count = chunks.len();
    let mut refs = Refs::new(ir, out);
    for (piece, (coords, chunk)) in chunks.into_iter().enumerate() {
        let key = format!("0/c/{}", coords.iter().map(|c| c.to_string()).collect::<Vec<_>>().join("/"));
        match chunk {
            Chunk::Ranges(parts) => refs.chunk(key, offset, length, form, Some(parts), piece as u64)?,
            // an edge chunk whose padding does not fit in a reference: its bytes, then the padding
            Chunk::Copy(o, len, pad) => {
                let mut b = copied
                    .get(&(o, len))
                    .ok_or("a copied chunk's bytes were not read: project a run's IR with the bytes its last batch read")?
                    .clone();
                b.resize(b.len() + pad as usize, 0);
                refs.out.bytes(&key, b);
            }
            Chunk::Bytes(b) => refs.out.bytes(&key, b),
        }
    }
    let mut out = refs.out;
    let sizes: Map<String, J> = axes.iter().zip(&shape).map(|(a, s)| (a.to_string(), json!(s))).collect();
    out.summary = json!({
        "kind": kind, "byteOrder": endian, "mode": mode, "dataType": data_type, "sizes": sizes,
        "chunkShape": grid, "chunks": count,
        "voxelSize": if voxel.iter().all(|v| v.is_some()) { json!(voxel) } else { J::Null },
        "translation": placed, "axisMap": mapped, "extendedHeader": nsymbt,
        "extendedType": json_text(h.bytes("exttyp")),
    });
    Ok(out)
}
