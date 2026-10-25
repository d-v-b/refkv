//! The NRRD image projection (spec/zarrify/nrrd.md §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#4-the-image>, the root's source
//! metadata of §5.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#51-the-root>,
//! and the chunks of the profile's §8.5
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#85-chunks>) from a valid store
//! IR and its facts: one image at the root, whose array's chunks are ranges of the data's object.

use super::{arr, image_axes, key, n, translation, with_counts};
use super::vsi::object_url;
use crate::ir::Ir;
use crate::nrrd::CHUNK_BYTES;
use crate::out::{array_json, bytes_codec, full_window, grid_chunks, image_ome, omero_channel, omero_json, root_json, Chunk, Out, Part, RGBA};
use crate::refs::Refs;
use serde_json::{json, Map, Value as J};

/// The most channels given display windows, as NIfTI's (spec/zarrify/nrrd.md §4.4
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#44-channels>).
const MAX_CHANNELS: u64 = 64;

/// A length unit's symbol as conventions §5
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#5-units> maps it.
fn length_unit(s: &str) -> Option<&'static str> {
    Some(match s {
        "\u{b5}m" | "\u{3bc}m" | "um" => "micrometer",
        "nm" => "nanometer",
        "mm" => "millimeter",
        "cm" => "centimeter",
        "m" => "meter",
        "\u{c5}" | "\u{212b}" => "angstrom",
        "pm" => "picometer",
        "in" => "inch",
        "ft" => "foot",
        _ => return None,
    })
}

/// A time unit's symbol as conventions §5
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#5-units> maps it.
fn time_unit(s: &str) -> Option<&'static str> {
    Some(match s {
        "s" => "second",
        "ms" => "millisecond",
        "min" => "minute",
        "h" => "hour",
        _ => return None,
    })
}

/// A number that is finite and positive.
fn valid(v: Option<f64>) -> Option<f64> {
    v.filter(|x| x.is_finite() && *x > 0.0)
}

/// Item `i` of an array of the facts as a number (None: null or absent).
fn num_at(v: &J, i: usize) -> Option<f64> {
    v.get(i).and_then(J::as_f64)
}

/// Item `i` of an array of texts of the facts.
fn str_at(v: &J, i: usize) -> Option<&str> {
    v.get(i).and_then(J::as_str)
}

/// The scales, units and translation of the image's axes (spec/zarrify/nrrd.md §4.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#43-scales-units-and-position>):
/// (scale by axis, unit by axis, the translation's `x`, `y`, `z` when the orientation is applied,
/// and whether it is, when the header has space directions).
#[allow(clippy::type_complexity)]
fn placement(a: &J) -> (Map<String, J>, Map<String, J>, Option<[f64; 3]>, Option<bool>) {
    let axes: Vec<&str> = arr(&a["axes"]).iter().filter_map(J::as_str).collect();
    let (dirs, spacings, units, space_units) = (&a["directions"], &a["spacings"], &a["units"], &a["space_units"]);
    let spatial: Vec<(usize, usize)> = axes.iter().enumerate().filter_map(|(i, x)| ["x", "y", "z"].iter().position(|s| s == x).map(|j| (i, j))).collect();
    let vector = |i: usize| -> Option<Vec<Option<f64>>> {
        dirs.get(i).and_then(J::as_array).map(|v| v.iter().map(J::as_f64).collect())
    };
    // representable: every spatial axis's vector lies along its own space coordinate, positive
    let representable = !dirs.is_null() && spatial.iter().all(|&(i, j)| {
        vector(i).is_some_and(|v| j < v.len() && v.iter().enumerate().all(|(k, c)| if k == j { valid(*c).is_some() } else { *c == Some(0.0) }))
    });
    let same_unit = arr(space_units).iter().filter_map(J::as_str).collect::<Vec<_>>();
    let shared = same_unit.first().filter(|u| same_unit.iter().all(|x| x == *u)).copied();
    let (mut scale, mut unit) = (Map::new(), Map::new());
    for (i, axis) in axes.iter().enumerate() {
        let (s, u) = match *axis {
            "c" => (None, None),
            "t" => {
                let s = valid(num_at(spacings, i));
                (s, s.and(str_at(units, i)).and_then(time_unit))
            }
            _ => {
                let j = ["x", "y", "z"].iter().position(|s| s == axis).unwrap_or(0);
                match vector(i) {
                    Some(v) if representable => (valid(v[j]), str_at(space_units, j).and_then(length_unit)),
                    Some(v) => {
                        let norm = v.iter().try_fold(0.0, |acc, c| c.map(|c| acc + c * c)).map(f64::sqrt);
                        let s = valid(norm);
                        (s, s.and(shared).and_then(length_unit))
                    }
                    None => {
                        let s = valid(num_at(spacings, i));
                        (s, s.and(str_at(units, i)).and_then(length_unit))
                    }
                }
            }
        };
        scale.insert(axis.to_string(), s.map_or(json!(1), |v| json!(v)));
        if let Some(u) = u {
            unit.insert(axis.to_string(), json!(u));
        }
    }
    let origin: Option<Vec<f64>> = a["origin"].as_array().and_then(|o| o.iter().map(J::as_f64).collect());
    let translation = (representable).then_some(()).and(origin).map(|o| {
        let mut t = [0.0; 3];
        for &(_, j) in &spatial {
            t[j] = o[j];
        }
        t
    });
    (scale, unit, translation, (!dirs.is_null()).then_some(representable))
}

