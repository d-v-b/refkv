//! The QPTIFF image projection (spec/zarrify/qptiff.md §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/qptiff.md#4-the-image>) from a valid IR:
//! one array per level, whose chunks are the tiles, or the strips, of each channel's IFD (a
//! short last strip padded to the chunk by the plan's `pad`: a zero data source, or a literal
//! tail spliced into its LZW stream); the plan holds the format, the channels' names and
//! colors (its `omero`), sizes, scales and translation; the windows of its `omero` are sampled
//! from the coarsest level's chunks, which the run reads (`reads`).

use super::tiled::{self, MAX_NAME};
use super::{arr, n, strs, window, Copied};
use crate::ir::Ir;
use crate::tiff::plan::plan;
use crate::out::{omero_channel, omero_json, root_json, sample_channels, type_window, Out};
use serde_json::{json, Value as J};

/// A channel's omero color from its QPI `Color` (three decimal bytes, `r,g,b`), else white.
fn color(v: &J) -> String {
    let parts: Option<Vec<u8>> = v
        .as_str()
        .map(|s| s.split(',').map(|p| p.trim_matches([' ', '\t', '\r', '\n']).parse::<u8>().ok()).collect::<Option<Vec<u8>>>())
        .unwrap_or(None);
    match parts {
        Some(p) if p.len() == 3 => format!("{:02X}{:02X}{:02X}", p[0], p[1], p[2]),
        _ => "FFFFFF".into(),
    }
}

/// A channel's QPI `Name` when it is present, not empty and at most 256 bytes.
fn label(v: &J) -> Option<&str> {
    v.as_str().filter(|s| !s.is_empty() && s.len() <= MAX_NAME)
}

/// The `omero` member (spec/zarrify/qptiff.md §4.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/qptiff.md#43-arrays>): one channel per
/// channel image, or, for an RGB image, one per sample; channel `c`'s window `windows[c]` (sampled
/// from the data, else an integer type's whole range), and none when it has neither.
fn omero(channels: &[J], spp: u64, windows: &[Option<J>]) -> J {
    if spp > 1 {
        let base = channels.first().and_then(|c| label(&c["name"])).map(|s| format!("{s} ")).unwrap_or_default();
        return omero_json(sample_channels(&base, spp, spp == 3, windows));
    }
    let mut out = Vec::new();
    for (i, c) in channels.iter().enumerate() {
        let l = label(&c["name"]).map(str::to_string).unwrap_or_else(|| format!("C{i}"));
        out.push(omero_channel(&l, color(&c["color"]), windows.get(i).and_then(Option::as_ref)));
    }
    omero_json(out)
}

/// The channel count: the samples of an image with more than one, else the channel images.
fn channel_count(planned: &J) -> u64 {
    let spp = n(&planned["spp"]);
    if spp > 1 { spp } else { arr(&planned["channels"]).len() as u64 }
}

/// A level's shape and chunk shape (along the plan's axes).
fn level_shape(planned: &J, lv: &J) -> (Vec<u64>, Vec<u64>) {
    let spp = n(&planned["spp"]);
    let c_chunk = if spp > 1 && n(&planned["planar"]) == 1 { spp } else { 1 };
    let (mut shape, mut chunk) = (Vec::with_capacity(3), Vec::with_capacity(3));
    if strs(&planned["axes"]).first() == Some(&"c") {
        shape.push(channel_count(planned));
        chunk.push(c_chunk);
    }
    shape.extend([n(&lv["h"]), n(&lv["w"])]);
    chunk.extend([n(&lv["th"]), n(&lv["tw"])]);
    (shape, chunk)
}

