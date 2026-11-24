//! The IMS image projection (spec/zarrify/ims.md §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ims.md#4-output>) from a valid IR: one
//! image at the root, an array per resolution level, each chunk the IR's data element of the
//! dataset's chunk (spec/zarrify/ims.md §7.8
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ims.md#78-chunk-references>), made from
//! the parser's description of the file (§7.10
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ims.md#710-the-description>) through
//! refkv's plan of it (`plan`).

use crate::error::Error;

use super::{along, arr, key, n, obj, strs, translations};
use crate::hdf5;
use crate::ir::Ir;
use crate::ims::{decimals, integer, jf, length_unit, timestamp, unit_rgb, WS};
use crate::out::{array_json, image_ome, omero_channel, omero_json, omero_window, root_json, Out, Part};
use crate::refs::Refs;
use serde_json::{json, Map, Value as J};
use std::collections::HashMap;

/// A level's image size (z, y, x), chunk shape and filters, as the description gives them.
type Level = ([u64; 3], [u64; 3], Vec<u64>);

fn triple(v: &J) -> [u64; 3] {
    let a: Vec<u64> = arr(v).iter().map(n).collect();
    [a.first().copied().unwrap_or(1), a.get(1).copied().unwrap_or(1), a.get(2).copied().unwrap_or(1)]
}

/// The (Zarr data type, byte order or None for one byte, integer range) of the description's
/// datatype (spec/zarrify/ims.md §2.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ims.md#22-the-imaris-layout>).
fn data_type(dt: &J) -> (String, Option<String>, Option<(i64, i64)>) {
    let bits = 8 * n(&dt["size"]);
    let order = dt["byte_order"].as_str().unwrap_or("little").to_string();
    if n(&dt["class"]) != 0 {
        return ("float32".into(), Some(order), None);
    }
    let o = (bits > 8).then_some(order);
    if dt["signed"].as_bool().unwrap_or(false) {
        (format!("int{bits}"), o, Some((-(1i64 << (bits - 1)), (1i64 << (bits - 1)) - 1)))
    } else {
        (format!("uint{bits}"), o, Some((0, (1i64 << bits) - 1)))
    }
}

/// A channel's omero color (spec/zarrify/ims.md §4
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ims.md#4-output>): the base color, or
/// the color table's top entry first when `ColorMode` is `TableColor`, else `FFFFFF`.
fn color(ch: &J) -> String {
    let values = |v: &J| arr(v).iter().filter_map(J::as_f64).collect::<Vec<f64>>();
    let base = ch["color"].as_array().and_then(|_| unit_rgb(Some(&values(&ch["color"]))));
    let table_first = ch["color_mode"].as_str().is_some_and(|m| m.trim_matches(WS) == "TableColor");
    if base.is_some() && !table_first {
        return base.unwrap();
    }
    let table = integer(ch["color_table_length"].as_str()).and_then(|l| decimals(ch["color_table"].as_str(), 3 * l));
    let top = table.as_ref().and_then(|t| unit_rgb(Some(&t[t.len() - 3..])));
    top.or(base).unwrap_or_else(|| "FFFFFF".into())
}

