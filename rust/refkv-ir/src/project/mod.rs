//! The image projections (design/ARCHITECTURE.md §3.4
//! <https://github.com/d-v-b/refkv/blob/main/design/ARCHITECTURE.md#34-projections>): a valid IR and
//! its description into
//! today's Zarr hierarchy (spec/zarrify/tiff.md §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#4-the-image>,
//! spec/zarrify/czi.md §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#4-output>,
//! spec/zarrify/nd2.md §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nd2.md#4-the-image>, and ND2's root
//! source metadata of §5.1
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nd2.md#51-decoded-chunks>). A
//! projection checks nothing (the parser checked the schema)
//! and reads nothing (the IR holds what it needs).

mod audio;
mod contiguous;
pub mod hdf5_nodes;
pub mod store;
pub mod zarr2_nodes;
pub mod zarr3_nodes;
mod tiled;
pub(crate) use tiled::MAX_NAME;
mod window;

// Every profile's projection (src/project/<profile>.rs), by the registry (profiles/; build.rs)
include!(concat!(env!("OUT_DIR"), "/projections.rs"));

use crate::fed::uint;
use crate::ir::Ir;
use crate::out::{Chunking, Out};
use crate::record::{Header, Num, width};
use crate::text::text_json;
use crate::run::Format;
use serde_json::{Map, Value as J};
use std::fmt::Write;

/// The bytes of the source's ranges a projection copies into the archive, by (offset, length).
pub type Copied = std::collections::HashMap<(u64, u64), Vec<u8>>;

/// An edge chunk refkv's output holds as bytes, of a source range and its padding, which a
/// convention's hierarchy routine gives apart rather than reading it: its key, the range's offset
/// and length, and the padding's length.
pub type CopiedChunk = (String, u64, u64, u64);

/// The image projection of `format`: today's hierarchy, without `refkv_source`, from the IR and
/// the parser's description. `copied` holds the bytes of the ranges `reads` gives for the same
/// `chunking`, and `layouts` names the layouts written beside the image (None: every one).
pub fn project(format: Format, ir: &Ir, description: &J, url: &str, copied: &Copied, layouts: Option<&[String]>,
               chunking: &Chunking) -> Result<Out, String> {
    format.profile().project_layouts(ir, description, url, copied, layouts, chunking)
}

/// The source's ranges the projection of `format` copies into the archive (NIfTI's and
/// MRC's edge chunks whose padding does not fit in a reference) or decodes (the TIFF and
/// QPTIFF chunks their `omero` windows sample), which the run reads after the parser: a
/// projection reads nothing itself.
pub fn reads(format: Format, ir: &Ir, description: &J, chunking: &Chunking) -> Result<Vec<(u64, u64)>, String> {
    format.profile().reads(ir, description, chunking)
}

/// The source's ranges the OME image of `profile` (`tiff`, `qptiff`, `ndpi` or `ims`, `ome_image`)
/// samples for its `omero` windows, which a host reads before it asks for the image; none of an
/// image across files, whose store run read them, and none of an NDPI's or an IMS input's. An
/// error where refkv's plan rejects the input.
pub fn ome_image_reads(profile: &str, ir: &Ir, description: &J) -> Result<Vec<(u64, u64)>, String> {
    match profile {
        "qptiff" => qptiff::reads(ir, description),
        "ndpi" => crate::tiff::plan::plan("ndpi", description).map(|_| Vec::new()),
        "ims" => Ok(Vec::new()),
        _ if description.get("files").is_some() => Ok(Vec::new()),
        _ => tiff::reads(ir, description),
    }
}

