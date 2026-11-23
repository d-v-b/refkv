//! refkv's plan of a VSI slide (spec/zarrify/vsi.md §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#4-the-images>, §5
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#5-scales-units-and-position>): the
//! decisions of refkv's convention, made from the parser's description of the slide
//! (spec/zarrify/vsi.md §9.6 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#96-the-description>)
//! and nothing else: which stacks and IFDs are images and in what order, and which IFD images are not
//! presented, with the first reason that applied (§4.4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#44-ifd-images-that-are-not-presented>);
//! a pyramid's levels, its tiles inside them, its data type, fill value and codec (§4.1
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#41-the-pyramid>); an IFD image's
//! strips and JPEG prefix (§4.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#43-the-macro-image>);
//! the channels' names, colors and display limits (§4.2
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#42-channels>); and the units the
//! table `units` names. The plan is public data, which the convention's projection (`project/vsi.rs`)
//! reads; it belongs to refkv's convention, not to the IR.

use crate::error::Error;

use super::{tiff, MACRO_NAME};
use crate::fed::Word;
use crate::out::{payload_size, MAX_PAYLOAD};
use crate::rules::{self, num, Rules};
use crate::tiff::plan::unbase64;
use serde_json::{json, Value as J};

/// A tree value's numbers, as the parser reads a field "of several numbers": a number is a list of
/// one, an array its numbers; anything else, or an array with another member, none.
fn numbers(v: &J) -> Option<Vec<f64>> {
    match v {
        J::Number(n) => Some(vec![n.as_f64()?]),
        J::Array(a) => a.iter().map(J::as_f64).collect(),
        _ => None,
    }
}

/// Two numbers `[a, b]`, from a value of at least two.
fn pair(v: &J) -> Option<(f64, f64)> {
    numbers(v).filter(|v| v.len() >= 2).map(|v| (v[0], v[1]))
}

fn pair_json(p: Option<(f64, f64)>) -> J {
    p.map_or(J::Null, |(a, b)| json!([a, b]))
}

fn index(v: &J) -> Option<usize> {
    v.as_u64().map(|x| x as usize)
}

/// A stack, as the convention reads its described fields (spec/zarrify/vsi.md §2.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#22-stacks>).
struct Stack<'a> {
    d: &'a J,
    name: String,
    stack_type: Option<i64>,
    width: Option<u64>,
    height: Option<u64>,
    external: bool,
    /// (coordinate, meaning), by coordinate
    dims: Vec<(usize, String)>,
}

impl<'a> Stack<'a> {
    fn of(d: &'a J) -> Self {
        let boundary = numbers(&d["image_boundary"]).filter(|v| v.len() >= 4);
        let mut dims: Vec<(usize, String)> = d["dimensions"]
            .as_array()
            .map(|a| a.iter().map(|x| (2 + x["index"].as_u64().unwrap_or(0) as usize, x["meaning"].as_str().unwrap_or("").to_string())).collect())
            .unwrap_or_default();
        dims.sort_by_key(|x| x.0);
        Stack {
            d,
            name: d["name"].as_str().unwrap_or("").to_string(),
            stack_type: d["type"].as_f64().map(|t| t as i64),
            width: boundary.as_ref().map(|v| v[2].max(0.0) as u64),
            height: boundary.as_ref().map(|v| v[3].max(0.0) as u64),
            external: d["has_external_file"].as_f64().is_some_and(|v| v != 0.0),
            dims,
        }
    }
}

