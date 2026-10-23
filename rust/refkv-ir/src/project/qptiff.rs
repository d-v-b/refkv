//! The QPTIFF image projection (spec/virtualize/qptiff.md §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/qptiff.md#4-the-image>) from a valid IR:
//! one array per level, whose chunks are the tiles, or the strips, of each channel's IFD (a
//! short last strip padded to the chunk by the facts' `pad`: a zero data source, or a literal
//! tail spliced into its LZW stream); the facts hold the format, the channels' names and
//! colors (its `omero`), sizes, scales and translation; the windows of its `omero` are sampled
//! from the coarsest level's chunks, which the run reads (`reads`).

use super::{arr, key, n, obj, strs, translations, truthy, window, Copied};
use crate::ir::Ir;
use crate::out::{array_json, image_ome, omero_channel, omero_json, root_json, sample_channels, tiff_codecs, transpose_codec, type_window, Out, Part};
use crate::refs::Refs;
use serde_json::{json, Value as J};
use std::collections::HashMap;

/// The longest channel name taken for a label.
const MAX_NAME: usize = 256;

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

/// The `omero` member (spec/virtualize/qptiff.md §4.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/qptiff.md#43-arrays>): one channel per
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

/// The data type of the samples (the TIFF convention's).
fn data_type(facts: &J) -> Result<String, String> {
    let kind = match n(&facts["sample_format"]) {
        1 => "uint",
        2 => "int",
        3 => "float",
        f => return Err(format!("internal: sample format {f}")),
    };
    Ok(format!("{kind}{}", n(&facts["bits"])))
}

/// The channel count: the samples of an image with more than one, else the channel images.
fn channel_count(facts: &J) -> u64 {
    let spp = n(&facts["spp"]);
    if spp > 1 { spp } else { arr(&facts["channels"]).len() as u64 }
}

/// A level's shape and chunk shape (along the facts' axes).
fn level_shape(facts: &J, lv: &J) -> (Vec<u64>, Vec<u64>) {
    let spp = n(&facts["spp"]);
    let c_chunk = if spp > 1 && n(&facts["planar"]) == 1 { spp } else { 1 };
    let (mut shape, mut chunk) = (Vec::with_capacity(3), Vec::with_capacity(3));
    if strs(&facts["axes"]).first() == Some(&"c") {
        shape.push(channel_count(facts));
        chunk.push(c_chunk);
    }
    shape.extend([n(&lv["h"]), n(&lv["w"])]);
    chunk.extend([n(&lv["th"]), n(&lv["tw"])]);
    (shape, chunk)
}

/// The chunks the `omero` windows sample (spec/virtualize/qptiff.md §4.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/qptiff.md#43-arrays>): one per
/// channel of the coarsest level, unless the image is RGB (three samples) or the core does not
/// decode its compression.
fn samples(facts: &J) -> Vec<window::Sample> {
    let levels = arr(&facts["levels"]);
    let Some(last) = levels.len().checked_sub(1) else { return Vec::new() };
    if n(&facts["spp"]) == 3 || !window::decodable(n(&facts["compression"])) {
        return Vec::new();
    }
    let (shape, chunk) = level_shape(facts, &levels[last]);
    let axes = strs(&facts["axes"]);
    let count = if axes.first() == Some(&"c") { channel_count(facts) } else { 1 };
    window::samples(&format!("{last}/c/"), &axes, &shape, &chunk, count, n(&facts["bits"]) / 8)
}

/// The source's ranges the windows sample (`samples`): their chunks' bytes, which the run
/// reads after the parser.
pub fn reads(ir: &Ir, facts: &J) -> Result<Vec<(u64, u64)>, String> {
    let s = samples(facts);
    if s.is_empty() {
        return Ok(Vec::new());
    }
    let last = arr(&facts["levels"]).len() - 1;
    Ok(window::reads(&arrays(ir, facts, "", Some(last))?.0, &s))
}

