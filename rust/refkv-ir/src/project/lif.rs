//! The LIF image projection (spec/zarrify/lif.md §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#4-output>, and the source
//! metadata of §5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#5-source-metadata>)
//! from a valid IR and the parser's layout: one image per field of each image element, whose
//! chunks are bands of a plane of its memory block.

use super::{arr, key, n, obj, strs};
use crate::ir::Ir;
use crate::out::{array_json, bioformats2raw, bytes_codec, declare, full_window, group_doc, image_ome, omero_channel, omero_json, transpose_codec, Out, Part};
use crate::refs::Refs;
use serde_json::{json, Map, Value as J};
use std::collections::HashMap;

/// The longest image name, in UTF-8 bytes, an image's `name` takes, and the most channels its
/// `omero` lists (spec/zarrify/lif.md §4.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#42-images>).
const MAX_NAME: usize = 256;
const MAX_OMERO: usize = 64;
/// The DimID of a mosaic's tiles.
const MOSAIC: u64 = 10;

/// The data element of each memory block, by the block's index: (start, length, form).
fn block_data(ir: &Ir) -> HashMap<u64, (u64, u64, u32)> {
    let mut out = HashMap::new();
    for &b in ir.children(0) {
        if ir.names.get(ir.name[b as usize]) != "blocks/" {
            continue;
        }
        for &c in ir.children(b) {
            if let Some(f) = ir.form_id(c).filter(|_| ir.name_of(c) == "data") {
                out.insert(ir.nidx[b as usize], (ir.start[c as usize], ir.len[c as usize], f));
            }
        }
    }
    out
}

/// A series image's name: its element's Name, and its field when the element has several.
fn image_name(name: &J, field: Option<(&str, usize)>) -> J {
    let base = name.as_str().filter(|s| !s.is_empty() && s.len() <= MAX_NAME);
    match (base, field) {
        (Some(b), None) => json!(b),
        (Some(b), Some((word, f))) => json!(format!("{b} ({word} {f})")),
        (None, Some((word, f))) => json!(format!("{word} {f}")),
        (None, None) => J::Null,
    }
}

pub fn project(ir: &Ir, facts: &J, url: &str) -> Result<Out, String> {
    let series = n(&facts["series"]);
    let mut out = Out::new(url);
    bioformats2raw(&mut out, "lif", url, json!({"version": facts["version"]}), series as usize, series > 0);
    let blocks = block_data(ir);
    let mut refs = Refs::new(ir, out);
    let mut s = 0u64;
    let mut chunks = 0u64;
    for im in arr(&facts["images"]) {
        let axes = strs(&im["axes"]);
        let shape: Vec<u64> = arr(&im["shape"]).iter().map(n).collect();
        let chunk_shape: Vec<u64> = arr(&im["chunk_shape"]).iter().map(n).collect();
        let data_type = im["data_type"].as_str().unwrap_or("uint8");
        let item = n(&im["item"]);
        let interleaved = im["interleaved"].as_bool().unwrap_or(false);
        let mut codecs = Vec::new();
        if interleaved {
            codecs.push(transpose_codec(&axes));
        }
        codecs.push(bytes_codec(item, "little"));
        let array = array_json(&shape, data_type, json!(chunk_shape), J::Array(codecs), &axes);
        // omero (spec/zarrify/lif.md §4.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#43-image-arrays>)
        let colors = strs(&im["colors"]);
        let resolution: Vec<u64> = arr(&im["resolution"]).iter().map(n).collect();
        let omero = (colors.len() <= MAX_OMERO).then(|| {
            let channels = colors
                .iter()
                .zip(&resolution)
                .enumerate()
                .map(|(c, (color, &bits))| {
                    let window = data_type.starts_with("uint").then(|| full_window(json!(0), json!(u64::MAX >> (64 - bits))));
                    omero_channel(&format!("C{c}"), *color, window.as_ref())
                })
                .collect();
            omero_json(channels)
        });
        let units = obj(&im["units"]);
        let scale = im["scale"].clone();
        let field_dims: Vec<u64> = arr(&im["field_dims"]).iter().map(n).collect();
        let fields = arr(&im["fields"]);
        let word = if field_dims == [MOSAIC] { "tile" } else { "field" };
        let tiles = arr(&im["tiles"]);
        let (start, length, form) = *blocks.get(&n(&im["block"])).ok_or("internal: an image's block has no data")?;
        let base = n(&im["base"]);
        let offsets: Vec<u64> = arr(&im["offsets"]).iter().map(n).collect();
        let (nt, st) = (n(&im["t"][0]), n(&im["t"][1]));
        let (nz, sz) = (n(&im["z"][0]), n(&im["z"][1]));
        let (rows, row, y_stride, band) = (n(&im["rows"]), n(&im["row"]), n(&im["y_stride"]), n(&im["band"]));
        let planes: Vec<(Vec<u64>, u64)> = {
            let mut p = Vec::new();
            let cs = if interleaved { 1 } else { offsets.len() };
            for t in 0..nt {
                for (c, &o) in offsets.iter().enumerate().take(cs) {
                    for z in 0..nz {
                        let mut coords = Vec::new();
                        for a in &axes {
                            match *a {
                                "t" => coords.push(t),
                                "c" => coords.push(c as u64),
                                "z" => coords.push(z),
                                _ => {}
                            }
                        }
                        p.push((coords, o + t * st + z * sz));
                    }
                }
            }
            p
        };
        let mut piece = 0u64;
        for (f, field) in fields.iter().enumerate() {
            let mut meta = obj(&im["meta"]);
            if !field_dims.is_empty() {
                let index = arr(&field[0]);
                let mut m = Map::new();
                for (d, i) in field_dims.iter().zip(index) {
                    m.insert(d.to_string(), i.clone());
                }
                meta.insert("field".into(), J::Object(m));
            }
            if let Some(tile) = tiles.get(f) {
                meta.insert("tile".into(), tile.clone());
            }
            let name = image_name(&im["name"], (!field_dims.is_empty()).then_some((word, f)));
            let mut ome = image_ome(&axes, &units, std::slice::from_ref(&scale), Some(&name), None)?;
            if let Some(o) = &omero {
                ome["omero"] = o.clone();
            }
            let mut attributes = Map::new();
            attributes.insert("ome".into(), ome);
            refs.out.json(&format!("{s}/zarr.json"), &group_doc(declare(attributes, "lif", None, Some(J::Object(meta)))));
            refs.out.json(&format!("{s}/0/zarr.json"), &array);
            let at = base + n(&field[1]);
            let prefix = format!("{s}/0/c/");
            for (coords, o) in &planes {
                for j in 0..rows / band {
                    let first = at + o + j * band * y_stride;
                    let parts: Vec<Part> = if y_stride == row {
                        vec![Part::Src(first, band * row)]
                    } else {
                        (0..band).map(|r| Part::Src(first + r * y_stride, row)).collect()
                    };
                    let k = key(&prefix, coords.iter().copied().chain([j, 0]));
                    refs.chunk(k, start, length, form, Some(parts), piece)?;
                    piece += 1;
                    chunks += 1;
                }
            }
            s += 1;
        }
    }
    let mut out = refs.out;
    out.summary = json!({"version": facts["version"], "images": arr(&facts["images"]).len(), "series": series,
                         "blocks": facts["blocks"], "chunks": chunks});
    Ok(out)
}
