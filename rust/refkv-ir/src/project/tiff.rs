//! The TIFF image projection (spec/virtualize/tiff.md §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/tiff.md#4-the-image>) from a valid IR:
//! each level's planes name the IR's tiles; the facts hold the format, sizes, scales,
//! translation, and the OME-XML's channel names and colors (its `omero`); the windows of its
//! `omero` are sampled from the coarsest level's chunks, which the run reads (`reads`).

use super::{arr, key, n, obj, strs, translations, truthy, window, Copied};
use crate::ir::Ir;
use crate::out::{array_json, image_ome, omero_channel, omero_json, root_json, sample_channels, tiff_codecs, transpose_codec, type_window, Out};
use crate::refs::Refs;
use crate::tiff::MAX_OMERO;
use serde_json::{json, Value as J};

/// The longest channel name taken for a label.
const MAX_NAME: usize = 256;

/// A channel's omero color from its OME-XML `Color` (a signed 32-bit integer, `RRGGBBAA`), else
/// white.
fn color(v: &J) -> String {
    match v.as_str().and_then(|s| s.trim_matches([' ', '\t', '\r', '\n']).parse::<i32>().ok()) {
        Some(c) => format!("{:06X}", (c as u32) >> 8),
        None => "FFFFFF".into(),
    }
}

/// Whether the samples decode to RGB: PhotometricInterpretation 2, or 6 with JPEG tiles.
fn rgb(facts: &J) -> bool {
    let (interpretation, compression) = (n(&facts["interpretation"]), n(&facts["compression"]));
    n(&facts["spp"]) > 1 && (interpretation == 2 || (interpretation == 6 && compression == 7))
}

/// The `omero` member (spec/virtualize/tiff.md §4.4
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/tiff.md#44-arrays>), when there are
/// at most 64 channels and each has a window: the samples, `R`, `G`, `B` when they decode to
/// RGB; else one per channel, named and colored by the OME-XML's Channels. Channel `c`'s window
/// is `sampled[c]`, sampled from the data, else the type's whole range (an integer type's).
fn omero(facts: &J, size_c: u64, data_type: &str, sampled: &[Option<J>]) -> Option<J> {
    if size_c > MAX_OMERO {
        return None;
    }
    let typed = type_window(data_type);
    let windows: Vec<Option<J>> = (0..size_c as usize).map(|c| sampled.get(c).cloned().flatten().or_else(|| typed.clone())).collect();
    if windows.iter().any(Option::is_none) {
        return None;
    }
    let ome = arr(&facts["ome"]["channels"]);
    let name = |c: u64| ome.get(c as usize).and_then(|o| o["name"].as_str()).filter(|s| !s.is_empty() && s.len() <= MAX_NAME);
    let spp = n(&facts["spp"]);
    if spp > 1 {
        let base = name(0).map(|s| format!("{s} ")).unwrap_or_default();
        return Some(omero_json(sample_channels(&base, spp, rgb(facts), &windows)));
    }
    let channels = (0..size_c).map(|c| {
        let label = name(c).map(str::to_string).unwrap_or_else(|| format!("C{c}"));
        omero_channel(&label, color(ome.get(c as usize).map_or(&J::Null, |o| &o["color"])), windows[c as usize].as_ref())
    });
    Some(omero_json(channels.collect()))
}

/// The data type of the samples (spec/virtualize/tiff.md §4.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/tiff.md#43-data-types-and-codecs>).
fn data_type(facts: &J) -> Result<String, String> {
    let kind = match n(&facts["sample_format"]) {
        1 => "uint",
        2 => "int",
        3 => "float",
        f => return Err(format!("internal: sample format {f}")),
    };
    Ok(format!("{kind}{}", n(&facts["bits"])))
}

/// The axes, and a level's shape and chunk shape along them.
fn level_shape<'a>(facts: &'a J, lv: &J) -> (Vec<&'a str>, Vec<u64>, Vec<u64>) {
    let spp = n(&facts["spp"]);
    let contig = spp > 1 && n(&facts["planar"]) == 1;
    let sizes = &facts["sizes"];
    let (w, h, tw, th) = (n(&lv["w"]), n(&lv["h"]), n(&lv["tw"]), n(&lv["th"]));
    let axes = strs(&facts["axes"]);
    let of = |a: &str, t: u64, c: u64, z: u64, y: u64, x: u64| match a {
        "t" => t,
        "c" => c,
        "z" => z,
        "y" => y,
        _ => x,
    };
    let shape = axes.iter().map(|a| of(a, n(&sizes["t"]), n(&sizes["c"]), n(&sizes["z"]), h, w)).collect();
    let chunk = axes.iter().map(|a| of(a, 1, if contig { spp } else { 1 }, 1, th, tw)).collect();
    (axes, shape, chunk)
}

