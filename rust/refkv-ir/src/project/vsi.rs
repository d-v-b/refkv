//! The VSI projection (spec/zarrify/vsi.md §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#4-the-images>–§6.1
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#61-the-root>, the profile's
//! §9.4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#94-chunks>) from a valid
//! store IR: a bioformats2raw layout of one OME-Zarr image per stack with pixels, each level's
//! chunks the tile file's tiles, and the macro image from the `.vsi` file's own IFD; the root
//! carries the metadata tree as JSON. The facts hold what the parser read; the projection
//! checks only the archive's own limits.

use super::{along, arr, image_axes, key, n, translation, with_counts, Copied};
use crate::ir::Ir;
use crate::out::{array_json, bioformats2raw, bytes_codec, declared_group, image_ome, transpose_codec, Out, Part, BGRA, RGBA};
use crate::refs::Refs;
use serde_json::{json, Map, Value as J};

const PROFILE: &str = "vsi";

/// The URL of the object with relative key `key` (spec/zarrify.md §1.4
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>), as the hosts
/// make it: the unreserved bytes and `!$&'()*+,;=:@/` as they are, every other percent-encoded.
pub fn object_url(store_url: &str, key: &str) -> String {
    let mut s = String::from(store_url);
    for &b in key.as_bytes() {
        if b.is_ascii_alphanumeric() || b"-._~!$&'()*+,;=:@/".contains(&b) {
            s.push(b as char);
        } else {
            s.push_str(&format!("%{b:02X}"));
        }
    }
    s
}

/// The codecs of a tile file's arrays (§4.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#41-the-pyramid>): `transpose`
/// for samples, then the compression's.
fn codecs(codec: &str, bytes: u64, samples: u64, axes: &[&str]) -> J {
    let mut out = if samples > 1 { vec![transpose_codec(axes)] } else { vec![] };
    match codec {
        "jpeg" => out.push(json!({"name": "imagecodecs_jpeg"})),
        "jpeg2000" => out.push(json!({"name": "imagecodecs_jpeg2k"})),
        "ljpeg" => out.push(json!({"name": "imagecodecs_ljpeg"})),
        _ => out.push(bytes_codec(bytes, "little")),
    }
    J::Array(out)
}

/// The window of a channel (§4.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#42-channels>): 0 to the type's
/// largest value, with the display limits (clamped) as start and end; none for a type without
/// a largest value.
fn window(data_type: &str, display: Option<(f64, f64)>) -> Option<J> {
    let (lo, hi): (f64, f64) = match data_type {
        "uint8" => (0.0, 255.0),
        "int8" => (-128.0, 127.0),
        "uint16" => (0.0, 65535.0),
        "int16" => (-32768.0, 32767.0),
        "uint32" => (0.0, 4294967295.0),
        "int32" => (-2147483648.0, 2147483647.0),
        _ => return None,
    };
    let (start, end) = match display {
        Some((a, z)) if a.is_finite() && z.is_finite() && z > a => (a.clamp(lo, hi), z.clamp(lo, hi)),
        _ => (lo, hi),
    };
    Some(json!({"min": lo, "max": hi, "start": start, "end": end}))
}

fn pair(v: &J) -> Option<(f64, f64)> {
    let a = v.as_array()?;
    Some((a.first()?.as_f64()?, a.get(1)?.as_f64()?))
}

