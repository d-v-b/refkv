//! The GRIB projection (spec/zarrify/grib.md §3
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#3-the-hierarchy>–§4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#4-georeferencing>, and the
//! chunks of the profile's §8.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#85-chunks>)
//! from a valid store IR and its facts: a group per grid, holding its coordinates and an array per
//! variable, whose chunks are the file's messages, one field each.

use super::vsi::object_url;
use super::{arr, n, with_counts};
use crate::grib::names;
use crate::ir::Ir;
use crate::out::{array_json, convention, gzip_array, group_doc, root_property, Out, Part, CONVENTION_KEY};
use crate::safe::product::{proj_cmo, spatial_cmo};
use serde_json::{json, Map, Value as J};
use std::collections::{BTreeMap, BTreeSet};

const PROFILE: &str = "grib";
/// The most values of a coordinate's chunk (conventions §9
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#9-dataset-layouts>).
const COORD_CHUNK: u64 = 1 << 17;
const DEGREE: &str = "ANGLEUNIT[\"degree\",0.0174532925199433]";

/// A finite f64 of the facts.
fn f(v: &J) -> f64 {
    v.as_f64().unwrap_or(f64::NAN)
}

/// A level value as an ordered key (its bits, NaN-free values compare as numbers).
fn level_key(v: f64) -> i128 {
    let b = v.to_bits() as i64;
    (if b < 0 { b ^ i64::MAX } else { b }) as i128
}

/// A duration's text: hours, minutes or seconds, whichever divides it.
fn duration(s: i64) -> String {
    if s % 3600 == 0 {
        format!("{}h", s / 3600)
    } else if s % 60 == 0 {
        format!("{}min", s / 60)
    } else {
        format!("{s}s")
    }
}

/// The name of a field's fixed surfaces (its level dimension's, when it has values), and its
/// units (spec/zarrify/grib.md §3.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#32-names>).
fn level_name(centre: u64, first: u64, second: u64) -> (String, Option<&'static str>) {
    let one = |k: u64| names::surface(centre, k as u8).map_or_else(|| (format!("level{k}"), None), |(n, u)| (n.to_string(), u));
    let (a, u) = one(first);
    if second == 255 {
        return (a, u);
    }
    if second == first {
        return (format!("{a}Layer"), u);
    }
    let (b, _) = one(second);
    let mut c = b.chars();
    let b = c.next().map(|x| x.to_ascii_uppercase().to_string() + c.as_str()).unwrap_or_default();
    (format!("{a}To{b}"), u)
}

/// A variable: the fields of one identity (spec/zarrify/grib.md §3.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#31-variables>).
struct Variable<'a> {
    fields: Vec<&'a J>,
    name: String,
    level: Option<String>,
    member: bool,
}

/// The CRS's WKT2 (spec/zarrify/grib.md §4
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#4-georeferencing>), from a
/// grid's facts: its Earth and projection.
fn wkt2(g: &J) -> Option<String> {
    let e = &g["earth"];
    let ellipsoid = if let Some(r) = e["radius"].as_f64() {
        format!("ELLIPSOID[\"sphere\",{r},0,LENGTHUNIT[\"metre\",1]]")
    } else {
        format!("ELLIPSOID[\"ellipsoid\",{},{},LENGTHUNIT[\"metre\",1]]", e["a"].as_f64()?, e["inverse_flattening"].as_f64()?)
    };
    let geog = format!("DATUM[\"GRIB\",{ellipsoid}],PRIMEM[\"Greenwich\",0,{DEGREE}]");
    let p = &g["projection"];
    Some(match p["kind"].as_str()? {
        "latlon" => format!(
            "GEOGCRS[\"GRIB\",{geog},CS[ellipsoidal,2],AXIS[\"latitude\",north,ORDER[1],{DEGREE}],AXIS[\"longitude\",east,ORDER[2],{DEGREE}]]"
        ),
        _ => {
            let par = |name: &str, v: f64, id: u32| format!("PARAMETER[\"{name}\",{v},{DEGREE},ID[\"EPSG\",{id}]]");
            format!(
                "PROJCRS[\"GRIB\",BASEGEOGCRS[\"GRIB\",{geog}],CONVERSION[\"GRIB\",METHOD[\"Lambert Conic Conformal (2SP)\",ID[\"EPSG\",9802]],{},{},{},{},\
                 PARAMETER[\"Easting at false origin\",0,LENGTHUNIT[\"metre\",1],ID[\"EPSG\",8826]],PARAMETER[\"Northing at false origin\",0,LENGTHUNIT[\"metre\",1],ID[\"EPSG\",8827]]],\
                 CS[Cartesian,2],AXIS[\"easting\",east,ORDER[1],LENGTHUNIT[\"metre\",1]],AXIS[\"northing\",north,ORDER[2],LENGTHUNIT[\"metre\",1]]]",
                par("Latitude of false origin", f(&p["latin1"]), 8821),
                par("Longitude of false origin", lon180(f(&p["lov"])), 8822),
                par("Latitude of 1st standard parallel", f(&p["latin1"]), 8823),
                par("Latitude of 2nd standard parallel", f(&p["latin2"]), 8824),
            )
        }
    })
}

