//! The NIfTI image projection (spec/zarrify/nifti.md §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nifti.md#4-the-image>, and the root's
//! source metadata of §5
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nifti.md#5-source-metadata>) from a valid
//! IR: the header is the IR's `header` value, and the array's chunks are cut from its
//! `voxels` element.

use super::{along, contiguous, header_json, image_axes, n, Copied};
use crate::ir::Ir;
use crate::nifti::header;
use crate::record::Header;
use crate::out::{array_json, bytes_codec, full_window, grid_chunks, image_ome, omero_channel, omero_json, root_json, Chunk, Out, RGBA};
use crate::refs::Refs;
use serde_json::{json, Map, Value as J};

/// The most bytes of a chunk of the image (spec/zarrify/nifti.md §4.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nifti.md#41-the-array>).
const CHUNK_BYTES: u64 = 1 << 17;
/// The most channels given display windows (spec/zarrify/nifti.md §4.4
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nifti.md#44-channels>).
const MAX_CHANNELS: i64 = 64;

fn valid(v: f64) -> bool {
    v.is_finite() && v > 0.0
}

/// A scale or window value: an integer where today's profile writes one.
fn int_or(v: Option<f64>, default: i64) -> J {
    v.map(|x| json!(x)).unwrap_or(json!(default))
}

/// The array's axes, the voxels' axes in file order (dimension 5 outermost, a color
/// type's samples innermost) and their sizes, the bytes of an element of the stored
/// array, and its cut (§4.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nifti.md#41-the-array>).
struct Cut {
    axes: Vec<&'static str>,
    stored: Vec<&'static str>,
    shape: Vec<u64>,
    item: u64,
    grid: Vec<u64>,
    chunks: Vec<(Vec<u64>, Chunk)>,
}

fn cut(h: &Header, vox: u64, facts: &J) -> Result<Cut, String> {
    let dim: Vec<i64> = h.values("dim").iter().map(|x| x.i()).collect();
    let nd = dim[0];
    let size = |i: usize| if (i as i64) <= nd { dim[i] as u64 } else { 1 };
    let spec = &crate::rules::of("nifti").table("datatypes")[h.get("datatype").i().to_string()];
    let (bits, samples) = (n(&spec["bits"]), n(&spec["samples"]));
    let color = samples > 1;
    let axes = image_axes(nd >= 4, nd >= 5 || color, nd >= 3);
    let mut stored: Vec<&'static str> = if nd >= 5 { vec!["c"] } else { vec![] };
    stored.extend(axes.iter().filter(|a| **a != "c"));
    if color {
        stored.push("c");
    }
    let shape: Vec<u64> = stored
        .iter()
        .map(|a| along(a, [size(4), if color { samples } else { size(5) }, size(3), size(2), size(1)]))
        .collect();
    let item = bits / 8 / samples;
    let (grid, chunks) = grid_chunks(vox, &shape, item, CHUNK_BYTES, contiguous::threshold(facts)).ok_or("internal: the voxel data cannot be cut into chunks")?;
    Ok(Cut { axes, stored, shape, item, grid, chunks })
}

/// The header and the voxel data's element.
fn parts<'a>(ir: &'a Ir, facts: &J) -> Result<(Header<'a>, u32), String> {
    let raw = ir.value_bytes(n(&facts["header"]) as u32).ok_or("internal: the header is not held")?;
    Ok((header(raw, n(&facts["version"]) as u8), n(&facts["voxels"]) as u32))
}

/// The source's ranges of the edge chunks the projection copies (§7.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nifti.md#72-chunks>).
pub fn reads(ir: &Ir, facts: &J) -> Result<Vec<(u64, u64)>, String> {
    let (h, voxels) = parts(ir, facts)?;
    let c = cut(&h, ir.start[voxels as usize], facts)?;
    Ok(contiguous::copy_reads(&c.chunks).collect())
}