/// refkv's plan of the image (spec/zarrify/ims.md §3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ims.md#3-metadata>, §4
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ims.md#4-output>), from the description
/// alone: the axes, units, scales, name, translations and omero channels; the data type; each
/// level's array (`shape`, `chunks`, `codecs`, and `sizes`, `chunk` and `trim`, the bytes of a
/// chunk's Fletcher32 checksum); and the summary.
pub fn plan(d: &J) -> J {
    let rules = crate::rules::of("ims");
    let max_name = rules.limit("max_name") as usize;
    let level_info: Vec<Level> = arr(&d["levels"]).iter()
        .map(|l| (triple(&l["image_size"]), triple(&l["chunk"]), arr(&l["filters"]).iter().map(n).collect()))
        .collect();
    let (levels, times, channels) = (level_info.len() as u64, n(&d["times"]), n(&d["channels"]));
    let data_type = data_type(&d["datatype"]);
    let info = &d["info"];
    let image = &info["image"];
    let ext = |k: &str, i: usize| image[k][i].as_f64();
    let name = image["name"].as_str().filter(|s| !s.is_empty() && s.len() <= max_name);
    let chs = arr(&info["channels"]);
    let labels: Vec<String> = chs.iter().enumerate().map(|(c, ch)| match ch["name"].as_str() {
        Some(l) if !l.is_empty() && l.len() <= max_name => l.to_string(),
        _ => format!("Channel {c}"),
    }).collect();
    let mut period = None;
    if times > 1 {
        let tp = &info["time_points"];
        if let (Some(f), Some(l)) = (timestamp(tp["first"].as_str()), timestamp(tp["last"].as_str())) {
            let elapsed = (l.0 - f.0) as f64 + (l.1 - f.1);
            if elapsed > 0.0 {
                period = Some(elapsed / (times - 1) as f64);
            }
        }
    }

    let z0 = level_info.first().map_or(1, |i| i.0[0]);
    let has_z = z0 > 1 || level_info.iter().any(|i| i.1[0] > 1);
    let mut axes: Vec<&str> = Vec::new();
    if times > 1 {
        axes.push("t");
    }
    if channels > 1 {
        axes.push("c");
    }
    if has_z {
        axes.push("z");
    }
    axes.extend(["y", "x"]);
    let unit = match image["unit"].as_str() {
        None | Some("") => Some("micrometer"),
        Some(u) => length_unit(u),
    };
    let mut extent: HashMap<&str, f64> = HashMap::new();
    for (axis, i) in [("x", 0usize), ("y", 1), ("z", 2)] {
        if let (Some(lo), Some(hi)) = (ext("ext_min", i), ext("ext_max", i)) {
            let e = hi - lo;
            if e > 0.0 {
                extent.insert(axis, e);
            }
        }
    }
    let spatial: Vec<&str> = axes.iter().copied().filter(|a| matches!(*a, "z" | "y" | "x")).collect();
    let mut units = Map::new();
    units.insert("t".into(), period.map_or(J::Null, |_| json!("second")));
    for a in &spatial {
        units.insert((*a).into(), if extent.contains_key(a) { unit.map_or(J::Null, |u| json!(u)) } else { J::Null });
    }
    let mut scales = Vec::new();
    let mut arrays = Vec::new();
    for (sizes, chunk, filters) in &level_info {
        let along = |a: &str| match a {
            "z" => sizes[0],
            "y" => sizes[1],
            _ => sizes[2],
        };
        scales.push(J::Array(axes.iter().map(|a| match *a {
            "t" => period.map_or(json!(1), jf),
            "c" => json!(1),
            a => extent.get(a).map_or(json!(1), |e| jf(e / along(a) as f64)),
        }).collect()));
        let shape: Vec<u64> = axes.iter().map(|a| match *a {
            "t" => times,
            "c" => channels,
            a => along(a),
        }).collect();
        let chunks: Vec<u64> = axes.iter().map(|a| match *a {
            "t" | "c" => 1,
            "z" => chunk[0],
            "y" => chunk[1],
            _ => chunk[2],
        }).collect();
        let mut codecs = vec![match &data_type.1 {
            Some(e) => json!({"name": "bytes", "configuration": {"endian": e}}),
            None => json!({"name": "bytes"}),
        }];
        let item = data_type.1.as_ref().map_or(1, |_| if data_type.0.ends_with("16") { 2 } else { 4 });
        for f in filters {
            match *f {
                hdf5::SHUFFLE => codecs.push(json!({"name": "numcodecs.shuffle", "configuration": {"elementsize": item}})),
                hdf5::DEFLATE => codecs.push(json!({"name": "numcodecs.zlib", "configuration": {"level": 1}})),
                hdf5::LZ4 => codecs.push(json!({"name": "imagecodecs_lz4h5"})),
                _ => {}
            }
        }
        let trim = if filters.contains(&hdf5::FLETCHER32) { hdf5::CHECKSUM } else { 0 };
        arrays.push(json!({"shape": shape, "chunks": chunks, "codecs": codecs, "sizes": sizes, "chunk": chunk, "trim": trim}));
    }
    let translations = if spatial.iter().all(|a| extent.contains_key(a)) {
        let t: Vec<J> = axes.iter().map(|a| match *a {
            "x" => jf(ext("ext_min", 0).unwrap_or(0.0)),
            "y" => jf(ext("ext_min", 1).unwrap_or(0.0)),
            "z" => jf(ext("ext_min", 2).unwrap_or(0.0)),
            _ => json!(0),
        }).collect();
        J::Array(vec![J::Array(t); levels as usize])
    } else {
        J::Null
    };
    let mut omero = J::Null;
    if channels <= rules.limit("max_channels") as u64 {
        let mut out = Vec::new();
        for (k, label) in labels.iter().enumerate() {
            let rng: Option<Vec<f64>> = chs[k]["color_range"].as_array().map(|a| a.iter().filter_map(J::as_f64).collect());
            let window = match (data_type.2, rng) {
                (Some((lo, hi)), Some(r)) => Some(omero_window(json!(lo), json!(hi), jf(r[0]), jf(r[1]))),
                (Some((lo, hi)), None) => Some(omero_window(json!(lo), json!(hi), json!(lo), json!(hi))),
                (None, Some(r)) => Some(omero_window(jf(r[0]), jf(r[1]), jf(r[0]), jf(r[1]))),
                (None, None) => None,
            };
            out.push(omero_channel(label, color(&chs[k]).as_str(), window.as_ref()));
        }
        omero = omero_json(out);
    }
    let first = level_info.first().cloned().unwrap_or(([1, 1, 1], [1, 1, 1], Vec::new()));
    json!({
        "axes": axes,
        "units": units,
        "scales": scales,
        "name": name,
        "translations": translations,
        "omero": omero,
        "dtype": data_type.0,
        "arrays": arrays,
        "summary": {
            "levels": levels,
            "sizes": {"t": times, "c": channels, "z": first.0[0], "y": first.0[1], "x": first.0[2]},
            "dataType": data_type.0,
            "chunkShape": first.1,
            "compressed": level_info.iter().map(|i| !i.2.is_empty()).collect::<Vec<_>>(),
            "channels": labels,
        },
    })
}

