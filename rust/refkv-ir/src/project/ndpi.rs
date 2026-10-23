//! The NDPI image projection (spec/virtualize/ndpi.md §3
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/ndpi.md#3-levels>, §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/ndpi.md#4-the-image>) from a valid IR:
//! one array per level, whose chunk is the level's strip, or, with McuStarts, `a × b` of the
//! strip's restart intervals as a JPEG stream of their own (`ndpi::Grid`); the facts hold the
//! sizes, the grids, scales and translation, and the IR the McuStarts values. Its `omero` is the
//! three RGB samples the JPEG streams decode to, each with the window 0 to 255.

use super::{arr, n, obj, strs, translations, truthy};
use crate::ir::Ir;
use crate::ndpi::{combine, tag_values, Grid};
use crate::out::{array_json, image_ome, omero_json, root_json, sample_channels, transpose_codec, type_window, Out, Part};
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
    let codecs = J::Array(vec![transpose_codec(&axes), json!({"name": "imagecodecs_jpeg"})]);
    let mut refs = Refs::new(ir, Out::new(url));
    let levels = arr(&facts["levels"]);
    let mut shapes = Vec::with_capacity(levels.len());
    for (li, lv) in levels.iter().enumerate() {
        let (w, h, strip, length) = (n(&lv["w"]), n(&lv["h"]), n(&lv["strip"]), n(&lv["length"]));
        let chunk = if lv["grid"].is_null() {
            refs.chunk(format!("{li}/c/0/0/0"), strip, length, 0, Some(vec![Part::Src(strip, length)]), 0)?;
            [3, h, w]
        } else {
            let starts = values(ir, &lv["starts"]).ok_or("internal: no McuStarts in the IR")?;
            let high = values(ir, &lv["high"]);
            let g = Grid::from_facts(&lv["grid"], combine(starts, high.as_deref()), length);
            let (before, after) = g.pieces();
            let (before, after) = (refs.out.shared(before), refs.out.shared(after));
            let sof = g.sof(g.b);
            let (down, across) = g.chunks();
            for u in 0..down {
                for v in 0..across {
                    let parts = g.parts(u, v, g.b, &before, &sof, &after);
                    refs.chunk(format!("{li}/c/0/{u}/{v}"), strip, length, 0, Some(parts), u * across + v)?;
                }
            }
            g.chunk_shape()
        };
        let shape = vec![3, h, w];
        refs.out.json(&format!("{li}/zarr.json"), &array_json(&shape, "uint8", json!(chunk), codecs.clone(), &axes));
        shapes.push(shape);
    }
    let mut out = refs.out;
    let tr = &facts["translation"];
    let trs = J::Array(if truthy(tr) { vec![tr.clone(); levels.len()] } else { vec![] });
    let mut ome = image_ome(&axes, &obj(&facts["units"]), arr(&facts["scales"]), None, translations(&trs))?;
    ome["omero"] = omero_json(sample_channels("", 3, true, type_window("uint8").as_ref()));
    out.json("zarr.json", &root_json(ome, "ndpi", url, None));
    let references = out.refs.len();
    out.summary = json!({"axes": axes, "levels": shapes, "references": references, "codec": "imagecodecs_jpeg"});
    Ok(out)
}