pub fn project(ir: &Ir, facts: &J, url: &str, copied: &Copied) -> Result<Out, String> {
    let (h, voxels) = parts(ir, facts)?;
    let version = n(&facts["version"]) as u8;
    let (vox, form) = (ir.start[voxels as usize], ir.form_id(voxels).ok_or("internal: the voxels have no form")?);
    let length = ir.len[voxels as usize];
    let dim: Vec<i64> = h.values("dim").iter().map(|x| x.i()).collect();
    let c = if dim[0] >= 5 { dim[5] as u64 } else { 1 };
    let spec = &crate::rules::of("nifti").table("datatypes")[h.get("datatype").i().to_string()];
    let (data_type, samples) = (spec["dtype"].as_str().unwrap_or("uint8"), n(&spec["samples"]));
    let color = samples > 1;
    let f = |name: &str| h.get(name).f();

    // value scaling (§4.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nifti.md#42-value-scaling>)
    let mut scaling = None;
    if !color {
        let (slope, inter) = (f("scl_slope"), f("scl_inter"));
        if slope.is_finite() && slope != 0.0 {
            scaling = Some((slope, if inter.is_finite() { inter } else { 0.0 }));
        }
    }
    let nontrivial = scaling.is_some_and(|(s, i)| s != 1.0 || i != 0.0);

    // scales, units and position (§4.3
    // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nifti.md#43-scales-units-and-position>)
    let units_code = h.get("xyzt_units").i();
    let space = match units_code & 7 {
        1 => Some("meter"),
        2 => Some("millimeter"),
        3 => Some("micrometer"),
        _ => None,
    };
    let time = match units_code & 56 {
        8 => Some("second"),
        16 => Some("millisecond"),
        24 => Some("microsecond"),
        _ => None,
    };
    let pixdim: Vec<f64> = h.values("pixdim").iter().map(|v| v.f()).collect();
    let (mut affine, mut placed): (Option<&str>, Option<([f64; 3], [f64; 3])>) = (None, None);
    if h.get("sform_code").i() > 0 {
        affine = Some("sform");
        let s: Vec<f64> = ["srow_x", "srow_y", "srow_z"].iter().flat_map(|r| h.values(r)).map(|v| v.f()).collect();
        if s.iter().all(|v| v.is_finite()) && [1, 2, 4, 6, 8, 9].iter().all(|&k| s[k] == 0.0) && s[0] > 0.0 && s[5] > 0.0 && s[10] > 0.0 {
            placed = Some(([s[0], s[5], s[10]], [s[3], s[7], s[11]]));
        }
    } else if h.get("qform_code").i() > 0 {
        affine = Some("qform");
        let q = [f("quatern_b"), f("quatern_c"), f("quatern_d")];
        let o = [f("qoffset_x"), f("qoffset_y"), f("qoffset_z")];
        if q.iter().all(|&v| v == 0.0) && !(pixdim[0] < 0.0) && pixdim[1..4].iter().all(|&v| valid(v)) && o.iter().all(|v| v.is_finite()) {
            placed = Some(([pixdim[1], pixdim[2], pixdim[3]], o));
        }
    }
    let mut scale: Map<String, J> = Map::new();
    let mut units: Map<String, J> = Map::new();
    scale.insert("t".into(), int_or(valid(pixdim[4]).then_some(pixdim[4]), 1));
    scale.insert("c".into(), json!(1));
    if let (true, Some(u)) = (valid(pixdim[4]), time) {
        units.insert("t".into(), json!(u));
    }
    for (k, a) in ["x", "y", "z"].iter().enumerate() {
        let v = placed.map(|p| p.0[k]).unwrap_or(pixdim[k + 1]);
        scale.insert(a.to_string(), int_or(valid(v).then_some(v), 1));
        if let (true, Some(u)) = (valid(v), space) {
            units.insert(a.to_string(), json!(u));
        }
    }

    // channels (§4.4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nifti.md#44-channels>)
    let mut channels = None;
    if color {
        let window = full_window(json!(0), json!(255));
        channels = Some(RGBA[..samples as usize].iter().map(|(l, k)| omero_channel(l, *k, Some(&window))).collect::<Vec<J>>());
    } else {
        let (mut lo, mut hi) = (f("cal_min"), f("cal_max"));
        if lo.is_finite() && hi.is_finite() && hi > lo && (c as i64) <= MAX_CHANNELS {
            if let Some((s, i)) = scaling {
                let (a, b) = ((lo - i) / s, (hi - i) / s);
                (lo, hi) = if b < a { (b, a) } else { (a, b) };
            }
            if lo.is_finite() && hi.is_finite() {
                let window = full_window(json!(lo), json!(hi));
                channels = Some((0..c).map(|k| omero_channel(&format!("C{k}"), "FFFFFF", Some(&window))).collect());
            }
        }
    }

    // the array (§4.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nifti.md#41-the-array>)
    let Cut { axes, stored, shape: stored_shape, item, grid, chunks } = cut(&h, vox, facts)?;
    let shape_of = |a: &str| stored_shape[stored.iter().position(|s| *s == a).unwrap()];
    let pos = |a: &str| stored.iter().position(|s| *s == a).unwrap();
    let chunk_shape: Vec<u64> = axes.iter().map(|a| grid[pos(a)]).collect();
    let shape: Vec<u64> = axes.iter().map(|a| shape_of(a)).collect();
    let translation = placed.map(|p| super::translation(&axes, p.1.map(|v| json!(v))));
    let scales = J::Array(axes.iter().map(|a| scale[*a].clone()).collect());
    let trs = translation.clone().map(|t| vec![t]);
    let mut ome = image_ome(&axes, &units, &[scales], None, trs.as_deref())?;
    if let Some(ch) = channels {
        ome["omero"] = omero_json(ch);
    }
    let endian = if h.little { "little" } else { "big" };
    let mut own = Map::new();
    own.insert("nifti_version".into(), json!(version));
    own.insert("byte_order".into(), json!(endian));
    own.insert("header".into(), header_json(&h));
    if nontrivial {
        let (s, i) = scaling.unwrap();
        own.insert("scaling".into(), json!({"slope": s, "inter": i}));
    }
    if let Some(a) = affine {
        own.insert("affine".into(), json!({"form": a, "applied": placed.is_some()}));
    }
    let mut out = Out::new(url);
    out.json("zarr.json", &root_json(ome, "nifti", url, Some(J::Object(own))));
    let mut codecs = Vec::new();
    if stored != axes {
        let order: Vec<usize> = stored.iter().map(|a| axes.iter().position(|b| b == a).unwrap()).collect();
        codecs.push(json!({"name": "transpose", "configuration": {"order": order}}));
    }
    codecs.push(bytes_codec(item, endian));
    out.json("0/zarr.json", &array_json(&shape, data_type, json!(chunk_shape), J::Array(codecs), &axes));
    let count = chunks.len();
    let mut refs = Refs::new(ir, out);
    // the chunks' coordinates in the array's axis order
    contiguous::emit(&mut refs, chunks, (vox, length, form), copied, &["0/c/"], |c| axes.iter().map(|a| c[pos(a)]).collect())?;
    let mut out = refs.out;
    let sizes: Map<String, J> = axes.iter().map(|a| (a.to_string(), json!(shape_of(a)))).collect();
    out.summary = json!({
        "version": version, "byteOrder": endian, "sizes": sizes, "dataType": data_type, "color": color,
        "chunkShape": chunk_shape, "chunks": count,
        "scaling": if nontrivial { let (s, i) = scaling.unwrap(); json!({"slope": s, "inter": i}) } else { J::Null },
        "affine": affine, "translation": translation.is_some(), "extensions": facts["extended"],
    });
    Ok(out)
}