/// A longitude in [−180, 180).
fn lon180(l: f64) -> f64 {
    (l + 180.0).rem_euclid(360.0) - 180.0
}

/// The Lambert conformal conic projection's forward map (Snyder, *Map Projections: A Working
/// Manual*, 1987, eqs. 15-1 to 15-10, 14-15): the easting and northing in meters of a latitude
/// and longitude in degrees, the origin at (`latin1`, `lov`), computed in the order written with
/// musl's libm functions (spec/zarrify/grib.md §4
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#4-georeferencing>), so that every
/// host gets the same bits.
pub fn lambert(lat: f64, lon: f64, latin1: f64, latin2: f64, lov: f64, earth: &J) -> (f64, f64) {
    use libm::{cos, log, pow, sin, sqrt, tan};
    let r = |d: f64| d * (std::f64::consts::PI / 180.0);
    let (p, p1, p2) = (r(lat), r(latin1), r(latin2));
    let (a, e) = match earth["radius"].as_f64() {
        Some(radius) => (radius, 0.0),
        None => {
            let a = f(&earth["a"]);
            let fl = 1.0 / f(&earth["inverse_flattening"]);
            (a, sqrt(fl * (2.0 - fl)))
        }
    };
    let m = |x: f64| cos(x) / sqrt(1.0 - e * e * sin(x) * sin(x));
    let t = |x: f64| tan(std::f64::consts::FRAC_PI_4 - x / 2.0) / pow((1.0 - e * sin(x)) / (1.0 + e * sin(x)), e / 2.0);
    let n = if p1 == p2 { sin(p1) } else { (log(m(p1)) - log(m(p2))) / (log(t(p1)) - log(t(p2))) };
    let ff = m(p1) / (n * pow(t(p1), n));
    let rho = a * ff * pow(t(p), n);
    let rho0 = a * ff * pow(t(p1), n);
    let theta = n * r(lon180(lon - lov));
    (rho * sin(theta), rho0 - rho * cos(theta))
}

