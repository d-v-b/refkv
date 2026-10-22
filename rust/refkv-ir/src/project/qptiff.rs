//! The QPTIFF image projection (spec/virtualize/qptiff.md §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/qptiff.md#4-the-image>) from a valid IR:
//! one array per level, whose chunks are the tiles, or the strips, of each channel's IFD (a
//! short last strip padded to the chunk by the facts' `pad`: a zero data source, or a literal
//! tail spliced into its LZW stream); the facts hold the format, the channels' names and
//! colors (its `omero`), sizes, scales and translation.

use super::{arr, key, n, obj, strs, translations, truthy};
use crate::ir::Ir;
use crate::out::{array_json, image_ome, omero_channel, omero_json, root_json, tiff_codecs, transpose_codec, Out, Part};
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
/// channel image, or, for an RGB image, one per sample; no window, since the file records no
/// display range (a reader samples one).
fn omero(channels: &[J], spp: u64) -> J {
    let mut out = Vec::new();
    if spp > 1 {
        let base = channels.first().and_then(|c| label(&c["name"])).map(|s| format!("{s} ")).unwrap_or_default();
        let letters: &[(&str, &str)] = if spp == 3 { &[("R", "FF0000"), ("G", "00FF00"), ("B", "0000FF")] } else { &[] };
        for s in 0..spp {
            match letters.get(s as usize) {
                Some((l, c)) => out.push(omero_channel(&format!("{base}{l}"), *c, None)),
                None => out.push(omero_channel(&format!("{base}S{s}"), "FFFFFF", None)),
            }
        }
    } else {
        for (i, c) in channels.iter().enumerate() {
            let l = label(&c["name"]).map(str::to_string).unwrap_or_else(|| format!("C{i}"));
            out.push(omero_channel(&l, color(&c["color"]), None));
        }
    }
    omero_json(out)
}

/// The QPTIFF image: one array per level, its chunks the tiles or strips of each channel.
pub fn project(ir: &Ir, facts: &J, url: &str) -> Result<Out, String> {
    let little = facts["little"].as_bool().unwrap_or(false);
    let (spp, planar, bits) = (n(&facts["spp"]), n(&facts["planar"]), n(&facts["bits"]));
    let channels = arr(&facts["channels"]);
    let axes = strs(&facts["axes"]);
    let has_c = axes.first() == Some(&"c");
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
    let c_size = if spp > 1 { spp } else { channels.len() as u64 };
    let c_chunk = if contig { spp } else { 1 };
    let mut refs = Refs::new(ir, Out::new(url));
    let levels = arr(&facts["levels"]);
    let mut shapes = Vec::with_capacity(levels.len());
    for (li, lv) in levels.iter().enumerate() {
        let (w, h, tw, th) = (n(&lv["w"]), n(&lv["h"]), n(&lv["tw"]), n(&lv["th"]));
        let strips = lv["strips"].as_bool().unwrap_or(false);
        let down = h.div_ceil(th);
        let across = if strips { 1 } else { w.div_ceil(tw) };
        let mut shape = Vec::with_capacity(3);
        let mut chunk = Vec::with_capacity(3);
        if has_c {
            shape.push(c_size);
            chunk.push(c_chunk);
        }
        shape.extend([h, w]);
        chunk.extend([th, tw]);
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
    let mut out = refs.out;
    let tr = &facts["translation"];
    let trs = J::Array(if truthy(tr) { vec![tr.clone(); levels.len()] } else { vec![] });
    let mut ome = image_ome(&axes, &obj(&facts["units"]), arr(&facts["scales"]), Some(&facts["name"]), translations(&trs))?;
    ome["omero"] = omero(channels, spp);
    let own = json!({"byte_order": if little { "little" } else { "big" }, "bigtiff": facts["bigtiff"]});
    out.json("zarr.json", &root_json(ome, "qptiff", url, Some(own)));
    out.summary = json!({"axes": axes, "levels": shapes, "channels": channels.len(), "references": out.refs.len()});
    Ok(out)
}