/// refkv's plan of the slide a description describes: `file` and `stacks`, the root's records of the
/// `.vsi` file and of every stack; `images`, the images in the order of spec/zarrify/vsi.md §4
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#4-the-images>, each a pyramid's (`ets`) or
/// an IFD image's (`ifd`) with its stack's fields as the convention reads them; and `not_presented`,
/// the IFD images left out, with their reasons.
pub fn plan(description: &J) -> crate::error::Res<J> {
    let rules = rules::of("vsi");
    let d = description;
    let stacks: Vec<Stack> = d["stacks"].as_array().map(|a| a.iter().map(Stack::of).collect()).unwrap_or_default();
    let tile_files = d["tile_files"].as_array().map(Vec::as_slice).unwrap_or_default();
    let unit_name = |u: &J| u.as_str().and_then(|u| rules.table("units").get(u).cloned());
    let mut images = Vec::new();
    let mut left_out = Vec::new();
    for s in &stacks {
        let sd = s.d;
        let z_step = sd["z_increment"]["value"].as_f64();
        let z_unit = &sd["z_increment"]["unit"];
        let channels: Vec<J> = sd["channels"]
            .as_array()
            .map(|a| {
                a.iter()
                    .map(|c| {
                        let name = c["dimension_name"].as_str().or_else(|| c["channel_name"].as_str());
                        json!({"name": name, "channel_name": c["channel_name"], "color": c["lut_color"],
                               "display": pair_json(pair(&c["display_limits"])), "wavelength": c["wavelength"].as_f64()})
                    })
                    .collect()
            })
            .unwrap_or_default();
        let mut im = json!({
            "kind": "stack", "id": sd["id"], "name": s.name, "type": s.stack_type, "type_name": sd["type_name"],
            "width": s.width, "height": s.height,
            "dimension_sizes": numbers(&sd["dimension_sizes"]).unwrap_or_default().iter().map(|v| *v as i64).collect::<Vec<_>>(),
            "dimensions": s.dims.iter().map(|(c, m)| json!({"coordinate": c, "meaning": m})).collect::<Vec<_>>(),
            "scale": pair_json(pair(&sd["frame_scale"])), "unit": sd["frame_unit"].as_str(), "unit_name": unit_name(&sd["frame_unit"]),
            "origin": pair_json(pair(&sd["frame_origin"])),
            "z_step": z_step, "z_unit": z_unit.as_str(), "z_unit_name": unit_name(z_unit),
            "display": pair_json(pair(&sd["display_limits"])),
            "channels": channels,
            "creation_time": sd["creation_time"].as_f64().map(|v| v as i64), "magnification": sd["magnification"].as_f64(),
            "default_ifd": sd["default_ifd"], "coarse_ifd": sd["coarse_ifd"],
        });
        if let Some(f) = index(&sd["tile_file"]) {
            let file = tile_files.get(f).ok_or_else(|| Error::internal(format!("no tile file {f}")))?;
            im["ets"] = pyramid(rules, s, file)?;
        } else if let Some(k) = index(&sd["default_ifd"]) {
            match ifd_image(rules, d, k, &s.name) {
                Ok(ifd) => im["ifd"] = ifd,
                Err(reason) => {
                    left_out.push(json!({"id": sd["id"], "name": s.name, "ifd": k, "reason": reason.to_string()}));
                    continue;
                }
            }
        } else {
            continue;
        }
        images.push(im);
    }
    // the macro image: the collection volume's default sample IFD
    if let Some(k) = index(&d["macro_ifd"]) {
        match ifd_image(rules, d, k, MACRO_NAME) {
            Ok(ifd) => images.push(json!({"kind": "macro", "name": MACRO_NAME, "ifd": ifd})),
            Err(reason) => left_out.push(json!({"id": null, "name": MACRO_NAME, "ifd": k, "reason": reason.to_string()})),
        }
    }
    let ifds = d["ifds"].as_array().map(Vec::as_slice).unwrap_or_default();
    Ok(json!({
        "file": {"key": d["key"], "size": d["size"], "ifds": ifds.iter().filter(|f| f["exif"] == false).count()},
        "stacks": stacks.iter().map(|s| json!({
            "id": s.d["id"], "name": s.name, "type": s.stack_type, "external": s.external, "width": s.width, "height": s.height,
            "tile_file": index(&s.d["tile_file"]).and_then(|f| tile_files.get(f)).map(|f| f["key"].clone()),
        })).collect::<Vec<_>>(),
        "images": images,
        "not_presented": left_out,
    }))
}