/// The chunks the `omero` windows sample (spec/zarrify/qptiff.md §4.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/qptiff.md#43-arrays>): one per
/// channel of the coarsest level, unless the image is RGB (three samples) or the core does not
/// decode its compression.
fn samples(planned: &J) -> Vec<window::Sample> {
    let levels = arr(&planned["levels"]);
    let Some(last) = levels.len().checked_sub(1) else { return Vec::new() };
    if n(&planned["spp"]) == 3 || !window::decodable(n(&planned["compression"])) {
        return Vec::new();
    }
    let (shape, chunk) = level_shape(planned, &levels[last]);
    let axes = strs(&planned["axes"]);
    let count = if axes.first() == Some(&"c") { channel_count(planned) } else { 1 };
    window::samples(&format!("{last}/c/"), &axes, &shape, &chunk, count, n(&planned["bits"]) / 8)
}

/// The source's ranges the windows sample (`samples`): their chunks' bytes, which the run
/// reads after the parser.
pub fn reads(ir: &Ir, description: &J) -> Result<Vec<(u64, u64)>, String> {
    let planned = &plan("qptiff", description)?;
    tiled::reads(ir, planned, &samples(planned), arrays)
}

/// The levels' arrays and chunks (every level's, or only level `only`'s), and their shapes: a
/// level's chunks are the tiles, or the strips, of each channel's IFD.
fn arrays(ir: &Ir, planned: &J, url: &str, only: Option<usize>) -> Result<(Out, Vec<Vec<u64>>), String> {
    let has_c = strs(&planned["axes"]).first() == Some(&"c");
    // a short last strip's zero padding: a prefix of the output's zero data source
    let mut zeros = tiled::zero_source(planned);
    tiled::arrays(ir, planned, url, only, |lv| level_shape(planned, lv), |refs, li, lv| {
        let (w, h, tw, th) = (n(&lv["w"]), n(&lv["h"]), n(&lv["tw"]), n(&lv["th"]));
        let strips = lv["strips"].as_bool().unwrap_or(false);
        let down = h.div_ceil(th);
        let across = if strips { 1 } else { w.div_ceil(tw) };
        // a short last strip, padded to the chunk (spec/zarrify/qptiff.md §4.2
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/qptiff.md#42-tiles-and-strips>)
        let padding = tiled::Padding::of(refs, lv, &mut zeros)?;
        let prefix = format!("{li}/c/");
        for (c, p) in arr(&lv["planes"]).iter().enumerate() {
            let Some(e) = p.as_u64() else { continue };
            let c = c as u64;
            let coords = |s, row, col| {
                let mut coords = Vec::with_capacity(3);
                if has_c {
                    coords.push(tiled::channel(planned, s, c));
                }
                coords.extend([row, col]);
                coords
            };
            let parts = |k, row, start, length| padding.parts(c, k, row, start, length);
            tiled::plane(refs, &prefix, Some(e), across * down, across, coords, parts)?;
        }
        Ok(())
    })
}

/// The QPTIFF image: one array per level, its chunks the tiles or strips of each channel.
/// `copied` holds the bytes of the chunks the windows sample (`reads`).
pub fn project(ir: &Ir, description: &J, url: &str, copied: &Copied) -> Result<Out, String> {
    let planned = &plan("qptiff", description)?;
    let channels = arr(&planned["channels"]);
    let axes = strs(&planned["axes"]);
    let data_type = tiled::data_type(planned)?;
    let (mut out, shapes) = arrays(ir, planned, url, None)?;
    let sampled = tiled::sampled(&out, planned, &samples(planned), |lv| level_shape(planned, lv).1, &data_type, copied);
    let typed = type_window(&data_type);
    let windows: Vec<Option<J>> = (0..channel_count(planned) as usize).map(|c| sampled.get(c).cloned().flatten().or_else(|| typed.clone())).collect();
    let mut ome = tiled::ome(planned, &axes, Some(&planned["name"]))?;
    ome["omero"] = omero(channels, n(&planned["spp"]), &windows);
    out.json("zarr.json", &root_json(ome, "qptiff", url, Some(tiled::own(planned))));
    out.summary = json!({"axes": axes, "levels": shapes, "channels": channels.len(), "references": out.refs.len()});
    Ok(out)
}
