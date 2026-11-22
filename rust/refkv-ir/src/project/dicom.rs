//! The DICOM image projection (spec/zarrify/dicom.md §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#4-output>) from a valid IR and
//! the parser's description of the file (§7.8
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#78-the-description>), through
//! refkv's plan of it (`dicom::plan::plan`): each frame's chunk names the IR's data elements under
//! the top-level Pixel Data (its fragments by the plan's `bounds`, or one per frame), made explicit
//! about its color transform for a JPEG with three samples; the plan holds the array, its scales
//! and the window. The root's source metadata is made from the description's file in the DICOM
//! JSON Model (`root`).

use super::{arr, key, n, obj, strs};
use crate::dicom::plan::plan;
use crate::dicom::ADOBE;
use crate::ir::Ir;
use crate::dicom::json::PER_FRAME;
use crate::out::{array_json, bytes_codec, image_ome, item_size, js_string_size, json_size, omero_channel, omero_json, root_json, transpose_codec, Out, Part, RGBA, SOURCE_NODE};
use crate::refs::Refs;
use serde_json::{json, Map, Value as J};

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
        refs.chunk(format!("palette/c/{k}/0"), start, length, 0, Some(vec![Part::Src(start, length)]))?;
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

/// the most bytes of one member of the root's `meta` or `dataset`, and of the two together
const MAX_MEMBER: usize = 1 << 14;
const MAX_ROOT: usize = 1 << 16;

/// The root's source metadata (§5.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#51-the-root>), from the
/// description's file in the DICOM JSON Model (`dicom_json`): the preamble where it is not all
/// zeros; `meta` and `dataset` without the Per-Frame Functional Groups Sequence and the members
/// that would make the root large (`move_large`), each value kept out of the JSON naming its path
/// under the source node; and the paths of the pixel data's extra bytes, unreferenced bytes,
/// fragments and the trailing bytes, where the file has them.
pub fn root(d: &J) -> J {
    let model = &d["dicom_json"];
    let mut root = Map::new();
    if !model["preamble"].is_null() {
        root.insert("preamble".into(), model["preamble"].clone());
    }
    // each BulkDataURI under the source node first: the sizes `move_large` measures are the root's
    let members = |k: &str| {
        let mut v = model[k].clone();
        under_source_node(&mut v);
        match v {
            J::Object(m) => m,
            _ => Map::new(),
        }
    };
    let (mut meta, mut dataset) = (members("meta"), members("dataset"));
    dataset.swap_remove(PER_FRAME);
    move_large(&mut meta, &mut dataset);
    for (k, mut m) in [("meta", meta), ("dataset", dataset)] {
        m.sort_keys();
        root.insert(k.into(), J::Object(m));
    }
    let pixel = &d["pixel_data"];
    let structure = &pixel["structure"];
    if !structure["extra"].is_null() && !pixel["encapsulated"].as_bool().unwrap_or(false) {
        root.insert("pixel_extra".into(), json!(format!("{SOURCE_NODE}/pixel_extra")));
    }
    let eot = &structure["extended_offset_table"];
    if eot["skips"].as_bool().unwrap_or(false) {
        root.insert("pixel_unreferenced".into(), json!(format!("{SOURCE_NODE}/pixel_unreferenced")));
        if !eot["headers"].as_bool().unwrap_or(false) {
            root.insert("pixel_unreferenced_headers".into(), json!(true));
        }
    }
    let bounds: Vec<u64> = arr(&structure["bounds"]).iter().map(n).collect();
    if bounds.windows(2).any(|w| w[1] - w[0] > 1) {
        root.insert("pixel_fragments".into(), json!(format!("{SOURCE_NODE}/pixel_fragments")));
    }
    if structure["offset_table"].as_bool().unwrap_or(false) {
        root.insert("pixel_offset_table".into(), json!(true));
    }
    if !d["trailing"].is_null() {
        root.insert("trailing".into(), json!(format!("{SOURCE_NODE}/trailing")));
    }
    J::Object(root)
}

/// Each `BulkDataURI`, an element's path, made its path under the source node.
fn under_source_node(v: &mut J) {
    match v {
        J::Object(m) => {
            for (k, x) in m.iter_mut() {
                match x {
                    J::String(path) if k == "BulkDataURI" => *path = format!("{SOURCE_NODE}/{path}"),
                    _ => under_source_node(x),
                }
            }
        }
        J::Array(a) => a.iter_mut().for_each(under_source_node),
        _ => {}
    }
}

fn object_size(sizes: &[(String, usize)]) -> usize {
    2 + sizes.len().saturating_sub(1) + sizes.iter().map(|(k, n)| js_string_size(k) + 1 + n).sum::<usize>()
}