pub fn project(ir: &Ir, facts: &J, url: &str) -> Result<Out, String> {
    let a = &facts["array"];
    let nrrd_axes: Vec<&str> = arr(&a["axes"]).iter().filter_map(J::as_str).collect();
    let sizes: Vec<u64> = arr(&a["sizes"]).iter().map(n).collect();
    let (data_type, item, encoding) = (a["data_type"].as_str().unwrap_or("uint8"), n(&a["item"]), a["encoding"].as_str().unwrap_or("raw"));
    let endian = a["endian"].as_str().unwrap_or("little");
    let (start, length) = (n(&a["start"]), n(&a["length"]));
    let data_key = facts["data_key"].as_str().unwrap_or("").to_string();
    let size_of = |x: &str| nrrd_axes.iter().position(|y| *y == x).map(|i| sizes[i]);

    // the array (§4.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#41-the-array>):
    // stored with the header's last axis outermost
    let axes = image_axes(size_of("t").is_some(), size_of("c").is_some(), size_of("z").is_some());
    let stored: Vec<&str> = nrrd_axes.iter().rev().copied().collect();
    let stored_shape: Vec<u64> = sizes.iter().rev().copied().collect();
    let shape: Vec<u64> = axes.iter().map(|x| size_of(x).unwrap_or(1)).collect();
    let pos = |x: &str| stored.iter().position(|s| *s == x).unwrap_or(0);

    let (scale, units, origin, applied) = placement(a);
    let scales = J::Array(axes.iter().map(|x| scale[*x].clone()).collect());
    let tr = origin.map(|o| translation(&axes, o.map(|v| json!(v))));
    let mut ome = image_ome(&axes, &units, &[scales], None, tr.clone().map(|t| vec![t]).as_deref())?;

    // channels (§4.4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#44-channels>)
    let c = size_of("c").unwrap_or(1);
    let kind = nrrd_axes.iter().position(|x| *x == "c").and_then(|i| a["kinds"].get(i)).and_then(J::as_str).unwrap_or("");
    let colors = match kind {
        "rgb-color" if c == 3 => 3,
        "rgba-color" if c == 4 => 4,
        _ => 0,
    };
    if colors > 0 && data_type == "uint8" {
        let window = full_window(json!(0), json!(255));
        ome["omero"] = omero_json(RGBA[..colors].iter().map(|(l, k)| omero_channel(l, *k, Some(&window))).collect());
    } else if let (Some(lo), Some(hi)) = (a["min"].as_f64(), a["max"].as_f64())
        && hi > lo
        && c <= MAX_CHANNELS
    {
        let window = full_window(json!(lo), json!(hi));
        ome["omero"] = omero_json((0..c).map(|k| omero_channel(&format!("C{k}"), "FFFFFF", Some(&window))).collect());
    }

    let mut own = facts["own"].as_object().cloned().unwrap_or_default();
    if let Some(applied) = applied {
        own.insert("orientation".into(), json!({"applied": applied}));
    }
    let header_url = object_url(url, facts["header_key"].as_str().unwrap_or(""));
    let mut out = Out::new(url);
    out.json("zarr.json", &root_json(ome, "nrrd", &header_url, Some(J::Object(own))));

    let mut codecs = Vec::new();
    if stored != axes {
        let order: Vec<usize> = stored.iter().map(|s| axes.iter().position(|x| x == s).unwrap_or(0)).collect();
        codecs.push(json!({"name": "transpose", "configuration": {"order": order}}));
    }
    codecs.push(bytes_codec(item, endian));
    let gzip = encoding == "gzip";
    if gzip {
        codecs.push(json!({"name": "gzip", "configuration": {"level": 1}}));
    }
    let (grid, chunks) = if gzip {
        (stored_shape.clone(), vec![(vec![0; stored.len()], Chunk::Ranges(vec![Part::Src(start, length)]))])
    } else {
        let threshold = facts["copy_threshold"].as_u64().map_or(crate::out::MAX_PAYLOAD, |t| t as usize);
        grid_chunks(start, &stored_shape, item, CHUNK_BYTES, threshold).ok_or("internal: the data cannot be cut into chunks")?
    };
    let chunk_shape: Vec<u64> = axes.iter().map(|x| grid[pos(x)]).collect();
    out.json("0/zarr.json", &array_json(&shape, data_type, json!(chunk_shape), J::Array(codecs), &axes));

    // the chunks (§8.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#85-chunks>):
    // ranges of the data's object, an edge chunk's padding a literal, or copied with it
    let copied: Vec<(u64, Vec<u8>)> = arr(&facts["copied"]).iter()
        .map(|c| (n(&c[0]), arr(&c[1]).iter().map(|b| n(b) as u8).collect()))
        .collect();
    let count = chunks.len();
    let mut refs = Refs::new(ir, out);
    for (piece, (coords, chunk)) in chunks.into_iter().enumerate() {
        let k = key("0/c/", axes.iter().map(|x| coords[pos(x)]));
        match chunk {
            Chunk::Ranges(parts) => {
                let parts = parts.into_iter().map(|p| match p {
                    Part::Src(o, len) => Part::Obj(data_key.clone(), o, len),
                    other => other,
                }).collect();
                refs.chunk(k, start, length, 0, Some(parts), piece as u64)?;
            }
            Chunk::Copy(o, len, pad) => {
                let mut b = copied.iter().find(|c| c.0 == o && c.1.len() as u64 == len)
                    .ok_or("internal: a copied chunk's bytes are not in the facts")?.1.clone();
                b.resize(b.len() + pad as usize, 0);
                refs.out.bytes(&k, b);
            }
            Chunk::Bytes(b) => refs.out.bytes(&k, b),
        }
    }
    let mut out = refs.out;
    for k in out.entries.iter().map(|e| &e.0).chain(out.refs.iter().map(|r| &r.0)) {
        super::store::check_key(k)?;
    }
    let sizes_by: Map<String, J> = axes.iter().zip(&shape).map(|(x, s)| (x.to_string(), json!(s))).collect();
    out.summary = with_counts(json!({
        "sizes": sizes_by, "dataType": data_type, "encoding": encoding, "detached": facts["data_key"] != facts["header_key"],
        "dataFile": data_key, "chunkShape": chunk_shape, "chunks": count, "orientation": applied, "translation": tr.is_some(),
    }), facts);
    Ok(out)
}