/// The `omero` channels of an image: the samples' (R, G, B, or B, G, R for a raw BGR file),
/// else one per channel of the channel dimension, named and colored by the tree.
fn omero(im: &J, samples: u64, bgr: bool, c_size: u64, data_type: &str) -> J {
    let display = pair(&im["display"]);
    let mut out = Vec::new();
    if samples > 1 {
        let letters = if bgr { &BGRA } else { &RGBA };
        for (l, color) in letters.iter().take(samples as usize) {
            let mut ch = json!({"label": l, "color": color, "active": true});
            if let Some(w) = window(data_type, display) {
                ch["window"] = w;
            }
            out.push(ch);
        }
    } else {
        let channels = arr(&im["channels"]);
        for c in 0..c_size {
            let info = channels.get(c as usize);
            let label = info.and_then(|i| i["name"].as_str()).filter(|s| !s.is_empty()).map(str::to_string).unwrap_or_else(|| format!("C{c}"));
            let color = info.and_then(|i| i["color"].as_str()).unwrap_or("FFFFFF");
            let mut ch = json!({"label": label, "color": color, "active": true});
            if let Some(w) = window(data_type, info.and_then(|i| pair(&i["display"])).or(display)) {
                ch["window"] = w;
            }
            out.push(ch);
        }
    }
    json!({"channels": out})
}