/// The unsigned values of an IFD's described tag, where the profile reads them.
fn ints(ifd: &J, tag: u16) -> Option<Vec<u64>> {
    ifd["tags"][tag.to_string()]["value"].as_array()?.iter().map(J::as_u64).collect()
}

/// The (offset, length) pairs of an IFD's table of `offsets` and `counts`, where it has both, of
/// one length.
fn segments(ifd: &J, offsets: u16, counts: u16) -> Option<Vec<(u64, u64)>> {
    let (o, c) = (ints(ifd, offsets)?, ints(ifd, counts)?);
    (o.len() == c.len()).then(|| o.into_iter().zip(c).collect())
}

/// IFD `k` of the chain as an image the layout can present, or the first reason it cannot (§4.4
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#44-ifd-images-that-are-not-presented>):
/// its size, samples, bits, compression and photometric interpretation, its strips as
/// `[element, offset, length]`, and its JPEG prefix.
fn ifd_image(rules: &Rules, d: &J, k: usize, what: &str) -> crate::error::Res<J> {
    let ifd = d["ifds"]
        .as_array()
        .and_then(|a| a.iter().find(|f| index(&f["index"]) == Some(k) && f["exif"] == false))
        .ok_or_else(|| Error::rejected(format!("{what}: the .vsi file has no IFD {k}")))?;
    let g = |t: u16| ints(ifd, t).and_then(|v| v.first().copied());
    let (width, height) = (g(256).unwrap_or(0), g(257).unwrap_or(0));
    let compression = g(259).unwrap_or(1);
    let samples = g(277).unwrap_or(1);
    let bits = g(258).unwrap_or(1);
    let planar = g(284).unwrap_or(1);
    let format = g(339).unwrap_or(1);
    let photometric = g(262).unwrap_or(if samples == 3 { 2 } else { 1 });
    let strips = segments(ifd, 273, 279).or_else(|| segments(ifd, 324, 325)).unwrap_or_default();
    rules.check(
        "ifd_image",
        &mut rules::env([
            ("ifd", num(k as f64)),
            ("what", rules::text(what)),
            ("width", num(width as f64)),
            ("height", num(height as f64)),
            ("compression", num(compression as f64)),
            ("samples", num(samples as f64)),
            ("bits", num(bits as f64)),
            ("planar", num(planar as f64)),
            ("strips", num(strips.len() as f64)),
            ("format", num(format as f64)),
        ]),
    )?;
    let size = d["size"].as_u64().unwrap_or(0);
    for &(o, n) in &strips {
        if n == 0 || o.checked_add(n).is_none_or(|e| e > size) {
            return Err(Error::rejected(format!("IFD {k} ({what}): a strip ({n} bytes at {o}) is outside the {size}-byte .vsi file")));
        }
    }
    // the JPEG prefix: the Adobe marker for 3 samples, then the body of JPEGTables
    let prefix = if compression == 7 && ints(ifd, 259).is_some() {
        let tables = ifd["tags"]["347"]["value"].as_str().and_then(unbase64);
        crate::tiff::jpeg_frame(samples, photometric == 2, tables.as_deref()).0
    } else {
        Vec::new()
    };
    // the IFD's elements, where it lies in a TIFF region: its strips' and then its tiles' (§2.3
    // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#23-the-tiff-images>)
    let held = |name: &str| -> Option<Vec<u64>> { ifd[name].as_array().map(|a| a.iter().filter_map(J::as_u64).collect()) };
    let (from_strips, from_tiles) = (held("strips"), held("tiles").filter(|t| !t.is_empty()));
    if ifd["element"].is_null() || (from_strips.is_none() && from_tiles.is_none()) {
        return Err(Error::rejected(format!("IFD {k} ({what}) is not inside the metadata tree")));
    }
    let els: Vec<u64> = from_strips.into_iter().chain(from_tiles).flatten().collect();
    if els.len() != strips.len() {
        return Err(Error::rejected(format!("IFD {k} ({what}) has both strips and tiles")));
    }
    if compression != 7 && payload_size(&tiff::runs("", strips.iter().copied())) > MAX_PAYLOAD {
        return Err(Error::rejected(format!("IFD {k} ({what}): its strips do not fit in a reference of {MAX_PAYLOAD} bytes")));
    }
    Ok(json!({
        "index": k, "width": width, "height": height, "samples": samples, "bits": bits,
        "compression": compression, "photometric": photometric,
        "strips": strips.iter().zip(&els).map(|(&(o, n), &e)| json!([e, o, n])).collect::<Vec<_>>(),
        "prefix": prefix,
    }))
}