/// The output of the file `description` describes, through refkv's plan of it (`plan`).
pub fn project(ir: &Ir, description: &J, url: &str) -> crate::error::Res<Out> {
    let planned = &plan(description);
    let axes = strs(&planned["axes"]);
    let mut ome = image_ome(&axes, &obj(&planned["units"]), arr(&planned["scales"]), Some(&planned["name"]), translations(&planned["translations"]))?;
    if !planned["omero"].is_null() {
        ome["omero"] = planned["omero"].clone();
    }
    let mut out = Out::new(url);
    out.json("zarr.json", &root_json(ome, "ims", url, None));
    let dtype = planned["dtype"].as_str().unwrap_or("");
    for (r, a) in arr(&planned["arrays"]).iter().enumerate() {
        let shape: Vec<u64> = arr(&a["shape"]).iter().map(n).collect();
        out.json(&format!("{r}/zarr.json"), &array_json(&shape, dtype, a["chunks"].clone(), a["codecs"].clone(), &axes));
    }
    let mut refs = Refs::new(ir, out);
    let arrays = arr(&planned["arrays"]);
    let mut count = 0u64;
    for im in arr(&description["data"]) {
        let (r, t, c) = (n(&im["level"]), n(&im["time"]), n(&im["channel"]));
        let Some(el) = im["chunks"].as_u64() else { continue };
        let grid: Vec<u64> = arr(&im["grid"]).iter().map(n).collect();
        let level = arrays.get(r as usize).ok_or_else(|| Error::internal("no level"))?;
        let sizes: Vec<u64> = arr(&level["sizes"]).iter().map(n).collect();
        let chunk: Vec<u64> = arr(&level["chunk"]).iter().map(n).collect();
        let mut members = ir.members(el as u32);
        members.sort_unstable();
        for (idx, start, length, form) in members {
            if form == u32::MAX {
                return Err(Error::internal("a chunk without a form"));
            }
            let coords = hdf5::unravel(idx as u128, &grid);
            if (0..3).any(|i| coords[i] as u128 * chunk[i] as u128 >= sizes[i] as u128) {
                continue; // padding, wholly outside the image
            }
            let k = key(&format!("{r}/c/"), axes.iter().map(|a| along(a, [t, c, coords[0], coords[1], coords[2]])));
            // a Fletcher32 checksum, the chunk's last bytes, is left out (spec/zarrify/ims.md §7.8
            // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ims.md#78-chunk-references>)
            let trim = n(&level["trim"]);
            let parts = (trim > 0).then(|| vec![Part::Src(start, length.saturating_sub(trim))]);
            refs.chunk(k, start, length, form, parts)?;
            count += 1;
        }
    }
    let mut out = refs.out;
    let mut summary = planned["summary"].clone();
    summary["chunks"] = json!(count);
    out.summary = summary;
    Ok(out)
}
