//! refkv's plan of a DICOM file's image (spec/zarrify/dicom.md §3
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#3-the-image> and §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#4-output>): the decisions of
//! refkv's convention, made from the parser's description of the file (§7.8
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#78-the-description>) and nothing
//! else: whether it is a whole-slide image and how its tiles are arranged, the frames' axis, the
//! axes, shape and chunk shape, the data type, the scales and units, the omero window, how the
//! frames are referenced, and a JPEG's color transform. The plan is public data, which the
//! convention's projection (`project/dicom.rs`) reads; it belongs to refkv's convention, not to
//! the IR.

use super::adobe_marker;
use serde_json::{json, Map, Value as J};

/// The decimals of an attribute of the description: each value, or None where it is no number.
fn decimals(v: &J) -> Option<Vec<Option<f64>>> {
    v.as_array().map(|a| a.iter().map(J::as_f64).collect())
}

/// The omero window of the image (§4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#4-output>),
/// from the attributes of a description, and whether its ends are finite: the stored values'
/// range, and, of one sample, the VOI window mapped back through the rescale.
pub fn window(a: &J) -> (J, bool) {
    let int = |k: &str| a[k].as_u64().unwrap_or(0);
    let (bits_stored, representation, spp) = (int("bits_stored"), int("pixel_representation"), int("samples_per_pixel"));
    let (low, high): (i64, i64) = if representation == 0 {
        (0, (1i64 << bits_stored) - 1)
    } else {
        (-(1i64 << (bits_stored - 1)), (1i64 << (bits_stored - 1)) - 1)
    };
    let mut finite = true;
    let mut window = json!({"min": low, "max": high, "start": low, "end": high});
    if spp == 1 {
        let first = |k: &str| decimals(&a[k]).map(|v| v.first().copied().flatten());
        if let (Some(Some(c)), Some(Some(w))) = (first("window_center"), first("window_width")) {
            let k = first("rescale_intercept").unwrap_or(Some(0.0));
            let s = first("rescale_slope").unwrap_or(Some(1.0));
            if let (true, Some(k), Some(s)) = (w >= 1.0, k, s) {
                if s != 0.0 {
                    let (mut lo, mut hi) = ((c - w / 2.0 - k) / s, (c + w / 2.0 - k) / s);
                    if s < 0.0 {
                        std::mem::swap(&mut lo, &mut hi);
                    }
                    finite = lo.is_finite() && hi.is_finite();
                    window["start"] = json!(lo);
                    window["end"] = json!(hi);
                }
            }
        }
    }
    (window, finite)
}

/// refkv's plan of the image the description `d` describes: `whole_slide`, and its `across`, the
/// tiles of a row; `frame_axis`; `axes`, `shape` and `chunk_shape`; `data_type`; `scale` and
/// `units`; `window`; `count`, the stored elements the chunks reference; `planar`, whether a
/// native frame's samples are planes; `layout`, how the frames are referenced (`native`, `eot` or
/// `fragments`), and `bounds`, the fragments of each frame; and `adobe`, the color transform of a
/// JPEG frame of three samples.
pub fn plan(d: &J) -> J {
    let a = &d["attributes"];
    let int = |k: &str| a[k].as_u64();
    let (spp, rows, columns) = (int("samples_per_pixel").unwrap_or(1), int("rows").unwrap_or(0), int("columns").unwrap_or(0));
    let bits_allocated = int("bits_allocated").unwrap_or(8);
    let n = a["number_of_frames"].as_f64().unwrap_or(1.0) as u64;
    let codec = d["compression"].as_str().unwrap_or("native");
    let native = codec == "native";
    let photometric = a["photometric_interpretation"].as_str().unwrap_or("");

    // spec/zarrify/dicom.md §3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#3-the-image>
    let whole_slide = a["sop_class_uid"].as_str() == super::rules().table("whole_slide").as_str();
    let (width, height, across) = match (whole_slide, int("total_pixel_matrix_columns"), int("total_pixel_matrix_rows")) {
        (true, Some(w), Some(h)) => (w, h, w.div_ceil(columns.max(1))),
        _ => (columns, rows, 1),
    };

    // spec/zarrify/dicom.md §4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#4-output>
    let pointer = a["frame_increment_pointer"].as_str();
    let fa = if matches!(pointer, Some("00181063" | "00181065")) { "t" } else { "z" };
    let mut axes = Vec::new();
    if spp == 3 {
        axes.push("c");
    }
    if !whole_slide && n > 1 {
        axes.push(fa);
    }
    axes.extend(["y", "x"]);
    let spacing = decimals(&a["pixel_spacing"]);
    let usable = spacing.as_ref().is_some_and(|s| s.len() == 2 && s.iter().all(|x| x.is_some_and(|x| x > 0.0)));
    let step = decimals(&a["spacing_between_slices"]).and_then(|b| b.first().copied().flatten()).filter(|x| *x > 0.0);
    let period = if pointer == Some("00181063") {
        decimals(&a["frame_time"]).and_then(|t| t.first().copied().flatten()).filter(|x| *x > 0.0).map(|x| x / 1000.0)
    } else {
        None
    };
    let scale_of = |x: &str| -> f64 {
        match x {
            "z" => step.unwrap_or(1.0),
            "t" => period.unwrap_or(1.0),
            "y" if usable => spacing.as_ref().unwrap()[0].unwrap(),
            "x" if usable => spacing.as_ref().unwrap()[1].unwrap(),
            _ => 1.0,
        }
    };
    let scale: Vec<f64> = axes.iter().map(|x| scale_of(x)).collect();
    let mut units = Map::new();
    for x in &axes {
        let u = match *x {
            "z" if step.is_some() => "millimeter",
            "t" if period.is_some() => "second",
            "y" | "x" if usable => "millimeter",
            _ => continue,
        };
        units.insert(x.to_string(), json!(u));
    }
    let (window, _) = window(a);

    // the stored elements: native frames, or every 1-bit frame together, or encapsulated frames
    let structure = &d["pixel_data"]["structure"];
    let count = if native { structure["stored"].as_u64().unwrap_or(n) } else { n };
    let planar = native && spp == 3 && int("planar_configuration") == Some(1);
    let frames_per_chunk = if count == 1 { n } else { 1 };
    let shape: Vec<u64> = axes.iter().map(|x| match *x { "c" => 3, "y" => height, "x" => width, _ => n }).collect();
    let chunk: Vec<u64> = axes.iter().map(|x| match *x { "c" => if planar { 1 } else { 3 }, "y" => rows, "x" => columns, _ => frames_per_chunk }).collect();
    let signed = int("pixel_representation") == Some(1);
    let data_type = if bits_allocated == 1 { "bool".to_string() } else { format!("{}int{}", if signed { "" } else { "u" }, bits_allocated) };
    let layout = if native { "native" } else if !structure["extended_offset_table"].is_null() { "eot" } else { "fragments" };
    json!({
        "whole_slide": whole_slide, "across": across, "frame_axis": fa,
        "axes": axes, "shape": shape, "chunk_shape": chunk, "data_type": data_type,
        "scale": scale, "units": units, "window": window,
        "count": count, "planar": planar, "layout": layout,
        "bounds": if native { json!([]) } else { structure["bounds"].clone() },
        "adobe": (codec == "jpeg" && spp == 3).then(|| adobe_marker(photometric)),
    })
}