/// The size of coordinate `c` (2 or more) of a tile file: the ETS header's, or 1 past the dimensions
/// it lists.
fn coord_size(sizes: &[i64], c: usize) -> i64 {
    sizes.get(c).map_or(1, |&v| v.max(0))
}

/// The fill value of a tile file's arrays (§4.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#41-the-pyramid>): the background
/// color's first sample, as the pixel type reads it.
fn fill_value(data_type: &str, b: &[u8]) -> J {
    match data_type {
        "int8" => json!(b[0] as i8),
        "uint8" => json!(b[0]),
        "int16" => json!(i16::le_at(b, 0)),
        "uint16" => json!(u16::le_at(b, 0)),
        "int32" => json!(i32::le_at(b, 0)),
        "uint32" => json!(u32::le_at(b, 0)),
        "int64" => json!(i64::le_at(b, 0)),
        "uint64" => json!(u64::le_at(b, 0)),
        "float32" => {
            let v = f32::le_at(b, 0);
            if v.is_finite() { json!(v) } else { json!(0) }
        }
        _ => {
            let v = f64::le_at(b, 0);
            if v.is_finite() { json!(v) } else { json!(0) }
        }
    }
}

/// A stack's pyramid from its tile file (§4.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#41-the-pyramid>): the tile file's
/// key and header as the image's source metadata records it, its data type, sample bytes, samples and
/// codec, whether its samples are stored blue first, the tile size, the fill value, the level sizes,
/// the `t`, `c` and `z` sizes, and every tile inside its level as `[element, x, y, level, t, c, z]`,
/// with the count of those outside.
fn pyramid(rules: &Rules, s: &Stack, f: &J) -> crate::error::Res<J> {
    let (sis, e) = (&f["sis"], &f["ets"]);
    let i = |v: &J| v.as_i64().unwrap_or(0);
    let nc = i(&sis["coordinates"]).max(0) as usize;
    let level_coord = (e["use_pyramid"] == true).then(|| nc.saturating_sub(1));
    let sizes: Vec<i64> = e["dimension_sizes"].as_array().map(|a| a.iter().map(i).collect()).unwrap_or_default();
    let coord_of = |m: &str| s.dims.iter().find(|d| d.1 == m && Some(d.0) != level_coord && d.0 < nc).map(|d| d.0);
    let (tc, cc, zc) = (coord_of("t"), coord_of("c"), coord_of("z"));
    let size_of = |c: Option<usize>| c.map_or(1, |c| coord_size(&sizes, c).max(1));
    // each tile: (element, coordinates)
    let tiles: Vec<(u64, Vec<i64>)> = f["tiles"]
        .as_array()
        .map(|a| a.iter().map(|t| {
            let v = t.as_array().map(Vec::as_slice).unwrap_or_default();
            (v.first().and_then(J::as_u64).unwrap_or(0), v.iter().skip(3).map(i).collect())
        }).collect())
        .unwrap_or_default();
    let at = |t: &[i64], c: usize| t.get(c).copied().unwrap_or(0);
    let levels = if let Some(lc) = level_coord { tiles.iter().map(|t| at(&t.1, lc)).max().unwrap_or(0) + 1 } else { 1 } as usize;
    let (tw, th) = (i(&e["tile_width"]) as u64, i(&e["tile_height"]) as u64);
    // the level sizes, as Bio-Formats computes them from the stack's size and the tiles
    let (w0, h0) = (s.width.unwrap_or(0), s.height.unwrap_or(0));
    let mut max_x = vec![0i64; levels];
    let mut max_y = vec![0i64; levels];
    for (_, t) in &tiles {
        let l = level_coord.map_or(0, |lc| at(t, lc) as usize);
        max_x[l] = max_x[l].max(at(t, 0));
        max_y[l] = max_y[l].max(at(t, 1));
    }
    let mut lv: Vec<(u64, u64)> = vec![(w0, h0)];
    for l in 1..levels {
        let (pw, ph) = lv[l - 1];
        let (mx, my) = (tw * (max_x[l] as u64 + 1), th * (max_y[l] as u64 + 1));
        let mut w = pw / 2;
        if pw % 2 == 1 && w < mx {
            w += 1;
        } else if w > mx {
            w = mx;
        }
        let mut h = ph / 2;
        if ph % 2 == 1 && h < my {
            h += 1;
        } else if h > my {
            h = my;
        }
        lv.push((w, h));
    }
    let mut inside = Vec::with_capacity(tiles.len());
    let mut outside = 0u64;
    for (el, t) in &tiles {
        let l = level_coord.map_or(0, |lc| at(t, lc) as usize);
        let (w, h) = lv[l];
        let (x, y) = (at(t, 0) as u64, at(t, 1) as u64);
        if x >= w.div_ceil(tw) || y >= h.div_ceil(th) {
            outside += 1;
            continue;
        }
        let of = |c: Option<usize>| c.map_or(0, |c| at(t, c) as u64);
        inside.push(json!([el, x, y, l, of(tc), of(cc), of(zc)]));
    }
    let (pixel_type, compression) = (i(&e["pixel_type"]), i(&e["compression"]));
    let p = &rules.table("pixel_types")[pixel_type.to_string()];
    let data_type = p["dtype"].as_str().unwrap_or("uint8").to_string();
    let bytes = p["bytes"].as_u64().unwrap_or(1);
    let codec = rules.table("compressions")[compression.to_string()].as_str().unwrap_or("raw").to_string();
    let mut background = e["background"].as_str().and_then(unbase64).unwrap_or_default();
    background.resize(40, 0);
    // the tile file's header, as the image's source metadata records it (§6.1
    // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#61-the-root>)
    let header = json!({
        "key": f["key"], "size": f["size"], "version": sis["version"], "coordinates": sis["coordinates"], "tiles": sis["tiles"],
        "ets_version": e["version"], "pixel_type": pixel_type, "data_type": data_type, "samples": e["channels"],
        "colorspace": e["colorspace"], "compression": compression, "codec": codec, "quality": e["quality"],
        "tile_width": e["tile_width"], "tile_height": e["tile_height"], "tile_depth": e["tile_depth"],
        "background": e["background"], "component_order": e["component_order"],
        "use_pyramid": e["use_pyramid"], "dimensions": e["dimensions"], "dimension_sizes": e["dimension_sizes"],
    });
    Ok(json!({
        "key": f["key"], "header": header,
        "data_type": data_type, "bytes": bytes, "samples": e["channels"], "codec": codec,
        "bgr": i(&e["component_order"]) == 1 && compression == super::ets::RAW as i64,
        "tile": [tw, th], "fill": fill_value(&data_type, &background), "levels": lv.iter().map(|&(w, h)| json!([w, h])).collect::<Vec<_>>(),
        "sizes": {"t": size_of(tc), "c": size_of(cc), "z": size_of(zc)},
        "tiles": inside, "tiles_outside": outside,
    }))
}
