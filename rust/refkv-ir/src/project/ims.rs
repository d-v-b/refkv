//! The IMS image projection (spec/virtualize/ims.md §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/ims.md#4-output>) from a valid IR: one
//! image at the root, an array per resolution level, each chunk the IR's data element of the
//! dataset's chunk (spec/virtualize/ims.md §8.8
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/ims.md#88-chunk-references>). The
//! parser decided the layout and the keys (the facts); this writes them.

use super::{arr, key, n, obj, strs, translations};
use crate::hdf5;
use crate::ir::Ir;
use crate::out::{array_json, image_ome, root_json, Out};
use crate::refs::Refs;
use serde_json::{json, Value as J};

pub fn project(ir: &Ir, facts: &J, url: &str) -> Result<Out, String> {
    let axes = strs(&facts["axes"]);
    let mut ome = image_ome(&axes, &obj(&facts["units"]), arr(&facts["scales"]), Some(&facts["name"]), translations(&facts["translations"]))?;
    if !facts["omero"].is_null() {
        ome["omero"] = facts["omero"].clone();
    }
    let mut out = Out::new(url);
    out.json("zarr.json", &root_json(ome, "ims", url, None));
    let dtype = facts["dtype"].as_str().unwrap_or("");
    for (r, a) in arr(&facts["arrays"]).iter().enumerate() {
        let shape: Vec<u64> = arr(&a["shape"]).iter().map(n).collect();
        out.json(&format!("{r}/zarr.json"), &array_json(&shape, dtype, a["chunks"].clone(), a["codecs"].clone(), &axes));
    }
    let mut refs = Refs::new(ir, out);
    let arrays = arr(&facts["arrays"]);
    let mut count = 0u64;
    for im in arr(&facts["images"]) {
        let (r, t, c) = (n(&im[0]), n(&im[1]), n(&im[2]));
        let Some(el) = im[3].as_u64() else { continue };
        let grid: Vec<u64> = arr(&im[4]).iter().map(n).collect();
        let level = arrays.get(r as usize).ok_or("internal: no level")?;
        let sizes: Vec<u64> = arr(&level["sizes"]).iter().map(n).collect();
        let chunk: Vec<u64> = arr(&level["chunk"]).iter().map(n).collect();
        let mut members = ir.members(el as u32);
        members.sort_unstable();
        for (idx, start, length, form) in members {
            if form == u32::MAX {
                return Err("internal: a chunk without a form".into());
            }
            let coords = hdf5::unravel(idx as u128, &grid);
            if (0..3).any(|i| coords[i] as u128 * chunk[i] as u128 >= sizes[i] as u128) {
                continue; // padding, wholly outside the image
            }
            let k = key(&format!("{r}/c/"), axes.iter().map(|a| match *a {
                "t" => t,
                "c" => c,
                "z" => coords[0],
                "y" => coords[1],
                _ => coords[2],
            }));
            refs.chunk(k, start, length, form, None, 0)?;
            count += 1;
        }
    }
    let mut out = refs.out;
    let mut summary = facts["summary"].clone();
    summary["chunks"] = json!(count);
    out.summary = summary;
    Ok(out)
}