/// The chunks the `omero` windows sample (spec/virtualize/tiff.md §4.4
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/tiff.md#44-arrays>): one per
/// channel of the coarsest level, when the image has at most 64 channels, its samples do not
/// decode to RGB, and the core decodes its compression; else none.
fn samples(facts: &J) -> Vec<window::Sample> {
    let size_c = n(&facts["sizes"]["c"]);
    let levels = arr(&facts["levels"]);
    let Some(last) = levels.len().checked_sub(1) else { return Vec::new() };
    if size_c > MAX_OMERO || rgb(facts) || !window::decodable(n(&facts["compression"])) {
        return Vec::new();
    }
    let (axes, shape, chunk) = level_shape(facts, &levels[last]);
    window::samples(&format!("{last}/c/"), &axes, &shape, &chunk, size_c, n(&facts["bits"]) / 8)
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
    let sizes = &facts["sizes"];
    let (size_t, size_c, size_z) = (n(&sizes["t"]), n(&sizes["c"]), n(&sizes["z"]));
    let plane_c = n(&facts["plane_c"]);
    let contig = spp > 1 && planar == 1;
    let data_type = data_type(facts)?;
    let mut codecs = if contig { vec![transpose_codec(&strs(&facts["axes"]))] } else { vec![] };
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
        let (axes, shape, chunk) = level_shape(facts, lv);
        refs.out.json(&format!("{li}/zarr.json"), &array_json(&shape, &data_type, json!(chunk), codecs.clone(), &axes));
        shapes.push(shape);
        let across = w.div_ceil(tw);
        let per = across * h.div_ceil(th);
        let planes = arr(&lv["planes"]);
        let prefix = format!("{li}/c/");
        for t in 0..size_t {
            for c in 0..plane_c {
                for z in 0..size_z {
                    let tiles = planes.get(((t * plane_c + c) * size_z + z) as usize).and_then(|v| v.as_u64());
                    let mut members = tiles.map(|e| ir.members(e as u32)).unwrap_or_default();
                    members.sort_unstable();
                    for (k, start, length, form) in members {
                        let (s, j) = (k / per, k % per);
                        let mut coords = Vec::with_capacity(5);
                        if size_t > 1 {
                            coords.push(t);
                        }
                        if size_c > 1 {
                            coords.push(if spp > 1 { if contig { 0 } else { s } } else { c });
                        }
                        if size_z > 1 {
                            coords.push(z);
                        }
                        coords.extend([j / across, j % across]);
                        refs.chunk(key(&prefix, coords), start, length, form, None, 0)?;
                    }
                }
            }
        }
    }
    Ok((refs.out, shapes))
}

/// The TIFF image: one array per level, its chunks the tiles of each plane. `copied` holds the
/// bytes of the chunks the windows sample (`reads`).
pub fn project(ir: &Ir, facts: &J, url: &str, copied: &Copied) -> Result<Out, String> {
    let little = facts["little"].as_bool().unwrap_or(false);
    let axes = strs(&facts["axes"]);
    let levels = arr(&facts["levels"]);
    let data_type = data_type(facts)?;
    let (mut out, shapes) = arrays(ir, facts, url, None)?;
    let samples = samples(facts);
    let sampled = match levels.last() {
        Some(lv) if !samples.is_empty() => {
            let size = level_shape(facts, lv).2.iter().product::<u64>() * (n(&facts["bits"]) / 8);
            window::windows(&out, &samples, copied, n(&facts["compression"]), &data_type, little, size)
        }
        _ => Vec::new(),
    };
    let tr = &facts["translation"];
    let trs = J::Array(if truthy(tr) { vec![tr.clone(); levels.len()] } else { vec![] });
    let mut ome = image_ome(&axes, &obj(&facts["units"]), arr(&facts["scales"]), Some(&facts["name"]), translations(&trs))?;
    if let Some(omero) = omero(facts, n(&facts["sizes"]["c"]), &data_type, &sampled) {
        ome["omero"] = omero;
    }
    let own = json!({"byte_order": if little { "little" } else { "big" }, "bigtiff": facts["bigtiff"]});
    out.json("zarr.json", &root_json(ome, "tiff", url, Some(own)));
    out.summary = json!({"axes": axes, "levels": shapes, "references": out.refs.len()});
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn omero_lists_at_most_64_channels() {
        let facts = json!({"spp": 1});
        assert_eq!(arr(&omero(&facts, 64, "uint8", &[]).unwrap()["channels"]).len(), 64);
        assert_eq!(omero(&facts, 65, "uint8", &[]), None);
    }

    #[test]
    fn omero_of_a_float_type_needs_every_window_sampled() {
        let w = Some(json!({"min": -1.0, "max": 2.0, "start": 0.5, "end": 1.5}));
        assert_eq!(omero(&json!({"spp": 1}), 1, "float32", &[]), None);
        assert_eq!(omero(&json!({"spp": 1}), 2, "float32", &[w.clone(), None]), None);
        assert_eq!(omero(&json!({"spp": 1}), 1, "float32", &[w.clone()]).unwrap()["channels"][0]["window"], w.unwrap());
    }
}