/// A grid's dimensions, transform, and coordinates: (name, first value, step, units, standard
/// name) for y, then x.
#[allow(clippy::type_complexity)]
fn axes(g: &J) -> ([(&'static str, f64, f64, &'static str, &'static str); 2], J) {
    let p = &g["projection"];
    if p["kind"] == "latlon" {
        let (lat0, lon0, dlat, dlon) = (f(&p["lat0"]), f(&p["lon0"]), f(&p["dlat"]), f(&p["dlon"]));
        (
            [("latitude", lat0, dlat, "degrees_north", "latitude"), ("longitude", lon0, dlon, "degrees_east", "longitude")],
            json!([dlon, 0.0, lon0, 0.0, dlat, lat0]),
        )
    } else {
        let (x0, y0) = lambert(f(&p["lat0"]), f(&p["lon0"]), f(&p["latin1"]), f(&p["latin2"]), f(&p["lov"]), &g["earth"]);
        let (dx, dy) = (f(&p["dx"]), f(&p["dy"]));
        (
            [("y", y0, dy, "m", "projection_y_coordinate"), ("x", x0, dx, "m", "projection_x_coordinate")],
            json!([dx, 0.0, x0, 0.0, dy, y0]),
        )
    }
}

/// An array of float64 values the archive holds whole, little-endian and deflated.
fn values_array(out: &mut Out, path: &str, dims: &[&str], shape: &[u64], values: &[f64], attributes: J) {
    let codecs = json!([{"name": "bytes", "configuration": {"endian": "little"}}, {"name": "gzip", "configuration": {"level": crate::out::GZIP_LEVEL}}]);
    let mut doc = array_json(shape, "float64", json!(shape), codecs, dims);
    doc["fill_value"] = json!("NaN");
    doc["attributes"] = attributes;
    out.json(&format!("{path}/zarr.json"), &doc);
    let raw: Vec<u8> = values.iter().flat_map(|v| v.to_le_bytes()).collect();
    out.bytes(&format!("{path}/c/{}", vec!["0"; shape.len()].join("/")), crate::out::gzip(&raw));
}

/// A field's chunk (spec/zarrify/grib.md §8.5
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#85-chunks>): its message, when
/// the message holds it alone; else a message of its own, made of its sections between a new
/// section 0 and a `7777`.
fn chunk_parts(field: &J, messages: &[J], key: &str) -> Vec<Part> {
    let m = &messages[n(&field["message"]) as usize];
    let (start, length) = (n(&m[0]), n(&m[1]));
    if field["single"].as_bool().unwrap_or(true) {
        return vec![Part::Obj(key.to_string(), start, length)];
    }
    let parts: Vec<(u64, u64)> = arr(&field["parts"]).iter().map(|p| (n(&p[0]), n(&p[1]))).collect();
    let total = 16 + parts.iter().map(|p| p.1).sum::<u64>() + 4;
    let mut s0 = b"GRIB\0\0".to_vec();
    s0.push(n(&field["discipline"]) as u8);
    s0.push(2);
    s0.extend_from_slice(&total.to_be_bytes());
    let mut out = vec![Part::Lit(s0)];
    out.extend(parts.into_iter().map(|(o, len)| Part::Obj(key.to_string(), o, len)));
    out.push(Part::Lit(b"7777".to_vec()));
    out
}

pub fn project(ir: &Ir, facts: &J, url: &str) -> Result<Out, String> {
    let file_key = facts["file_key"].as_str().unwrap_or("").to_string();
    let file_url = object_url(url, &file_key);
    let messages = arr(&facts["messages"]);
    let fields: Vec<&J> = arr(&facts["fields"]).iter().filter(|x| x.get("reason").is_none()).collect();
    let mut out = Out::new(url);
    let mut prop = root_property(PROFILE, &file_url);
    prop.insert(PROFILE.into(), facts["own"].clone());
    let mut root = Map::new();
    root.insert("zarr_conventions".into(), json!([convention(PROFILE)]));
    root.insert(CONVENTION_KEY.into(), J::Object(prop));
    out.json("zarr.json", &group_doc(root));

    // the grids, in the order of their first fields
    let mut grids: Vec<&str> = Vec::new();
    for x in &fields {
        let g = x["grid"].as_str().unwrap_or("");
        if !grids.contains(&g) {
            grids.push(g);
        }
    }
    let mut summary = Vec::new();
    let mut refs: Vec<(String, Vec<Part>)> = Vec::new();
    for (gi, gkey) in grids.iter().enumerate() {
        let group = format!("grid{gi}");
        let in_grid: Vec<&J> = fields.iter().copied().filter(|x| x["grid"].as_str() == Some(gkey)).collect();
        let g = &in_grid[0]["grid_info"];
        let (ny, nx) = (n(&g["ny"]), n(&g["nx"]));
        let (yx, transform) = axes(g);

        // the variables, by identity, in the order of their first fields
        let mut order: Vec<String> = Vec::new();
        let mut by: BTreeMap<String, Vec<&J>> = BTreeMap::new();
        for x in &in_grid {
            let has = |k: usize| !x["surfaces"][k][1].is_null();
            let id = format!("{}:{}{}", x["identity"].as_str().unwrap_or(""), has(0) as u8, has(1) as u8);
            if !by.contains_key(&id) {
                order.push(id.clone());
            }
            by.entry(id).or_default().push(x);
        }
        let mut taken: BTreeSet<String> = ["time", "step", "valid_time", "number", yx[0].0, yx[1].0].into_iter().map(String::from).collect();
        // a field whose cell an earlier one of its identity holds goes to the next copy, an array
        // of its own (spec/zarrify/grib.md §3.1
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#31-variables>)
        let mut copies: Vec<Vec<&J>> = Vec::new();
        for id in &order {
            let mut cells: BTreeMap<String, usize> = BTreeMap::new();
            let first = copies.len();
            for x in by.remove(id).unwrap_or_default() {
                let cell = format!("{}|{}|{}|{}|{}", x["reference"], x["step"], x["surfaces"][0][1], x["surfaces"][1][1], x["member"]);
                let c = cells.entry(cell).or_insert(0);
                if first + *c >= copies.len() {
                    copies.push(Vec::new());
                }
                copies[first + *c].push(x);
                *c += 1;
            }
        }
        let mut variables: Vec<Variable> = Vec::new();
        for fs in copies {
            let x = fs[0];
            let centre = n(&x["centre"]);
            let (d, c, num) = (n(&x["discipline"]) as u8, n(&x["category"]) as u8, n(&x["number"]) as u8);
            let short = names::parameter(centre, d, c, num).map_or_else(|| format!("d{d}_c{c}_p{num}"), |p| p.0.to_string());
            let (s1, s2) = (n(&x["surfaces"][0][0]), n(&x["surfaces"][1][0]));
            let (lname, _) = level_name(centre, s1, s2);
            let mut base = if s1 == 255 { short } else { format!("{short}_{lname}") };
            if let Some(q) = x["qualifier"].as_str() {
                base.push('_');
                base.push_str(q);
            }
            if let Some(s) = x["statistic"].as_array() {
                base.push('_');
                base.push_str(&names::statistic(n(&s[0]) as u8));
                if let Some(l) = s[1].as_i64() {
                    base.push_str(&duration(l));
                }
            }
            let mut name = base.clone();
            let mut k = 2;
            while taken.contains(&name) {
                name = format!("{base}_{k}");
                k += 1;
            }
            taken.insert(name.clone());
            let level = (!x["surfaces"][0][1].is_null()).then_some(lname);
            let member = !x["member"].is_null();
            variables.push(Variable { fields: fs, name, level, member });
        }

        // the group's dimension coordinates: the union of its fields' values
        let times: Vec<i64> = in_grid.iter().filter_map(|x| x["reference"].as_i64()).collect::<BTreeSet<_>>().into_iter().collect();
        let steps: Vec<i64> = in_grid.iter().filter_map(|x| x["step"].as_i64()).collect::<BTreeSet<_>>().into_iter().collect();
        let members: Vec<u64> = in_grid.iter().filter_map(|x| x["member"].as_u64()).collect::<BTreeSet<_>>().into_iter().collect();
        let mut levels: BTreeMap<String, BTreeSet<(i128, i128)>> = BTreeMap::new();
        let mut level_values: BTreeMap<(i128, i128), (f64, f64)> = BTreeMap::new();
        let mut level_units: BTreeMap<String, Option<&str>> = BTreeMap::new();
        let mut layered: BTreeSet<String> = BTreeSet::new();
        for v in &variables {
            let Some(l) = &v.level else { continue };
            for x in &v.fields {
                let (a, b) = (f(&x["surfaces"][0][1]), x["surfaces"][1][1].as_f64().unwrap_or(f64::NAN));
                let k = (level_key(a), level_key(b));
                level_values.insert(k, (a, b));
                levels.entry(l.clone()).or_default().insert(k);
                if !b.is_nan() {
                    layered.insert(l.clone());
                }
            }
            let x = v.fields[0];
            level_units.insert(l.clone(), level_name(n(&x["centre"]), n(&x["surfaces"][0][0]), n(&x["surfaces"][1][0])).1);
        }
        // a level dimension sorts by the first surface's value, then the second's
        let levels: BTreeMap<String, Vec<(i128, i128)>> = levels.into_iter().map(|(k, s)| {
            let mut v: Vec<(i128, i128)> = s.into_iter().collect();
            v.sort_by(|p, q| level_values[p].0.total_cmp(&level_values[q].0).then(level_values[p].1.total_cmp(&level_values[q].1)));
            (k, v)
        }).collect();

        // the group: its georeferencing (§4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#4-georeferencing>)
        let mut a = Map::new();
        a.insert("zarr_conventions".into(), json!([proj_cmo(), spatial_cmo()]));
        if let Some(w) = wkt2(g) {
            a.insert("proj:wkt2".into(), json!(w));
        }
        a.insert("spatial:dimensions".into(), json!([yx[0].0, yx[1].0]));
        a.insert("spatial:transform".into(), transform);
        a.insert("spatial:shape".into(), json!([ny, nx]));
        a.insert("spatial:registration".into(), json!("node"));
        out.json(&format!("{group}/zarr.json"), &group_doc(a));

        // the coordinates (§3.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#33-coordinates>)
        let tv: Vec<f64> = times.iter().map(|&t| t as f64).collect();
        values_array(&mut out, &format!("{group}/time"), &["time"], &[tv.len() as u64], &tv,
                     json!({"units": "seconds since 1970-01-01T00:00:00Z", "calendar": "proleptic_gregorian", "standard_name": "forecast_reference_time", "long_name": "initial time of forecast"}));
        let sv: Vec<f64> = steps.iter().map(|&s| s as f64).collect();
        values_array(&mut out, &format!("{group}/step"), &["step"], &[sv.len() as u64], &sv,
                     json!({"units": "seconds", "standard_name": "forecast_period", "long_name": "time since forecast_reference_time"}));
        let valid: Vec<f64> = times.iter().flat_map(|&t| steps.iter().map(move |&s| (t + s) as f64)).collect();
        values_array(&mut out, &format!("{group}/valid_time"), &["time", "step"], &[tv.len() as u64, sv.len() as u64], &valid,
                     json!({"units": "seconds since 1970-01-01T00:00:00Z", "calendar": "proleptic_gregorian", "standard_name": "time", "long_name": "time"}));
        if !members.is_empty() {
            let mv: Vec<f64> = members.iter().map(|&m| m as f64).collect();
            values_array(&mut out, &format!("{group}/number"), &["number"], &[mv.len() as u64], &mv,
                         json!({"units": "1", "standard_name": "realization", "long_name": "ensemble member numerical id"}));
        }
        for (l, ks) in &levels {
            let firsts: Vec<f64> = ks.iter().map(|k| level_values[k].0).collect();
            let mut attrs = Map::new();
            if let Some(u) = level_units.get(l).copied().flatten() {
                attrs.insert("units".into(), json!(u));
            }
            attrs.insert("long_name".into(), json!(l));
            if layered.contains(l) {
                attrs.insert("bounds".into(), json!(format!("{l}_bounds")));
                let b: Vec<f64> = ks.iter().flat_map(|k| [level_values[k].0, level_values[k].1]).collect();
                values_array(&mut out, &format!("{group}/{l}_bounds"), &[l, "bounds"], &[ks.len() as u64, 2], &b, json!({}));
            }
            values_array(&mut out, &format!("{group}/{l}"), &[l], &[firsts.len() as u64], &firsts, J::Object(attrs));
        }
        for (k, (dim, first, step, units, standard)) in yx.iter().enumerate() {
            let len = if k == 0 { ny } else { nx };
            let filters = vec![json!({"name": "numcodecs.delta", "configuration": {"dtype": "<u4"}})];
            let attributes = json!({"units": units, "standard_name": standard, "scale_factor": step, "add_offset": first});
            gzip_array(&mut out, &format!("{group}/{dim}"), "uint32", len, COORD_CHUNK, dim, filters, attributes, |q, c| {
                let mut raw = Vec::with_capacity((c * 4) as usize);
                raw.extend_from_slice(&((q * c) as u32).to_le_bytes());
                for _ in 1..c {
                    raw.extend_from_slice(&1u32.to_le_bytes());
                }
                raw
            });
        }

        // the variables' arrays and chunks (§3.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#31-variables>)
        for v in &variables {
            let x = v.fields[0];
            let mut dims: Vec<String> = vec!["time".into(), "step".into()];
            let mut shape = vec![times.len() as u64, steps.len() as u64];
            if let Some(l) = &v.level {
                dims.push(l.clone());
                shape.push(levels[l].len() as u64);
            }
            if v.member {
                dims.push("number".into());
                shape.push(members.len() as u64);
            }
            dims.extend([yx[0].0.to_string(), yx[1].0.to_string()]);
            shape.extend([ny, nx]);
            let chunk_shape: Vec<u64> = shape.iter().enumerate().map(|(k, &s)| if k + 2 >= shape.len() { s } else { 1 }).collect();
            let dim_refs: Vec<&str> = dims.iter().map(String::as_str).collect();
            let mut doc = array_json(&shape, "float64", json!(chunk_shape), json!([{"name": "refkv.grib"}]), &dim_refs);
            doc["fill_value"] = json!("NaN");
            let centre = n(&x["centre"]);
            let (d, c, num) = (n(&x["discipline"]) as u8, n(&x["category"]) as u8, n(&x["number"]) as u8);
            let mut attrs = Map::new();
            if let Some((_, long, units)) = names::parameter(centre, d, c, num) {
                attrs.insert("long_name".into(), json!(long));
                attrs.insert("units".into(), json!(units));
            }
            attrs.insert("coordinates".into(), json!("valid_time"));
            attrs.insert("GRIB_centre".into(), json!(centre));
            attrs.insert("GRIB_discipline".into(), json!(d));
            attrs.insert("GRIB_parameterCategory".into(), json!(c));
            attrs.insert("GRIB_parameterNumber".into(), json!(num));
            attrs.insert("GRIB_productDefinitionTemplateNumber".into(), x["product"].clone());
            attrs.insert("GRIB_typeOfFirstFixedSurface".into(), x["surfaces"][0][0].clone());
            attrs.insert("GRIB_typeOfSecondFixedSurface".into(), x["surfaces"][1][0].clone());
            if let Some(s) = x["statistic"].as_array() {
                attrs.insert("GRIB_typeOfStatisticalProcessing".into(), s[0].clone());
                attrs.insert("GRIB_lengthOfTimeRange".into(), s[1].clone());
            }
            doc["attributes"] = J::Object(attrs);
            out.json(&format!("{group}/{}/zarr.json", v.name), &doc);
            for x in &v.fields {
                let t = times.binary_search(&x["reference"].as_i64().unwrap_or(0)).unwrap_or(0);
                let s = steps.binary_search(&x["step"].as_i64().unwrap_or(0)).unwrap_or(0);
                let mut coords = vec![t, s];
                if let Some(l) = &v.level {
                    let k = (level_key(f(&x["surfaces"][0][1])), level_key(x["surfaces"][1][1].as_f64().unwrap_or(f64::NAN)));
                    coords.push(levels[l].iter().position(|q| *q == k).unwrap_or(0));
                }
                if v.member {
                    coords.push(members.binary_search(&x["member"].as_u64().unwrap_or(0)).unwrap_or(0));
                }
                coords.extend([0, 0]);
                let key = format!("{group}/{}/c/{}", v.name, coords.iter().map(|c| c.to_string()).collect::<Vec<_>>().join("/"));
                refs.push((key, chunk_parts(x, messages, &file_key)));
            }
            summary.push(json!({"path": format!("{group}/{}", v.name), "shape": shape, "fields": v.fields.len()}));
        }
    }
    let count = refs.len();
    let _ = ir;
    for (k, p) in refs {
        if crate::out::payload_size(&p) > crate::out::MAX_PAYLOAD {
            return Err(format!("{k}: a reference payload of more than {} bytes", crate::out::MAX_PAYLOAD));
        }
        out.refs(&k, p);
    }
    for k in out.entries.iter().map(|e| &e.0).chain(out.refs.iter().map(|r| &r.0)) {
        super::store::check_key(k)?;
    }
    let mut unpresented: BTreeMap<String, u64> = BTreeMap::new();
    for x in arr(&facts["fields"]) {
        if let Some(r) = x["reason"].as_str() {
            *unpresented.entry(r.to_string()).or_default() += 1;
        }
    }
    out.summary = with_counts(json!({
        "messages": messages.len(), "fields": arr(&facts["fields"]).len(), "grids": grids.len(), "arrays": summary,
        "chunks": count, "unpresented": unpresented, "index": facts["index_key"],
    }), facts);
    Ok(out)
}