/// Leaves out of the root's `meta` and `dataset` the members that would make it large:
/// each over 16384 bytes, then, while the two are over 65536 together, the largest left
/// (of equal sizes, the first by name, `meta`'s before `dataset`'s).
fn move_large(meta: &mut Map<String, J>, dataset: &mut Map<String, J>) {
    let mut sizes: [Vec<(String, usize)>; 2] = [Vec::new(), Vec::new()];
    for (i, members) in [&mut *meta, &mut *dataset].into_iter().enumerate() {
        let keys: Vec<String> = members.keys().cloned().collect();
        for k in keys {
            let n = json_size(&members[&k]);
            if n > MAX_MEMBER {
                members.swap_remove(&k);
            } else {
                sizes[i].push((k, n));
            }
        }
    }
    let mut total: usize = sizes.iter().map(|s| object_size(s)).sum();
    let mut largest: Vec<(usize, String, usize)> = sizes.iter().enumerate().flat_map(|(i, s)| s.iter().map(move |(k, n)| (*n, k.clone(), i))).collect();
    largest.sort_by(|a, b| b.0.cmp(&a.0).then_with(|| a.1.cmp(&b.1)).then_with(|| a.2.cmp(&b.2)));
    for (n, k, i) in largest {
        if total <= MAX_ROOT {
            break;
        }
        let members = if i == 0 { &mut *meta } else { &mut *dataset };
        total -= js_string_size(&k) + 1 + n + usize::from(members.len() > 1);
        members.swap_remove(&k);
    }
}

/// The output of the file `description` describes.
pub fn project(ir: &Ir, description: &J, url: &str) -> Result<Out, String> {
    let d = description;
    let plan = plan(d);
    let attributes = &d["attributes"];
    let axes = strs(&plan["axes"]);
    let shape: Vec<u64> = arr(&plan["shape"]).iter().map(n).collect();
    let chunk: Vec<u64> = arr(&plan["chunk_shape"]).iter().map(n).collect();
    let data_type = plan["data_type"].as_str().unwrap_or("");
    let codec = d["compression"].as_str().unwrap_or("native");
    let spp = n(&attributes["samples_per_pixel"]);
    let (planar, count, across) = (plan["planar"].as_bool().unwrap_or(false), n(&plan["count"]), n(&plan["across"]));
    let whole_slide = plan["whole_slide"].as_bool().unwrap_or(false);
    let fa = plan["frame_axis"].as_str().unwrap_or("z");
    let little = d["little"].as_bool().unwrap_or(true);
    let endian = if little { "little" } else { "big" };
    // an RLE frame decodes to planar samples, all others to interleaved ones
    let mut codecs = if transposed(codec, spp, planar) { vec![transpose_codec(&axes)] } else { vec![] };
    codecs.push(chain(codec, data_type, spp, endian));
    let mut ome = image_ome(&axes, &obj(&plan["units"]), &[plan["scale"].clone()], None, None)?;
    let window = plan["window"].clone();
    let photometric = attributes["photometric_interpretation"].as_str().unwrap_or("");
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
    let mut own = root(d);
    if let Some(p) = d.get("palette").filter(|p| !p.is_null()) {
        own["palette"] = json!({"path": "palette", "first_mapped": p["first_mapped"], "bits": p["bits"]});
    }
    refs.out.json("zarr.json", &root_json(ome, "dicom", url, Some(own)));
    let mut array = array_json(&shape, data_type, json!(chunk), J::Array(codecs), &axes);
    if data_type == "bool" {
        array["fill_value"] = json!(false);
    }
    refs.out.json("0/zarr.json", &array);
    // the data elements under Pixel Data, by name index: frames, or fragments
    let mut members = ir.members(n(&d["elements"]["pixel_data"]) as u32);
    members.sort_unstable();
    let bounds: Vec<u64> = arr(&plan["bounds"]).iter().map(n).collect();
    let adobe = plan["adobe"].as_u64().map(|t| t as u8);
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
            refs.chunk(key("0/c/", coords), start, length, form, Some(parts))?;
        }
    }
    if let Some(p) = d.get("palette").filter(|p| !p.is_null()) {
        palette(&mut refs, p, endian)?;
    }
    let mut out = refs.out;
    let references = out.refs.len();
    out.summary = json!({
        "axes": axes,
        "shape": shape,
        "dataType": data_type,
        "transferSyntax": d["transfer_syntax"],
        "photometric": photometric,
        "frames": attributes["number_of_frames"].as_f64().map_or(1, |f| f as u64),
        "wholeSlide": whole_slide,
        "references": references,
    });
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A member whose size is at most 16384 bytes only without the source node in its
    /// `BulkDataURI`s is left out of the root: the sizes are the root's, the prefix included.
    #[test]
    fn a_member_is_measured_with_its_bulk_data_uris_under_the_source_node() {
        let item = json!({"00281201": {"vr": "OW", "BulkDataURI": "dataset/00082112/0/00281201"}});
        let one = json_size(&item) + 1;
        // just under the limit without the prefix, over it with
        let count = (MAX_MEMBER - 40) / one + 1;
        let member = json!({"vr": "SQ", "Value": vec![item; count]});
        let size = json_size(&member);
        assert!(size <= MAX_MEMBER && size + count * (SOURCE_NODE.len() + 1) > MAX_MEMBER, "{size}");
        let d = json!({"dicom_json": {"preamble": null, "meta": {}, "dataset": {"00082112": member}},
                       "pixel_data": {"encapsulated": false, "structure": {"extra": null}}, "trailing": null});
        assert!(root(&d)["dataset"].get("00082112").is_none());
    }
}
