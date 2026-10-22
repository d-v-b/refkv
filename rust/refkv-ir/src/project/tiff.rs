//! The TIFF image projection (spec/virtualize/tiff.md §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/tiff.md#4-the-image>) from a valid IR:
//! each level's planes name the IR's tiles; the facts hold the format, sizes, scales,
//! translation, and the OME-XML's channel names and colors (its `omero`).

use super::{arr, key, n, obj, strs, translations, truthy};
use crate::ir::Ir;
use crate::out::{array_json, image_ome, omero_channel, omero_json, root_json, sample_channels, tiff_codecs, transpose_codec, Out};
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

/// The `omero` member (spec/virtualize/tiff.md §4.4
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/tiff.md#44-arrays>), when there are at
/// most 64 channels: the samples, `R`, `G`, `B` when they decode to RGB; else one per channel,
/// named and colored by the OME-XML's Channels. No window, since a TIFF records no display range.
fn omero(facts: &J, size_c: u64) -> Option<J> {
    if size_c > MAX_OMERO {
        return None;
    }
    let ome = arr(&facts["ome"]["channels"]);
    let name = |c: u64| ome.get(c as usize).and_then(|o| o["name"].as_str()).filter(|s| !s.is_empty() && s.len() <= MAX_NAME);
    let spp = n(&facts["spp"]);
    if spp > 1 {
        let (interpretation, compression) = (n(&facts["interpretation"]), n(&facts["compression"]));
        let rgb = interpretation == 2 || (interpretation == 6 && compression == 7);
        let base = name(0).map(|s| format!("{s} ")).unwrap_or_default();
        return Some(omero_json(sample_channels(&base, spp, rgb)));
    }
    let channels = (0..size_c).map(|c| {
        let label = name(c).map(str::to_string).unwrap_or_else(|| format!("C{c}"));
        omero_channel(&label, color(ome.get(c as usize).map_or(&J::Null, |o| &o["color"])), None)
    });
    Some(omero_json(channels.collect()))
}

/// The TIFF image: one array per level, its chunks the tiles of each plane.
pub fn project(ir: &Ir, facts: &J, url: &str) -> Result<Out, String> {
    let little = facts["little"].as_bool().unwrap_or(false);
    let (spp, planar, bits) = (n(&facts["spp"]), n(&facts["planar"]), n(&facts["bits"]));
    let sizes = &facts["sizes"];
    let (size_t, size_c, size_z) = (n(&sizes["t"]), n(&sizes["c"]), n(&sizes["z"]));
    let plane_c = n(&facts["plane_c"]);
    let axes = strs(&facts["axes"]);
    let contig = spp > 1 && planar == 1;
    let kind = match n(&facts["sample_format"]) {
        1 => "uint",
        2 => "int",
        3 => "float",
        f => return Err(format!("internal: sample format {f}")),
    };
    let data_type = format!("{kind}{bits}");
    let mut codecs = if contig { vec![transpose_codec(&axes)] } else { vec![] };
    codecs.extend(tiff_codecs(n(&facts["compression"]), bits, little));
    let codecs = J::Array(codecs);
    let mut refs = Refs::new(ir, Out::new(url));
    let levels = arr(&facts["levels"]);
    let mut shapes = Vec::with_capacity(levels.len());
    for (li, lv) in levels.iter().enumerate() {
        let (w, h, tw, th) = (n(&lv["w"]), n(&lv["h"]), n(&lv["tw"]), n(&lv["th"]));
        let of = |a: &str, t: u64, c: u64, z: u64, y: u64, x: u64| match a {
            "t" => t,
            "c" => c,
            "z" => z,
            "y" => y,
            _ => x,
        };
        let shape: Vec<u64> = axes.iter().map(|a| of(a, size_t, size_c, size_z, h, w)).collect();
        let chunk: Vec<u64> = axes.iter().map(|a| of(a, 1, if contig { spp } else { 1 }, 1, th, tw)).collect();
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
    let mut out = refs.out;
    let tr = &facts["translation"];
    let trs = J::Array(if truthy(tr) { vec![tr.clone(); levels.len()] } else { vec![] });
    let mut ome = image_ome(&axes, &obj(&facts["units"]), arr(&facts["scales"]), Some(&facts["name"]), translations(&trs))?;
    if let Some(omero) = omero(facts, size_c) {
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
        assert_eq!(arr(&omero(&facts, 64).unwrap()["channels"]).len(), 64);
        assert_eq!(omero(&facts, 65), None);
    }
}