/// One image's group and arrays: a stack's pyramid (`ets`), or an IFD image (`ifd`).
fn image(refs: &mut Refs, k: usize, im: &J, vsi_key: &str) -> Result<J, String> {
    let path = k.to_string();
    let name = im["name"].as_str().unwrap_or("");
    let unit = im["unit_name"].as_str().map(str::to_string);
    let scale = pair(&im["scale"]).filter(|_| unit.is_some());
    let origin = pair(&im["origin"]).filter(|(x, y)| x.is_finite() && y.is_finite() && unit.is_some());
    let mut own = Map::new();
    for m in ["id", "type", "type_name", "width", "height", "dimension_sizes", "dimensions", "scale", "unit", "origin", "z_step", "z_unit", "display", "channels", "creation_time", "magnification", "default_ifd", "coarse_ifd"] {
        if im["kind"] == "stack" {
            own.insert(m.into(), im[m].clone());
        }
    }
    own.insert("name".into(), json!(name));
    let summary;
    if let Some(ets) = im.get("ets").filter(|e| !e.is_null()) {
        let (data_type, bytes, samples) = (ets["data_type"].as_str().unwrap_or("uint8").to_string(), n(&ets["bytes"]), n(&ets["samples"]));
        let bgr = ets["bgr"].as_bool().unwrap_or(false);
        let codec = ets["codec"].as_str().unwrap_or("raw");
        let key_ets = ets["key"].as_str().unwrap_or("").to_string();
        let (tw, th) = (n(&ets["tile"][0]), n(&ets["tile"][1]));
        let sizes = &ets["sizes"];
        let (size_t, size_c, size_z) = (n(&sizes["t"]).max(1), n(&sizes["c"]).max(1), n(&sizes["z"]).max(1));
        let c_axis = size_c > 1 || samples > 1;
        let axes = image_axes(size_t > 1, c_axis, size_z > 1);
        let levels = arr(&ets["levels"]);
        let (w0, h0) = (n(&levels[0][0]) as f64, n(&levels[0][1]) as f64);
        let z_unit = im["z_unit_name"].as_str().map(str::to_string);
        let z_step = im["z_step"].as_f64().filter(|v| v.is_finite() && *v > 0.0 && z_unit.is_some());
        let mut units: Map<String, J> = Map::new();
        if let Some(u) = &unit {
            if scale.is_some() {
                units.insert("x".into(), json!(u));
                units.insert("y".into(), json!(u));
            }
        }
        if let (Some(u), Some(_)) = (&z_unit, z_step) {
            units.insert("z".into(), json!(u));
        }
        let mut scales = Vec::with_capacity(levels.len());
        for lv in levels {
            let (w, h) = (n(&lv[0]) as f64, n(&lv[1]) as f64);
            let s: Vec<J> = axes.iter().map(|a| match *a {
                "x" => json!(scale.map_or(1.0, |(sx, _)| sx * (w0 / w.max(1.0)))),
                "y" => json!(scale.map_or(1.0, |(_, sy)| sy * (h0 / h.max(1.0)))),
                "z" => json!(z_step.unwrap_or(1.0)),
                _ => json!(1),
            }).collect();
            scales.push(J::Array(s));
        }
        let trs = origin.map(|(ox, oy)| vec![translation(&axes, [json!(ox), json!(oy), json!(0)]); levels.len()]);
        let mut ome = image_ome(&axes, &units, &scales, Some(&json!(name)), trs.as_deref())?;
        ome["omero"] = omero(im, samples, bgr, size_c, &data_type);
        let mut a = Map::new();
        a.insert("ome".into(), ome);
        own.insert("tile_file".into(), json!(key_ets));
        own.insert("ets".into(), ets["header"].clone());
        own.insert("levels".into(), json!(levels));
        own.insert("tiles_outside".into(), ets["tiles_outside"].clone());
        refs.out.json(&format!("{path}/zarr.json"), &declared_group(a, PROFILE, None, Some(J::Object(own))));
        let codecs = codecs(codec, bytes, samples, &axes);
        let mut grids = Vec::new();
        for (l, lv) in levels.iter().enumerate() {
            let (w, h) = (n(&lv[0]), n(&lv[1]));
            let shape: Vec<u64> = axes.iter().map(|a| along(a, [size_t, size_c.max(samples), size_z, h, w])).collect();
            let chunk: Vec<u64> = axes.iter().map(|a| along(a, [1, if samples > 1 { samples } else { 1 }, 1, th, tw])).collect();
            let mut doc = array_json(&shape, &data_type, json!(chunk), codecs.clone(), &axes);
            doc["fill_value"] = ets["fill"].clone();
            refs.out.json(&format!("{path}/{l}/zarr.json"), &doc);
            grids.push(json!([w.div_ceil(tw), h.div_ceil(th)]));
        }
        let ir = refs.ir;
        let mut count = 0u64;
        for t in arr(&ets["tiles"]) {
            let v: Vec<u64> = arr(t).iter().map(n).collect();
            let [el, x, y, l, ti, ci, zi] = v[..] else { return Err("internal: a tile is not 7 numbers".into()) };
            let (Some(&start), Some(&length)) = (ir.start.get(el as usize), ir.len.get(el as usize)) else {
                return Err(format!("internal: no element {el}"));
            };
            let coords = axes.iter().map(|a| along(a, [ti, if samples > 1 { 0 } else { ci }, zi, y, x]));
            refs.chunk(key(&format!("{path}/{l}/c/"), coords), start, length, 0, Some(vec![Part::Obj(key_ets.clone(), start, length)]), 0)?;
            count += 1;
        }
        summary = json!({"name": name, "kind": "stack", "source": "tiles", "id": im["id"], "dataType": data_type, "samples": samples, "codec": codec,
                         "axes": axes, "sizes": {"t": size_t, "c": size_c, "z": size_z}, "levels": levels, "grids": grids,
                         "chunks": count, "tilesOutside": ets["tiles_outside"]});
    } else {
        // an IFD image (§4.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#43-the-macro-image>)
        let ifd = &im["ifd"];
        let (w, h, samples, bits, compression) = (n(&ifd["width"]), n(&ifd["height"]), n(&ifd["samples"]), n(&ifd["bits"]), n(&ifd["compression"]));
        let data_type = if bits == 16 { "uint16" } else { "uint8" };
        let axes: Vec<&str> = if samples > 1 { vec!["c", "y", "x"] } else { vec!["y", "x"] };
        let shape: Vec<u64> = if samples > 1 { vec![samples, h, w] } else { vec![h, w] };
        let mut units: Map<String, J> = Map::new();
        if let (Some(u), Some(_)) = (&unit, scale) {
            units.insert("x".into(), json!(u));
            units.insert("y".into(), json!(u));
        }
        let scales = vec![J::Array(axes.iter().map(|a| match (*a, scale) {
            ("x", Some((sx, _))) => json!(sx),
            ("y", Some((_, sy))) => json!(sy),
            _ => json!(1),
        }).collect())];
        let trs = origin.map(|(ox, oy)| vec![translation(&axes, [json!(ox), json!(oy), json!(0)])]);
        let mut ome = image_ome(&axes, &units, &scales, Some(&json!(name)), trs.as_deref())?;
        ome["omero"] = omero(im, samples, false, 1, data_type);
        let mut a = Map::new();
        a.insert("ome".into(), ome);
        own.insert("ifd".into(), json!({"index": ifd["index"], "width": w, "height": h, "samples": samples, "bits": bits, "compression": compression, "photometric": ifd["photometric"]}));
        refs.out.json(&format!("{path}/zarr.json"), &declared_group(a, PROFILE, None, Some(J::Object(own))));
        let mut codecs = if samples > 1 { vec![transpose_codec(&axes)] } else { vec![] };
        if compression == 7 {
            codecs.push(json!({"name": "imagecodecs_jpeg"}));
        } else {
            codecs.push(bytes_codec(bits / 8, "little"));
        }
        let doc = array_json(&shape, data_type, json!(shape), J::Array(codecs), &axes);
        refs.out.json(&format!("{path}/0/zarr.json"), &doc);
        let strips = arr(&ifd["strips"]);
        let prefix: Vec<u8> = arr(&ifd["prefix"]).iter().map(|v| n(v) as u8).collect();
        let mut parts: Vec<Part> = Vec::new();
        if compression == 7 {
            let (o, len) = (n(&strips[0][1]), n(&strips[0][2]));
            if prefix.is_empty() || len <= 2 {
                parts.push(Part::Obj(vsi_key.to_string(), o, len));
            } else {
                parts.push(Part::Obj(vsi_key.to_string(), o, 2));
                parts.push(refs.out.shared(&prefix));
                parts.push(Part::Obj(vsi_key.to_string(), o + 2, len - 2));
            }
        } else {
            // the strips, contiguous ones as one range
            for s in strips {
                let (o, len) = (n(&s[1]), n(&s[2]));
                match parts.last_mut() {
                    Some(Part::Obj(_, po, pn)) if *po + *pn == o => *pn += len,
                    _ => parts.push(Part::Obj(vsi_key.to_string(), o, len)),
                }
            }
        }
        let (el, start, length) = (n(&strips[0][0]), n(&strips[0][1]), n(&strips[0][2]));
        let _ = el;
        refs.chunk(key(&format!("{path}/0/c/"), vec![0; axes.len()]), start, length, 0, Some(parts), 0)?;
        summary = json!({"name": name, "kind": im["kind"], "source": "ifd", "id": im["id"], "dataType": data_type, "samples": samples, "codec": if compression == 7 { "jpeg" } else { "raw" },
                         "axes": axes, "levels": [[w, h]], "chunks": 1});
    }
    Ok(summary)
}

