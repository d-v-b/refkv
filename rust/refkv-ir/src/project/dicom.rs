//! The DICOM image projection (spec/virtualize/dicom.md §4
//! <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/dicom.md#4-output>) from a valid IR:
//! each frame's chunk names the IR's data elements under the top-level Pixel Data (its
//! fragments by `bounds`, or one per frame), made explicit about its color transform for a
//! JPEG with three samples; the facts hold the array, its scales, the window and the root's
//! source metadata.

use super::{arr, n, obj, strs};
use crate::dicom::ADOBE;
use crate::ir::Ir;
use crate::out::{array_json, image_ome, root_json, transpose_codec, Out, Part};
use crate::refs::Refs;
use serde_json::{json, Value as J};

pub fn project(ir: &Ir, facts: &J, url: &str) -> Result<Out, String> {
    let axes = strs(&facts["axes"]);
    let shape: Vec<u64> = arr(&facts["shape"]).iter().map(n).collect();
    let chunk: Vec<u64> = arr(&facts["chunk_shape"]).iter().map(n).collect();
    let data_type = facts["data_type"].as_str().unwrap_or("");
    let codec = facts["codec"].as_str().unwrap_or("native");
    let (spp, planar, count, across) = (n(&facts["spp"]), facts["planar"].as_bool().unwrap_or(false), n(&facts["n"]), n(&facts["across"]));
    let whole_slide = facts["whole_slide"].as_bool().unwrap_or(false);
    let fa = facts["frame_axis"].as_str().unwrap_or("z");
    let mut codecs = if spp == 3 && !planar { vec![transpose_codec(&axes)] } else { vec![] };
    codecs.push(match codec {
        "native" if data_type.ends_with("int8") => json!({"name": "bytes"}),
        "native" => json!({"name": "bytes", "configuration": {"endian": if facts["little"].as_bool().unwrap_or(true) { "little" } else { "big" }}}),
        c => json!({"name": format!("imagecodecs_{c}")}),
    });
    let mut ome = image_ome(&axes, &obj(&facts["units"]), &[facts["scale"].clone()], None, None)?;
    let window = facts["window"].clone();
    let photometric = facts["photometric"].as_str().unwrap_or("");
    let channels: Vec<J> = if spp == 1 {
        let mut c = json!({"label": "gray", "color": "FFFFFF", "active": true, "window": window});
        if photometric == "MONOCHROME1" {
            c["inverted"] = json!(true);
        }
        vec![c]
    } else {
        [("R", "FF0000"), ("G", "00FF00"), ("B", "0000FF")]
            .iter()
            .map(|(l, c)| json!({"label": l, "color": c, "active": true, "window": window}))
            .collect()
    };
    ome["omero"] = json!({"channels": channels});

    let mut refs = Refs::new(ir, Out::new(url));
    refs.out.json("zarr.json", &root_json(ome, "dicom", url, Some(facts["root"].clone())));
    refs.out.json("0/zarr.json", &array_json(&shape, data_type, json!(chunk), J::Array(codecs), &axes));
    // the data elements under Pixel Data, by name index: frames, or fragments
    let mut members = ir.members(n(&facts["pixel"]) as u32);
    members.sort_unstable();
    let bounds: Vec<u64> = arr(&facts["bounds"]).iter().map(n).collect();
    let adobe = facts["adobe"].as_u64().map(|t| t as u8);
    let mut at = 0usize;
    for f in 0..count {
        // this frame's data elements: (name index, start, length, form)
        let (lo, hi) = if bounds.is_empty() { (f, f + 1) } else { (bounds[f as usize], bounds[f as usize + 1]) };
        while at < members.len() && members[at].0 < lo {
            at += 1;
        }
        let mut mine = Vec::new();
        while at < members.len() && members[at].0 < hi {
            mine.push(members[at]);
            at += 1;
        }
        let (_, start, length, form) = *mine.first().ok_or_else(|| format!("internal: frame {f} has no data element"))?;
        let mut ranges: Vec<(u64, u64)> = mine.iter().map(|m| (m.1, m.2)).collect();
        let (row, col) = if whole_slide { (f / across, f % across) } else { (0, 0) };
        let samples = if planar { 3 } else { 1 };
        for s in 0..samples {
            let parts: Vec<Part> = if planar {
                let third = length / 3;
                ranges = vec![(start + s * third, third)];
                vec![Part::Src(start + s * third, third)]
            } else {
                let mut p: Vec<Part> = ranges.iter().map(|&(o, m)| Part::Src(o, m)).collect();
                if let Some(t) = adobe {
                    let mut lit = ADOBE.to_vec();
                    lit.push(t);
                    p[0] = Part::Src(ranges[0].0 + 2, ranges[0].1 - 2);
                    p.insert(0, Part::Lit(lit));
                }
                p
            };
            let coords: Vec<String> = axes
                .iter()
                .map(|a| match *a {
                    "c" => s,
                    "y" => row,
                    "x" => col,
                    a if a == fa => f,
                    _ => 0,
                })
                .map(|c| c.to_string())
                .collect();
            refs.chunk(format!("0/c/{}", coords.join("/")), start, length, form, Some(parts), s)?;
        }
    }
    let mut out = refs.out;
    let references = out.refs.len();
    out.summary = json!({
        "axes": axes,
        "shape": shape,
        "dataType": data_type,
        "transferSyntax": facts["syntax"],
        "photometric": photometric,
        "frames": count,
        "wholeSlide": whole_slide,
        "references": references,
    });
    Ok(out)
}