/// The levels' arrays and chunks (every level's, or only level `only`'s), and their shapes.
fn arrays(ir: &Ir, facts: &J, url: &str, only: Option<usize>) -> Result<(Out, Vec<Vec<u64>>), String> {
    let little = facts["little"].as_bool().unwrap_or(false);
    let (spp, planar, bits) = (n(&facts["spp"]), n(&facts["planar"]), n(&facts["bits"]));
    let axes = strs(&facts["axes"]);
    let has_c = axes.first() == Some(&"c");
    let contig = spp > 1 && planar == 1;
    let data_type = data_type(facts)?;
    let mut codecs = if contig { vec![transpose_codec(&axes)] } else { vec![] };
    codecs.extend(tiff_codecs(n(&facts["compression"]), bits, little));
    let codecs = J::Array(codecs);
    let mut refs = Refs::new(ir, Out::new(url));
    let levels = arr(&facts["levels"]);
    let mut shapes = Vec::with_capacity(levels.len());
    for (li, lv) in levels.iter().enumerate() {
        if only.is_some_and(|l| l != li) {
            continue;
        }
        let (w, h, tw, th) = (n(&lv["w"]), n(&lv["h"]), n(&lv["tw"]), n(&lv["th"]));
        let strips = lv["strips"].as_bool().unwrap_or(false);
        let down = h.div_ceil(th);
        let across = if strips { 1 } else { w.div_ceil(tw) };
        let (shape, chunk) = level_shape(facts, lv);
        refs.out.json(&format!("{li}/zarr.json"), &array_json(&shape, &data_type, json!(chunk), codecs.clone(), &axes));
        shapes.push(shape);
        // a short last strip, padded to the chunk (spec/virtualize/qptiff.md §4.2
        // <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/qptiff.md#42-tiles-and-strips>)
        let pad = &lv["pad"];
        let zeros = pad["zeros"].as_u64().map(|z| refs.out.shared(&vec![0u8; z as usize]));
        let lzw: HashMap<(u64, u64), (u64, Vec<u8>)> = arr(&pad["lzw"])
            .iter()
            .map(|p| ((n(&p["c"]), n(&p["k"])), (n(&p["prefix"]), arr(&p["tail"]).iter().map(|b| n(b) as u8).collect())))
            .collect();
        let per = across * down;
        let prefix = format!("{li}/c/");
        for (c, p) in arr(&lv["planes"]).iter().enumerate() {
            let Some(e) = p.as_u64() else { continue };
            let mut members = ir.members(e as u32);
            members.sort_unstable();
            for (k, start, length, form) in members {
                let (s, j) = (k / per, k % per);
                let mut coords = Vec::with_capacity(3);
                if has_c {
                    coords.push(if spp > 1 { if contig { 0 } else { s } } else { c as u64 });
                }
                coords.extend([j / across, j % across]);
                let parts = if strips && j == down - 1 {
                    if let Some(z) = &zeros {
                        Some(vec![Part::Src(start, length), z.clone()])
                    } else {
                        lzw.get(&(c as u64, k)).map(|(p, tail)| vec![Part::Src(start, *p), Part::Lit(tail.clone())])
                    }
                } else {
                    None
                };
                refs.chunk(key(&prefix, coords), start, length, form, parts, 0)?;
            }
        }
    }
    Ok((refs.out, shapes))
}

/// The QPTIFF image: one array per level, its chunks the tiles or strips of each channel.
/// `copied` holds the bytes of the chunks the windows sample (`reads`).
pub fn project(ir: &Ir, facts: &J, url: &str, copied: &Copied) -> Result<Out, String> {
    let little = facts["little"].as_bool().unwrap_or(false);
    let channels = arr(&facts["channels"]);
    let axes = strs(&facts["axes"]);
    let levels = arr(&facts["levels"]);
    let data_type = data_type(facts)?;
    let (mut out, shapes) = arrays(ir, facts, url, None)?;
    let samples = samples(facts);
    let sampled = match levels.last() {
        Some(lv) if !samples.is_empty() => {
            let size = level_shape(facts, lv).1.iter().product::<u64>() * (n(&facts["bits"]) / 8);
            window::windows(&out, &samples, copied, n(&facts["compression"]), &data_type, little, size)
        }
        _ => Vec::new(),
    };
    let typed = type_window(&data_type);
    let windows: Vec<Option<J>> = (0..channel_count(facts) as usize).map(|c| sampled.get(c).cloned().flatten().or_else(|| typed.clone())).collect();
    let tr = &facts["translation"];
    let trs = J::Array(if truthy(tr) { vec![tr.clone(); levels.len()] } else { vec![] });
    let mut ome = image_ome(&axes, &obj(&facts["units"]), arr(&facts["scales"]), Some(&facts["name"]), translations(&trs))?;
    ome["omero"] = omero(channels, n(&facts["spp"]), &windows);
    let own = json!({"byte_order": if little { "little" } else { "big" }, "bigtiff": facts["bigtiff"]});
    out.json("zarr.json", &root_json(ome, "qptiff", url, Some(own)));
    out.summary = json!({"axes": axes, "levels": shapes, "channels": channels.len(), "references": out.refs.len()});
    Ok(out)
}
