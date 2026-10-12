//! The IMS image projection (spec/virtualize/ims.md §4
//! <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/ims.md#4-output>) from a valid IR: one
//! image at the root, an array per resolution level, each chunk the IR's data element of the
//! dataset's chunk (spec/virtualize/ims.md §8.8
//! <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/ims.md#88-chunk-references>). The
//! parser decided the layout and the keys (the facts); this writes them.

use super::{arr, n, obj, strs, translations};
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
    let chunks = arr(&facts["chunks"]);
    for c in chunks {
        let (key, el) = (c[0].as_str().unwrap_or(""), n(&c[1]) as usize);
        let (Some(&start), Some(&length)) = (ir.start.get(el), ir.len.get(el)) else {
            return Err(format!("internal: no element {el}"));
        };
        let form = ir.form_id(el as u32).ok_or("internal: a chunk without a form")?;
        refs.chunk(key.to_string(), start, length, form, None, 0)?;
    }
    let mut out = refs.out;
    let mut summary = facts["summary"].clone();
    summary["chunks"] = json!(chunks.len());
    out.summary = summary;
    Ok(out)
}
