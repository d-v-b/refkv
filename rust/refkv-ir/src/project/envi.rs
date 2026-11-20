//! The ENVI image projection (spec/zarrify/envi.md §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#4-the-image>, the root's source
//! metadata of §5.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#51-the-root>,
//! and the chunks of the profile's §8.5
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#85-chunks>) from a valid store
//! IR and its facts: one image at the root, whose array's chunks are ranges of the data file.

use super::{key, n, with_counts};
use super::vsi::object_url;
use crate::envi::stored_axes;
use crate::ir::Ir;
use crate::out::{array_json, bytes_codec, gzip_codecs, image_ome, root_json, Out, Part};
use crate::refs::Refs;
use serde_json::{json, Map, Value as J};

/// The image's axes, and the ENVI axis each is.
const AXES: [(&str, &str); 3] = [("c", "band"), ("y", "line"), ("x", "sample")];

pub fn project(ir: &Ir, facts: &J, url: &str) -> Result<Out, String> {
    let a = &facts["array"];
    let (data_type, item) = (a["data_type"].as_str().unwrap_or("uint8"), n(&a["item"]));
    let endian = a["endian"].as_str().unwrap_or("little");
    let interleave = a["interleave"].as_str().unwrap_or("bsq");
    let (start, length, gzip) = (n(&a["start"]), n(&a["length"]), a["gzip"].as_bool().unwrap_or(false));
    let data_key = facts["data_key"].as_str().unwrap_or("").to_string();
    let size_of = |envi: &str| match envi {
        "band" => n(&a["bands"]),
        "line" => n(&a["lines"]),
        _ => n(&a["samples"]),
    };

    // the array (§4.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#41-the-array>):
    // `c`, `y`, `x`, stored in the interleave's order
    let axes: Vec<&str> = AXES.iter().map(|x| x.0).collect();
    let shape: Vec<u64> = AXES.iter().map(|x| size_of(x.1)).collect();
    let stored = stored_axes(interleave);
    let stored_shape: Vec<u64> = stored.iter().map(|s| size_of(s)).collect();
    // each storage axis's position among the image's
    let order: Vec<usize> = stored.iter().map(|s| AXES.iter().position(|x| x.1 == *s).unwrap_or(0)).collect();

    let scales = json!([1, 1, 1]);
    let ome = image_ome(&axes, &Map::new(), &[scales], None, None)?;
    let own = facts["own"].clone();
    let header_url = object_url(url, facts["header_key"].as_str().unwrap_or(""));
    let mut out = Out::new(url);
    out.json("zarr.json", &root_json(ome, "envi", &header_url, Some(own)));

    let mut codecs = Vec::new();
    if order != [0, 1, 2] {
        codecs.push(json!({"name": "transpose", "configuration": {"order": order}}));
    }
    codecs.push(bytes_codec(item, endian));
    if gzip {
        codecs.extend(gzip_codecs(n(&a["offset"]), 1));
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
    out.json("0/zarr.json", &array);

    let mut refs = Refs::new(ir, out);
    let count = if gzip { 1 } else { stored_shape[0] };
    let slab = item * stored_shape[1] * stored_shape[2];
    for i in 0..count {
        let mut coords = [0u64; 3];
        coords[order[0]] = i;
        let range = if gzip { (start, length) } else { (start + i * slab, slab) };
        refs.chunk(key("0/c/", coords), start, length, 0, Some(vec![Part::Obj(data_key.clone(), range.0, range.1)]))?;
    }
    let mut out = refs.out;
    for k in out.entries.iter().map(|e| &e.0).chain(out.refs.iter().map(|r| &r.0)) {
        super::store::check_key(k)?;
    }
    let sizes_by: Map<String, J> = axes.iter().zip(&shape).map(|(x, s)| (x.to_string(), json!(s))).collect();
    out.summary = with_counts(json!({
        "sizes": sizes_by, "dataType": data_type, "interleave": interleave, "compression": if gzip { "gzip" } else { "none" },
        "dataFile": data_key, "chunkShape": chunk_shape, "chunks": count,
    }), facts);
    Ok(out)
}

/// The projection of the profile's store run (`StoreFormat`; crate::store).
pub fn store_project(ir: &Ir, facts: &J, url: &str) -> Result<Out, String> {
    project(ir, facts, url)
}
