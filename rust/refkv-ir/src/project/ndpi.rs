//! The NDPI image projection (spec/zarrify/ndpi.md §3
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#3-levels>, §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#4-the-image>) from a valid IR:
//! one array per level, whose chunk is the level's strip (with the level's size in its SOF0 when
//! the frame's differs), or, with McuStarts, `a × b` of the
//! strip's restart intervals as a JPEG stream of their own (`ndpi::Grid`); the facts hold the
//! sizes, the grids, scales and translation, and the IR the McuStarts values. A JPEG XR level's
//! chunks are its tiles (the members of its IFD's `tiles`), or its strip, each as it is, of the
//! data type and samples its streams' pixel format gives. Its `omero` is the RGB samples the
//! streams decode to, or the one channel, labeled with the slide's Fluorescence, each with its
//! type's whole range for a window. A level's planes are stacked along a `z` axis, each with
//! chunks of its own, when the facts' axes have one.

use super::{arr, n, strs, tiled};
use crate::ir::Ir;
use crate::ndpi::{combine, tag_values, Grid};
use crate::out::{array_json, omero_channel, omero_json, root_json, sample_channels, transpose_codec, type_window, Out, Part};
use crate::refs::Refs;
use serde_json::{json, Value as J};

/// A tag's integer values from its facts (`{value, type, high}`), as the IR holds them.
fn values(ir: &Ir, f: &J) -> Option<Vec<u64>> {
    let v = f["value"].as_u64()? as u32;
    let typ = f["type"].as_u64()? as u16;
    Some(tag_values(ir, v, typ, f["high"].as_u64().map(|h| h as u32)))
}

pub fn project(ir: &Ir, facts: &J, url: &str) -> Result<Out, String> {
    let axes = strs(&facts["axes"]);
    let (data_type, samples) = (facts["data_type"].as_str().unwrap_or("uint8"), n(&facts["samples"]));
    let codec = facts["codec"].as_str().unwrap_or("imagecodecs_jpeg");
    let mut codecs = if samples > 1 { vec![transpose_codec(&axes)] } else { vec![] };
    codecs.push(json!({"name": codec}));
    let codecs = J::Array(codecs);
    let mut refs = Refs::new(ir, Out::new(url));
    let levels = arr(&facts["levels"]);
    let mut shapes = Vec::with_capacity(levels.len());
    // planes stacked along `z`, each with chunks of its own (spec/zarrify/ndpi.md §4
    // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#4-the-image>)
    let stacked = axes.len() == 4;
    for (li, lv) in levels.iter().enumerate() {
        let (w, h) = (n(&lv["w"]), n(&lv["h"]));
        let planes = arr(&lv["planes"]);
        let mut chunk = [samples, h, w];
        for (z, p) in planes.iter().enumerate() {
            let at = if stacked { format!("{li}/c/0/{z}/") } else { format!("{li}/c/0/") };
            let (strip, length) = (n(&p["strip"]), n(&p["length"]));
            chunk = if codec == "imagecodecs_jpegxr" {
                // a JPEG XR level: its tiles, or its strip, each a chunk as it is
                // (spec/zarrify/ndpi.md §7 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#7-ndpi-profile>)
                let (tw, th) = (n(&p["tw"]), n(&p["th"]));
                if p["tiles_ifd"].is_null() {
                    refs.chunk(format!("{at}0/0"), strip, length, 0, Some(vec![Part::Src(strip, length)]))?;
                } else {
                    let across = w.div_ceil(tw);
                    let per = across * h.div_ceil(th);
                    tiled::plane(&mut refs, &at, p["tiles"].as_u64(), per, across, |_, row, col| vec![row, col],
                                 |_, _, _, _| None)?;
                }
                [samples, th, tw]
            } else if p["grid"].is_null() {
                // the strip, or, when its frame header is not the level's size, the strip with the
                // level's height and width in its SOF0 (spec/zarrify/ndpi.md §7
                // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#7-ndpi-profile>)
                let parts = match p["frame"].as_u64() {
                    Some(f) => {
                        let mut size = (h as u16).to_be_bytes().to_vec();
                        size.extend((w as u16).to_be_bytes());
                        vec![Part::Src(strip, f), Part::Lit(size), Part::Src(strip + f + 4, length - f - 4)]
                    }
                    None => vec![Part::Src(strip, length)],
                };
                refs.chunk(format!("{at}0/0"), strip, length, 0, Some(parts))?;
                [3, h, w]
            } else {
                let starts = values(ir, &p["starts"]).ok_or("internal: no McuStarts in the IR")?;
                let high = values(ir, &p["high"]);
                let g = Grid::from_facts(&p["grid"], combine(starts, high.as_deref()), length);
                let (before, after) = g.pieces();
                let (before, after) = (refs.out.shared(before), refs.out.shared(after));
                let sof = g.sof(g.b);
                let (down, across) = g.chunks();
                for u in 0..down {
                    for v in 0..across {
                        let parts = g.parts(u, v, g.b, &before, &sof, &after);
                        refs.chunk(format!("{at}{u}/{v}"), strip, length, 0, Some(parts))?;
                    }
                }
                g.chunk_shape()
            };
        }
        let [cs, ch, cw] = chunk;
        let (shape, chunk) = if stacked {
            (vec![samples, planes.len() as u64, h, w], json!([cs, 1, ch, cw]))
        } else {
            (vec![samples, h, w], json!(chunk))
        };
        refs.out.json(&format!("{li}/zarr.json"), &array_json(&shape, data_type, chunk, codecs.clone(), &axes));
        shapes.push(shape);
    }
    let mut out = refs.out;
    let mut ome = tiled::ome(facts, &axes, None)?;
    let window = type_window(data_type);
    ome["omero"] = omero_json(if samples == 1 {
        vec![omero_channel(facts["label"].as_str().unwrap_or("C0"), "FFFFFF", window.as_ref())]
    } else {
        sample_channels("", samples, true, &vec![window; samples as usize])
    });
    let own = facts["own"].as_object().filter(|o| !o.is_empty()).map(|o| J::Object(o.clone()));
    out.json("zarr.json", &root_json(ome, "ndpi", url, own));
    let references = out.refs.len();
    out.summary = json!({"axes": axes, "levels": shapes, "references": references, "codec": codec});
    Ok(out)
}