/// The OME-Zarr 0.5 image refkv's convention writes at the root of a TIFF, QPTIFF, NDPI or IMS
/// input (spec/zarrify/tiff.md §4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#4-the-image>,
/// spec/zarrify/qptiff.md §4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/qptiff.md#4-the-image>,
/// spec/zarrify/ndpi.md §4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#4-the-image>,
/// spec/zarrify/ims.md §4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ims.md#4-output>),
/// as refkv's own output presents it but for the IR mirror; of the TIFF family, made from the
/// parser's description through refkv's plan (`tiff::plan::plan`). Its nodes: the root group, with
/// its attributes without refkv's declarations and its source metadata (`own`), then each level's
/// array, by path; and its documents and chunks, the arrays sharded as the plan asks
/// (`shard::apply_all`, ranges joined across inner chunks unless `checksums`). `copied` holds the
/// bytes of the ranges `ome_image_reads` gives. The root's document is declared for the URL `""`; a
/// caller declares it again for its own.
pub fn ome_image(profile: &str, ir: &Ir, description: &J, copied: &Copied, checksums: bool) -> Result<(Vec<J>, Out), String> {
    let mut out = match profile {
        "ndpi" => ndpi::project(ir, description, "")?,
        "qptiff" => qptiff::project(ir, description, "", copied)?,
        "ims" => ims::project(ir, description, "")?,
        "tiff" if description.get("files").is_some() => tiff::project_set(ir, description, "", copied)?,
        "tiff" => tiff::project(ir, description, "", copied)?,
        other => return Err(format!("internal: no OME image of a {other} input")),
    };
    crate::shard::apply_all(&mut out, !checksums)?;
    let mut nodes = Vec::new();
    for (key, bytes) in &out.entries {
        let Some(path) = key.strip_suffix("zarr.json").map(|p| p.trim_end_matches('/')) else { continue };
        let doc: J = serde_json::from_slice(bytes).map_err(|e| format!("internal: {e}"))?;
        let mut attributes = doc["attributes"].as_object().cloned().unwrap_or_default();
        let own = attributes.remove(crate::out::CONVENTION_KEY).and_then(|v| v.get(profile).cloned()).unwrap_or(J::Null);
        attributes.remove("zarr_conventions");
        let mut node = serde_json::json!({"path": path, "attributes": attributes, "own": own});
        if doc["node_type"] == "array" {
            node["array"] = serde_json::json!({});
        }
        nodes.push(node);
    }
    // the root first, then the levels in order
    nodes.sort_by_key(|n| n["path"].as_str().map(|p| (p.len(), p.to_string())));
    Ok((nodes, out))
}

// ---- reading the descriptions and plans (JSON)

/// A fixed-size character field: its bytes up to the first NUL, as a text value
/// (conventions §6
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#6-source-metadata-as-json>):
/// UTF-8 if valid, else {"latin1": ...}.
pub(crate) fn json_text(b: &[u8]) -> J {
    text_json(&b[..b.iter().position(|&c| c == 0).unwrap_or(b.len())])
}

/// Opaque bytes as a source metadata member (conventions §6
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#6-source-metadata-as-json>):
/// their base64 encoding.
fn base64(b: &[u8]) -> String {
    crate::mirror::base64(b)
}

/// A float header value as the source metadata writes it (conventions §6
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#6-source-metadata-as-json>):
/// a number, "NaN", "Infinity" or "-Infinity", or {"bits": hex} (its IEEE 754 bits, most
/// significant first, as 2 * `width` lowercase hexadecimal digits) for a negative zero or
/// a NaN other than the canonical one, whose bits a JSON number or "NaN" does not keep.
pub(crate) fn float_json(bits: u64, width: usize, v: f64) -> J {
    let canonical = if width == 4 { 0x7fc0_0000 } else { 0x7ff8_0000_0000_0000 };
    if (v == 0.0 && v.is_sign_negative()) || (v.is_nan() && bits != canonical) {
        serde_json::json!({"bits": format!("{bits:0w$x}", w = 2 * width)})
    } else if v.is_nan() {
        J::String("NaN".into())
    } else if v.is_infinite() {
        J::String(if v > 0.0 { "Infinity".into() } else { "-Infinity".into() })
    } else {
        serde_json::json!(v)
    }
}

