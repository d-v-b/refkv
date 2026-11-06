//! The DICOM image projection (spec/zarrify/dicom.md §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#4-output>) from a valid IR:
//! each frame's chunk names the IR's data elements under the top-level Pixel Data (its
//! fragments by `bounds`, or one per frame), made explicit about its color transform for a
//! JPEG with three samples; the facts hold the array, its scales, the window and the root's
//! source metadata.

use super::{arr, key, n, obj, strs};
use crate::dicom::ADOBE;
use crate::ir::Ir;
use crate::out::{array_json, bytes_codec, image_ome, item_size, omero_channel, omero_json, root_json, transpose_codec, Out, Part, RGBA};
use crate::refs::Refs;
use serde_json::{json, Value as J};

/// The channels of an image of YCbCr samples as stored, its labels and colors.
const YBR: [(&str, &str); 3] = [("Y", "FFFFFF"), ("Cb", "0000FF"), ("Cr", "FF0000")];

/// A data type as `imagecodecs_dicomrle`'s `dtype` names it, as NumPy writes it.
fn numpy_dtype(data_type: &str) -> String {
    let size = item_size(data_type);
    let order = if size == 1 { '|' } else { '<' };
    let kind = if data_type.starts_with('u') { 'u' } else { 'i' };
    format!("{order}{kind}{size}")
}

/// The array `palette` (spec/zarrify/dicom.md §4.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#41-the-palette>): each chunk one
/// lookup table's entries, in the file.
fn palette(refs: &mut Refs, p: &J, endian: &str) -> Result<(), String> {
    let (entries, cell) = (n(&p["entries"]), n(&p["cell"]));
    let data_type = if cell == 1 { "uint8" } else { "uint16" };
    refs.out.json("palette/zarr.json", &array_json(&[3, entries], data_type, json!([1, entries]), json!([bytes_codec(cell, endian)]), &["c", "i"]));
    for (k, table) in arr(&p["tables"]).iter().enumerate() {
        let (start, length) = (n(table), entries * cell);
        refs.chunk(format!("palette/c/{k}/0"), start, length, 0, Some(vec![Part::Src(start, length)]), 0)?;
    }
    Ok(())
}

/// Whether a frame's samples are stored interleaved, so that `transpose` comes first: 3 samples
/// of Planar Configuration 0, but for RLE, whose frames decode to planar samples (spec/zarrify/dicom.md
/// §2.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#21-transfer-syntax>).
pub fn transposed(codec: &str, spp: u64, planar: bool) -> bool {
    spp == 3 && !planar && codec != "dicomrle"
}

/// The codec of a frame after any `transpose` (spec/zarrify/dicom.md §2.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#21-transfer-syntax>), by the
/// transfer syntax's codec (`native` for none), for samples of `data_type`, `spp` a pixel, in the
/// byte order `endian` of native ones.
pub fn chain(codec: &str, data_type: &str, spp: u64, endian: &str) -> J {
    match codec {
        // zarr-extensions' `packbits`, bit packing (not TIFF's PackBits)
        "native" if data_type == "bool" => json!({"name": "packbits", "configuration": {"padding_encoding": "none"}}),
        "native" => bytes_codec(item_size(data_type), endian),
        "dicomrle" => json!({"name": "imagecodecs_dicomrle", "configuration": {"dtype": numpy_dtype(data_type)}}),
        "htj2k" if spp == 3 => json!({"name": "imagecodecs_htj2k", "configuration": {"planar": false}}),
        c => json!({"name": format!("imagecodecs_{c}")}),
    }
}

pub fn project(ir: &Ir, facts: &J, url: &str) -> Result<Out, String> {
    let axes = strs(&facts["axes"]);
    let shape: Vec<u64> = arr(&facts["shape"]).iter().map(n).collect();
    let chunk: Vec<u64> = arr(&facts["chunk_shape"]).iter().map(n).collect();
    let data_type = facts["data_type"].as_str().unwrap_or("");
    let codec = facts["codec"].as_str().unwrap_or("native");
    let (spp, planar, count, across) = (n(&facts["spp"]), facts["planar"].as_bool().unwrap_or(false), facts["stored"].as_u64().unwrap_or(n(&facts["n"])), n(&facts["across"]));
    let whole_slide = facts["whole_slide"].as_bool().unwrap_or(false);
    let fa = facts["frame_axis"].as_str().unwrap_or("z");
    let little = facts["little"].as_bool().unwrap_or(true);
    let endian = if little { "little" } else { "big" };
    // an RLE frame decodes to planar samples, all others to interleaved ones
    let mut codecs = if transposed(codec, spp, planar) { vec![transpose_codec(&axes)] } else { vec![] };
    codecs.push(chain(codec, data_type, spp, endian));
    let mut ome = image_ome(&axes, &obj(&facts["units"]), &[facts["scale"].clone()], None, None)?;
    let window = facts["window"].clone();
    let photometric = facts["photometric"].as_str().unwrap_or("");
    let channels: Vec<J> = if spp == 1 {
        let mut c = omero_channel(if photometric == "PALETTE COLOR" { "index" } else { "gray" }, "FFFFFF", Some(&window));
        if photometric == "MONOCHROME1" {
            c["inverted"] = json!(true);
        }
        vec![c]
    } else if photometric == "YBR_FULL" && codec != "jpeg" {
        // as stored: only a JPEG decoder, whose frames are made explicit about it, converts them
        YBR.iter().map(|(l, c)| omero_channel(l, *c, Some(&window))).collect()
    } else {
        RGBA[..3].iter().map(|(l, c)| omero_channel(l, *c, Some(&window))).collect()
    };
    ome["omero"] = omero_json(channels);

    let mut refs = Refs::new(ir, Out::new(url));
    let mut own = facts["root"].clone();
    if let Some(p) = facts.get("palette").filter(|p| !p.is_null()) {
        own["palette"] = json!({"path": "palette", "first_mapped": p["first_mapped"], "bits": p["bits"]});
    }
    refs.out.json("zarr.json", &root_json(ome, "dicom", url, Some(own)));
    let mut array = array_json(&shape, data_type, json!(chunk), J::Array(codecs), &axes);
    if data_type == "bool" {
        array["fill_value"] = json!(false);
    }
    refs.out.json("0/zarr.json", &array);
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
            let coords = axes.iter().map(|a| match *a {
                "c" => s,
                "y" => row,
                "x" => col,
                a if a == fa => f,
                _ => 0,
            });
            refs.chunk(key("0/c/", coords), start, length, form, Some(parts), s)?;
        }
    }
    if let Some(p) = facts.get("palette").filter(|p| !p.is_null()) {
        palette(&mut refs, p, endian)?;
    }
    let mut out = refs.out;
    let references = out.refs.len();
    out.summary = json!({
        "axes": axes,
        "shape": shape,
        "dataType": data_type,
        "transferSyntax": facts["syntax"],
        "photometric": photometric,
        "frames": n(&facts["n"]),
        "wholeSlide": whole_slide,
        "references": references,
    });
    Ok(out)
}
