//! The ENVI image projection (spec/zarrify/envi.md §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#4-the-image>, the root's source
//! metadata of §5.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#51-the-root>,
//! and the chunks of the profile's §8.5
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#85-chunks>) from a valid store
//! IR and the parser's description of the raster (§8.7
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#87-the-description>): one image
//! at the root, whose array's chunks are ranges of the data file. The convention's decisions (the
//! Zarr data type, the transpose order, the codecs, the chunk cut, the axis names and the fill
//! value) are made here, by [`image`], from the header's fields as the parser read them.

use crate::error::Error;

use super::vsi::object_url;
use super::{key, n};
use crate::envi::stored_axes;
use crate::ir::Ir;
use crate::out::{array_json, bytes_codec, gzip_codecs, image_ome, root_json, Out, Part};
use crate::refs::Refs;
use serde_json::{json, Map, Value as J};

/// The image's axes, and the ENVI axis each is.
const AXES: [(&str, &str); 3] = [("c", "band"), ("y", "line"), ("x", "sample")];

/// The ENVI convention's image of a raster, decided from the parser's description.
#[derive(Debug, Clone, PartialEq)]
pub struct Image {
    /// the root's source metadata (§5.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#51-the-root>)
    pub own: J,
    /// the array's document, `0/zarr.json` (§4.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#41-the-array>),
    /// with no attributes
    pub array: J,
    /// every chunk (§8.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#85-chunks>):
    /// its coordinates along the array's axes, and its (offset, length) in the data file
    pub chunks: Vec<([u64; 3], (u64, u64))>,
    /// the header file's key, whose object URL the root names as its source
    pub header_key: String,
    /// the data file's key, which every chunk references
    pub data_key: String,
}

/// The convention's image of the raster the description describes (spec/zarrify/envi.md §4
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#4-the-image>): the Zarr data
/// type of the `data type` code (§3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#3-data-types>),
/// the axes `c`, `y`, `x` stored in the interleave's order, the codecs, the chunk cut and the fill
/// value. An error for a description no valid parse returns.
pub fn image(d: &J) -> crate::error::Res<Image> {
    let rules = crate::rules::of("envi");
    let code = n(&d["data_type"]);
    let spec = rules.entry("types", &code.to_string()).ok_or_else(|| Error::internal(format!("no ENVI data type {code}")))?;
    let data_type = spec["dtype"].as_str().ok_or_else(|| Error::internal("a data type of no Zarr data type"))?;
    let item = n(&spec["bytes"]);
    let endian = if n(&d["byte_order"]) == 1 { "big" } else { "little" };
    let interleave = d["interleave"].as_str().ok_or_else(|| Error::internal("no interleave"))?;
    let gzip = n(&d["file_compression"]) == 1;
    let offset = n(&d["header_offset"]);
    let (start, length) = (n(&d["data_range"][0]), n(&d["data_range"][1]));
    let size_of = |envi: &str| match envi {
        "band" => n(&d["bands"]),
        "line" => n(&d["lines"]),
        _ => n(&d["samples"]),
    };

    // the array (§4.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#41-the-array>):
    // `c`, `y`, `x`, stored in the interleave's order
    let axes: Vec<&str> = AXES.iter().map(|x| x.0).collect();
    let shape: Vec<u64> = AXES.iter().map(|x| size_of(x.1)).collect();
    let stored = stored_axes(interleave);
    let stored_shape: Vec<u64> = stored.iter().map(|s| size_of(s)).collect();
    // each storage axis's position among the image's
    let order: Vec<usize> = stored.iter().map(|s| AXES.iter().position(|x| x.1 == *s).unwrap_or(0)).collect();
    let mut codecs = Vec::new();
    if order != [0, 1, 2] {
        codecs.push(json!({"name": "transpose", "configuration": {"order": order}}));
    }
    codecs.push(bytes_codec(item, endian));
    if gzip {
        codecs.extend(gzip_codecs(offset, 1));
    }
    // the chunks (§8.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#85-chunks>):
    // a gzip file's stream whole, or one index of the data's outermost axis each
    let stored_chunk: Vec<u64> = if gzip { stored_shape.clone() } else { vec![1, stored_shape[1], stored_shape[2]] };
    let chunk_shape: Vec<u64> = (0..3).map(|i| stored_chunk[order.iter().position(|&o| o == i).unwrap_or(0)]).collect();
    let mut array = array_json(&shape, data_type, json!(chunk_shape), J::Array(codecs), &axes);
    if data_type.starts_with("complex") {
        // a complex fill value is its real and imaginary parts (Zarr v3)
        array["fill_value"] = json!([0, 0]);
    }
    let count = if gzip { 1 } else { stored_shape[0] };
    let slab = item * stored_shape[1] * stored_shape[2];
    let chunks = (0..count)
        .map(|i| {
            let mut coords = [0u64; 3];
            coords[order[0]] = i;
            (coords, if gzip { (start, length) } else { (start + i * slab, slab) })
        })
        .collect();

    let data_key = d["data_file"]["key"].as_str().ok_or_else(|| Error::internal("no data file"))?.to_string();
    let mut own = Map::new();
    own.insert("fields".into(), d["fields"].clone());
    own.insert("comments".into(), d["comments"].clone());
    own.insert("data_type".into(), json!(data_type));
    own.insert("interleave".into(), json!(interleave));
    if item > 1 {
        own.insert("byte_order".into(), json!(endian));
    }
    own.insert("compression".into(), json!(if gzip { "gzip" } else { "none" }));
    own.insert("data_file".into(), json!(data_key));
    let header_key = d["header_file"]["key"].as_str().ok_or_else(|| Error::internal("no header file"))?.to_string();
    Ok(Image { own: J::Object(own), array, chunks, header_key, data_key })
}

pub fn project(ir: &Ir, description: &J, url: &str) -> crate::error::Res<Out> {
    let img = image(description)?;
    let scales = json!([1, 1, 1]);
    let axes: Vec<&str> = AXES.iter().map(|x| x.0).collect();
    let ome = image_ome(&axes, &Map::new(), &[scales], None, None)?;
    let mut out = Out::new(url);
    out.json("zarr.json", &root_json(ome, "envi", &object_url(url, &img.header_key), Some(img.own.clone())));
    out.json("0/zarr.json", &img.array);

    let (start, length) = (n(&description["data_range"][0]), n(&description["data_range"][1]));
    let mut refs = Refs::new(ir, out);
    for (coords, (offset, len)) in &img.chunks {
        refs.chunk(key("0/c/", *coords), start, length, 0, Some(vec![Part::Obj(img.data_key.clone(), *offset, *len)]))?;
    }
    let mut out = refs.out;
    for k in out.entries.iter().map(|e| &e.0).chain(out.refs.iter().map(|r| &r.0)) {
        super::store::check_key(k)?;
    }
    let shape = img.array["shape"].as_array().cloned().unwrap_or_default();
    let sizes_by: Map<String, J> = axes.iter().zip(shape).map(|(x, s)| (x.to_string(), s)).collect();
    out.summary = json!({
        "sizes": sizes_by, "dataType": img.array["data_type"], "interleave": img.own["interleave"],
        "compression": img.own["compression"], "dataFile": img.data_key,
        "chunkShape": img.array["chunk_grid"]["configuration"]["chunk_shape"], "chunks": img.chunks.len(),
    });
    Ok(out)
}

/// The projection of the profile's store run (`StoreFormat`; crate::store).
pub fn store_project(ir: &Ir, description: &J, url: &str, _copied: &super::Copied, _chunking: &crate::out::Chunking) -> crate::error::Res<Out> {
    project(ir, description, url)
}