/// A count or index of a description or plan (an integer, or a number written as a float).
/// A header value as the root's source metadata writes it (spec/zarrify/mrc.md §5
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mrc.md#5-source-metadata>,
/// spec/zarrify/nifti.md §5
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nifti.md#5-source-metadata>): an
/// integer exactly, whatever its magnitude; a float as a number, "NaN", "Infinity" or
/// "-Infinity", or {"bits": hex} for a negative zero or a NaN other than the canonical one.
fn header_value(h: &Header, at: usize, kind: &str) -> J {
    match h.number(at, kind) {
        Num::Int(v) => J::from(v),
        Num::Float(v) => {
            let w = width(kind);
            float_json(uint(h.raw, at, w, h.little), w, v)
        }
    }
}

/// Every field of a header, in its table's order: `header` of the root's source metadata
/// (texts as text, opaque bytes as base64, labels as a list of texts).
pub(crate) fn header_json(h: &Header) -> J {
    let mut out = Map::new();
    for &(name, at, kind, count) in h.fields {
        let v = match kind {
            "c" => json_text(h.bytes(name)),
            "b" => J::from(base64(h.bytes(name))),
            "l" => J::Array(h.bytes(name).chunks(80).map(json_text).collect()),
            _ if count == 1 => header_value(h, at, kind),
            _ => J::Array((0..count).map(|k| header_value(h, at + k * width(kind), kind)).collect()),
        };
        out.insert(name.into(), v);
    }
    J::Object(out)
}

fn n(v: &J) -> u64 {
    v.as_u64().or_else(|| v.as_f64().map(|f| f as u64)).unwrap_or(0)
}

/// An array of a description or plan (empty when absent).
fn arr(v: &J) -> &[J] {
    v.as_array().map(|a| a.as_slice()).unwrap_or(&[])
}

/// An object of a description or plan (empty when absent).
fn obj(v: &J) -> Map<String, J> {
    v.as_object().cloned().unwrap_or_default()
}

/// Axis names.
fn strs(v: &J) -> Vec<&str> {
    arr(v).iter().filter_map(|a| a.as_str()).collect()
}

/// A JSON value's truth as Python's.
fn truthy(v: &J) -> bool {
    match v {
        J::Null => false,
        J::Bool(b) => *b,
        J::Number(x) => x.as_f64().is_some_and(|f| f != 0.0),
        J::String(s) => !s.is_empty(),
        J::Array(a) => !a.is_empty(),
        J::Object(o) => !o.is_empty(),
    }
}

/// `translations` as `image_ome` takes them: None when absent or empty.
fn translations(v: &J) -> Option<&[J]> {
    v.as_array().filter(|a| !a.is_empty()).map(|a| a.as_slice())
}

/// The value along axis `a` of the ones along `t`, `c`, `z`, `y` and `x` (any other axis: `x`'s).
fn along<T>(a: &str, [t, c, z, y, x]: [T; 5]) -> T {
    match a {
        "t" => t,
        "c" => c,
        "z" => z,
        "y" => y,
        _ => x,
    }
}

/// An image's axes: `t`, `c` and `z` when present, then `y` and `x`.
fn image_axes(t: bool, c: bool, z: bool) -> Vec<&'static str> {
    let mut axes: Vec<&'static str> = [("t", t), ("c", c), ("z", z)].iter().filter(|a| a.1).map(|a| a.0).collect();
    axes.extend(["y", "x"]);
    axes
}

/// A translation along `axes`: `x`, `y` and `z` along those, 0 along any other.
fn translation(axes: &[&str], [x, y, z]: [J; 3]) -> J {
    J::Array(axes.iter().map(|a| match *a {
        "x" => x.clone(),
        "y" => y.clone(),
        "z" => z.clone(),
        _ => serde_json::json!(0),
    }).collect())
}

/// A store projection's summary with refkv's plan's `counts` after its own members.
fn with_counts(mut summary: J, plan: &J) -> J {
    if let (Some(m), Some(c)) = (summary.as_object_mut(), plan["counts"].as_object()) {
        m.extend(c.clone());
    }
    summary
}

/// A chunk key: `prefix` and the coordinates, joined by "/".
fn key(prefix: &str, coords: impl IntoIterator<Item = u64>) -> String {
    let mut k = String::from(prefix);
    for (i, c) in coords.into_iter().enumerate() {
        if i > 0 {
            k.push('/');
        }
        let _ = write!(k, "{c}");
    }
    k
}