/// The VSI hierarchy: the root (the tree), `OME/zarr.json`, and one image per stack with pixels
/// and for the macro image.
pub fn project(ir: &Ir, facts: &J, url: &str, _copied: &Copied) -> Result<Out, String> {
    let images = arr(&facts["images"]);
    let vsi_key = facts["vsi"]["key"].as_str().unwrap_or("").to_string();
    let mut out = Out::new(url);
    let own = json!({
        "file": facts["vsi"],
        "stacks": facts["stacks"],
        "tree": facts["tree"],
    });
    let vsi_url = object_url(url, &vsi_key);
    bioformats2raw(&mut out, PROFILE, &vsi_url, own, images.len(), !images.is_empty());
    let mut refs = Refs::new(ir, out);
    let mut summaries = Vec::with_capacity(images.len());
    for (k, im) in images.iter().enumerate() {
        summaries.push(image(&mut refs, k, im, &vsi_key)?);
    }
    let mut out = refs.out;
    for k in out.entries.iter().map(|e| &e.0).chain(out.refs.iter().map(|r| &r.0)) {
        super::store::check_key(k)?;
    }
    let chunks: u64 = summaries.iter().map(|s| n(&s["chunks"])).sum();
    out.summary = with_counts(json!({"images": summaries, "chunks": chunks, "dataSources": out.data.len()}), facts);
    Ok(out)
}
