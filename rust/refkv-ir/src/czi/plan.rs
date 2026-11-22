//! refkv's plan of a CZI image (spec/zarrify/czi.md §3
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#3-placement>, §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#4-output>), made from the parser's
//! description alone (spec/zarrify/czi.md §7.10
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#710-the-description>): which
//! subblocks are placed, their series, layers and levels, the images and tile arrays with their axes,
//! sizes, chunks and row bands, the scales, units and position, and the root's source metadata. It
//! belongs to refkv's convention, not to the IR: the parser's IR does not depend on it. The image's
//! checks run here, in the profile's order and with its messages.

use crate::coding;
use crate::cxml;
use crate::ir::Res;
use crate::rules::{self, Rules, flag, num};
use serde_json::{Value as J, json};
use std::collections::{BTreeMap, HashMap};

const SERIES: &[u8] = b"SBHIRV";

/// The plan of the image the description `d` describes: of a file, or of an image across files
/// (its named file's description, with each part's in `files`). Its members are the projection's:
/// `root`, `subblocks`, `images`, `tiles` and `values`.
pub fn plan(d: &J) -> Res<J> {
    let files: Vec<&J> = std::iter::once(d).chain(d["files"].as_array().into_iter().flatten().map(|f| &f["description"])).collect();
    let mut ps = Vec::new();
    let mut index = 0;
    let mut subblocks = 0;
    for f in &files {
        for e in f["entries"].as_array().into_iter().flatten() {
            subblocks += usize::from(!e["subblock"].is_null());
            if let Some(p) = placed(index, e) {
                ps.push(p);
            }
            index += 1;
        }
    }
    let mut plan = image(ps, subblocks, &values(&d["metadata"]["values"]), &d["file_header"], rules::of("czi"))?;
    if let Some(files) = d["files"].as_array() {
        plan["root"]["parts"] = J::Array(files.iter().map(|f| f["key"].clone()).collect());
        plan["named_key"] = d["named_key"].clone();
    }
    Ok(plan)
}

/// The XML's values from the description's (none of an XML the profile does not read).
fn values(v: &J) -> cxml::Values {
    let f = |k: &str| v[k].as_f64();
    let channel = |c: &J| cxml::Channel {
        name: c["name"].as_str().map(str::to_string),
        color: c["color"].as_str().map(str::to_string),
        bits: c["bits"].as_u64(),
        low: c["low"].as_f64(),
        high: c["high"].as_f64(),
    };
    let list = |k: &str| v[k].as_array().into_iter().flatten().map(channel).collect();
    cxml::Values {
        px: f("px"),
        py: f("py"),
        pz: f("pz"),
        inc: f("inc"),
        bits: v["bits"].as_u64(),
        info: list("info"),
        display: list("display"),
        scenes: v["scenes"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|s| Some((s["index"].as_u64()?, s["name"].as_str().map(str::to_string))))
            .collect(),
        ..Default::default()
    }
}

/// Entry `index` of the image's when it is placed (spec/zarrify/czi.md §3.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#32-conforming-and-unplaced-subblocks>):
/// an entry that read its subblock, whose copy agrees, whose stream is what the entry says, with
/// its pixels' element.
fn placed(index: usize, e: &J) -> Option<Placed> {
    let s = &e["subblock"];
    let (stream, el) = (&s["stream"], s["data"].as_u64()?);
    if s["agrees"] != json!(true) || !stream.is_object() {
        return None;
    }
    let dims: Vec<(u8, i64, i64, i64)> = e["dimensions"]
        .as_array()?
        .iter()
        .map(|d| Some((*d["dimension"].as_str()?.as_bytes().first()?, d["start"].as_i64()?, d["size"].as_i64()?, d["stored_size"].as_i64()?)))
        .collect::<Option<_>>()?;
    let dim = |l: u8| dims.iter().find(|d| d.0 == l).map(|d| (d.1, d.2, d.3));
    let (x, y) = (dim(b'X')?, dim(b'Y')?);
    let st = |l: u8| dim(l).map_or(0, |d| d.0);
    let flag = stream["lossless"].as_bool().unwrap_or(false) || stream["hilo"].as_bool().unwrap_or(false);
    Some(Placed {
        index,
        series: SERIES.iter().map(|&l| dim(l).map_or((0, 0), |d| (1, d.0))).collect(),
        plane: (st(b'T'), st(b'C'), st(b'Z')),
        x: x.0,
        y: y.0,
        wl: x.1,
        hl: y.1,
        w: x.2,
        h: y.2,
        cw: stream["width"].as_u64()?,
        ch: stream["height"].as_u64()?,
        form: (e["pixel_type"].as_i64()? as i32, e["compression"].as_i64()? as i32, flag),
        layer: layer(x.1, y.1, x.2, y.2),
        el: el as u32,
        key: s["element"].as_u64()? as u32,
    })
}

struct Placed {
    index: usize,
    series: Vec<(u8, i64)>,
    plane: (i64, i64, i64),
    x: i64,
    y: i64,
    wl: i64,
    hl: i64,
    w: i64,
    h: i64,
    cw: u64,
    ch: u64,
    form: (i32, i32, bool),
    layer: Option<(u32, u32)>,
    el: u32,
    /// the subblock's struct, the key of its description
    key: u32,
}

impl Placed {
    fn conforming(&self) -> bool {
        self.cw as i64 == self.w && self.ch as i64 == self.h
    }
}

struct Level {
    layer: (u32, u32),
    form: (i32, i32, bool),
    factor: i64,
    tile: (i64, i64),
    edge: (i64, i64),
    origin: (i64, i64),
    grid: (i64, i64),
    cells: Vec<(usize, i64, i64)>, // (placed, column, row)
}


// ---- the layout (spec/zarrify/czi.md §3.2
// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#32-conforming-and-unplaced-subblocks>–§4.4
// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#44-tiles>): placement, series,
// layers, levels, tiles

const LAYERS_2: [(f64, f64, u32); 10] = [
    (2.0, 0.1, 1), (4.0, 0.2, 2), (8.0, 0.4, 3), (16.0, 0.8, 4), (32.0, 1.0, 5), (64.0, 1.0, 6), (128.0, 1.0, 7),
    (256.0, 2.0, 8), (512.0, 4.0, 9), (1024.0, 10.0, 10),
];
const LAYERS_3: [(f64, f64, u32); 7] =
    [(3.0, 0.1, 1), (9.0, 0.2, 2), (27.0, 0.8, 3), (81.0, 1.5, 4), (243.0, 2.0, 5), (729.0, 5.0, 6), (2187.0, 15.0, 7)];

/// A subblock's layer from its logical and stored sizes (libCZI's pyramid tables).
fn layer(wl: i64, hl: i64, w: i64, h: i64) -> Option<(u32, u32)> {
    if wl == w && hl == h {
        return Some((1, 0));
    }
    let f = if w > h { wl as f64 / w as f64 } else { hl as f64 / h as f64 };
    for (table, base) in [(&LAYERS_2[..], 2), (&LAYERS_3[..], 3)] {
        for &(v, delta, n) in table {
            if v - delta <= f && f <= v + delta {
                return Some((base, n));
            }
        }
    }
    None
}

/// The level `b` is when it is regular (spec/zarrify/czi.md §3.5
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#35-regular-levels>), else None.
fn classify(b: &[usize], ps: &[Placed]) -> Option<Level> {
    let f0 = ps[b[0]].form;
    if b.iter().any(|&i| ps[i].form != f0) {
        return None;
    }
    let wl = b.iter().map(|&i| ps[i].wl).max()?;
    let hl = b.iter().map(|&i| ps[i].hl).max()?;
    let w = b.iter().map(|&i| ps[i].w).max()?;
    let h = b.iter().map(|&i| ps[i].h).max()?;
    if wl % w != 0 || hl % h != 0 || wl / w != hl / h {
        return None;
    }
    let x0 = b.iter().map(|&i| ps[i].x).min()?;
    let y0 = b.iter().map(|&i| ps[i].y).min()?;
    let mut cells = Vec::new();
    let mut seen = std::collections::HashSet::new();
    for &i in b {
        let s = &ps[i];
        if (s.x - x0) % wl != 0 || (s.y - y0) % hl != 0 {
            return None;
        }
        let (c, r) = ((s.x - x0) / wl, (s.y - y0) / hl);
        if !seen.insert((s.plane, c, r)) {
            return None;
        }
        cells.push((i, c, r));
    }
    let m = cells.iter().map(|c| c.1).max()? + 1;
    let r = cells.iter().map(|c| c.2).max()? + 1;
    let (mut last_col, mut last_row) = (Vec::new(), Vec::new());
    for &(i, c, j) in &cells {
        let s = &ps[i];
        if c < m - 1 && (s.wl, s.w) != (wl, w) {
            return None;
        }
        if j < r - 1 && (s.hl, s.h) != (hl, h) {
            return None;
        }
        if c == m - 1 && !last_col.contains(&(s.wl, s.w)) {
            last_col.push((s.wl, s.w));
        }
        if j == r - 1 && !last_row.contains(&(s.hl, s.h)) {
            last_row.push((s.hl, s.h));
        }
    }
    if last_col.len() != 1 || last_row.len() != 1 {
        return None;
    }
    Some(Level {
        layer: ps[b[0]].layer.unwrap(),
        form: f0,
        factor: wl / w,
        tile: (w, h),
        edge: (last_col[0].1, last_row[0].1),
        origin: (x0, y0),
        grid: (m, r),
        cells,
    })
}

fn gcd(a: u128, b: u128) -> u128 {
    if b == 0 { a } else { gcd(b, a % b) }
}

/// The rows of a band of an uncompressed tile of w × h (edge height h2) of q-byte
/// pixels: h when the tile is at most max_band bytes, else the largest divisor of
/// gcd(h, h2) whose band is at most max_band bytes, or 1.
fn row_band(h: u128, h2: u128, w: u128, q: u128, max_band: u128) -> u128 {
    if w * h * q <= max_band {
        return h;
    }
    let g = gcd(h, h2);
    let mut best = 1;
    let mut d = 1;
    while d * d <= g {
        if g % d == 0 {
            for v in [d, g / d] {
                if v * w * q <= max_band && v > best {
                    best = v;
                }
            }
        }
        d += 1;
    }
    best
}

/// The y chunk lengths of `r` tile rows of height h (the last h2), and the bands:
/// (rows a band holds, bands per tile row). Divisor bands (spec/zarrify/czi.md §4.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#43-image-arrays>)
/// when they hold at least band_floor bytes; else bands of the most rows within
/// max_band bytes, the last of each tile row shorter (a rectilinear grid), so that
/// a prime height cannot make a chunk per row.
#[allow(clippy::too_many_arguments)]
pub fn bands(h: u64, h2: u64, r: u64, w: u64, q: u64, uncompressed: bool, max_band: u64, floor: u64) -> (J, u64, u64) {
    let (hb, wb, qb) = (h as u128, w as u128, q as u128);
    if !uncompressed || wb * hb * qb <= max_band as u128 {
        let y = if h2 == h {
            json!(h)
        } else if r > 1 {
            json!([[h, r - 1], h2])
        } else {
            json!([h2])
        };
        return (y, h, 1);
    }
    let b = row_band(hb, h2 as u128, wb, qb, max_band as u128) as u64;
    if b as u128 * wb * qb >= floor as u128 || b == h {
        return (json!(b), b, h / b);
    }
    let big = 1u64.max((max_band as u128 / (wb * qb)) as u64);
    let per = h.div_ceil(big);
    let mut runs: Vec<(u64, u64)> = Vec::new();
    let mut push = |v: u64, n: u64| {
        if n == 0 {
            return;
        }
        match runs.last_mut() {
            Some(l) if l.0 == v => l.1 += n,
            _ => runs.push((v, n)),
        }
    };
    for j in 0..r {
        let hh = if j < r - 1 { h } else { h2 };
        push(big, hh / big);
        if hh % big != 0 {
            push(hh % big, 1);
        }
    }
    let y: Vec<J> = runs.iter().map(|&(v, n)| if n == 1 { json!(v) } else { json!([v, n]) }).collect();
    (J::Array(y), big, per)
}

/// The axes of an array over its subblocks' planes, the least t, c, z, and the extent along each.
fn plane_axes(planes: &[(i64, i64, i64)], p: u64) -> (Vec<&'static str>, [i64; 3], [i64; 3]) {
    let lo = [0, 1, 2].map(|k| planes.iter().map(|pl| [pl.0, pl.1, pl.2][k]).min().unwrap());
    let hi = [0, 1, 2].map(|k| planes.iter().map(|pl| [pl.0, pl.1, pl.2][k]).max().unwrap());
    let mut axes = Vec::new();
    if hi[0] > lo[0] {
        axes.push("t");
    }
    if hi[1] > lo[1] || p > 1 {
        axes.push("c");
    }
    if hi[2] > lo[2] {
        axes.push("z");
    }
    axes.extend(["y", "x"]);
    (axes, lo, [0, 1, 2].map(|k| hi[k] - lo[k] + 1))
}


fn checked_extent(rules: &Rules, v: i128) -> Res<i128> {
    rules.check("extent", rules::env([("extent", num(v as f64))]))?;
    Ok(v)
}


fn dims_json(series: &[(u8, i64)]) -> serde_json::Map<String, J> {
    SERIES
        .iter()
        .zip(series)
        .filter(|(_, k)| k.0 == 1)
        .map(|(l, k)| ((*l as char).to_string(), json!(k.1)))
        .collect()
}


/// The image of the subblocks `ps` placed, of `subblocks` subblocks in all, with the named file's
/// header and XML values.
fn image(ps: Vec<Placed>, subblocks: usize, v: &cxml::Values, header: &J, rules: &Rules) -> Res<J> {
    let max_band = rules.limit("max_band") as u64;
    let floor = rules.limit("band_floor") as u64;
    // series, layers and levels (§3.3
    // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#33-series>–§3.5
    // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#35-regular-levels>)
    let mut by_series: BTreeMap<Vec<(u8, i64)>, Vec<usize>> = BTreeMap::new();
    for (k, p) in ps.iter().enumerate() {
        by_series.entry(p.series.clone()).or_default().push(k);
    }
    let mut images: Vec<(Vec<(u8, i64)>, Vec<Level>)> = Vec::new();
    let mut tiles: Vec<usize> = Vec::new();
    for (key, members) in &by_series {
        let mut layers: BTreeMap<(u32, u32), Vec<usize>> = BTreeMap::new();
        for &k in members {
            match ps[k].layer {
                Some(l) if ps[k].conforming() => layers.entry(l).or_default().push(k),
                _ => tiles.push(k),
            }
        }
        let mut levels = Vec::new();
        for b in layers.values() {
            match classify(b, &ps) {
                Some(lv) => levels.push(lv),
                None => tiles.extend(b),
            }
        }
        levels.sort_by_key(|lv| (lv.factor, lv.layer));
        if !levels.is_empty() {
            let pt = levels[0].form.0;
            let (kept, other): (Vec<Level>, Vec<Level>) = levels.into_iter().partition(|lv| lv.form.0 == pt);
            for lv in &other {
                tiles.extend(lv.cells.iter().map(|c| c.0));
            }
            rules.check("image", rules::env([("levels", num(kept.len() as f64))]))?;
            images.push((key.clone(), kept));
        }
    }
    rules.check("images", rules::env([("images", num(images.len() as f64))]))?;
    // the images (§4.2
    // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#42-images>, §4.3
    // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#43-image-arrays>)
    let um = 1.0 / 1e-6;
    let px = v.px.map(|x| x * um);
    let py = v.py.map(|x| x * um);
    let mut images_json = Vec::new();
    for (key, levels) in &images {
        let (dtype, p, q) = coding::pixel_type(levels[0].form.0).unwrap();
        let planes: Vec<(i64, i64, i64)> = levels.iter().flat_map(|lv| lv.cells.iter().map(|c| ps[c.0].plane)).collect();
        let (axes, lo, extent) = plane_axes(&planes, p);
        let dims = dims_json(key);
        let name = dims
            .get("S")
            .and_then(|s| v.scene(s.as_i64().unwrap()))
            .filter(|n| !n.is_empty() && n.len() <= 256)
            .map(|n| n.to_string());
        let mut units = serde_json::Map::new();
        for (a, has) in [("x", px.is_some()), ("y", py.is_some()), ("z", v.pz.is_some())] {
            if has {
                units.insert(a.into(), json!("micrometer"));
            }
        }
        if v.inc.is_some() {
            units.insert("t".into(), json!("second"));
        }
        let mut scales = Vec::new();
        let mut translations = Vec::new();
        let mut finite = true;
        for lv in levels {
            let f = lv.factor as f64;
            let one = |a: &str| -> f64 {
                match a {
                    "t" => v.inc.unwrap_or(1.0),
                    "c" => 1.0,
                    "z" => v.pz.map(|z| z * um).unwrap_or(1.0),
                    "y" => py.map(|y| y * f).unwrap_or(f),
                    _ => px.map(|x| x * f).unwrap_or(f),
                }
            };
            let sx = (lv.origin.0 as f64 + (f - 1.0) / 2.0) * px.unwrap_or(1.0);
            let sy = (lv.origin.1 as f64 + (f - 1.0) / 2.0) * py.unwrap_or(1.0);
            let tr: Vec<f64> = axes.iter().map(|&a| if a == "x" { sx } else if a == "y" { sy } else { 0.0 }).collect();
            finite &= tr.iter().all(|t| t.is_finite());
            scales.push(axes.iter().map(|&a| json!(one(a))).collect::<Vec<_>>());
            translations.push(tr.into_iter().map(|t| json!(t)).collect::<Vec<_>>());
        }
        rules.check("translation", rules::env([("all_finite", flag(finite))]))?;
        let mut lvs = Vec::new();
        for lv in levels {
            let ((w, h), (w2, h2), (m, r)) = (lv.tile, lv.edge, lv.grid);
            let (y_len, rows, per) = bands(h as u64, h2 as u64, r as u64, w as u64, q, lv.form.1 == coding::UNCOMPRESSED, max_band, floor);
            let size = [
                ("t", checked_extent(rules, extent[0] as i128)?),
                ("c", checked_extent(rules, extent[1] as i128 * p as i128)?),
                ("z", checked_extent(rules, extent[2] as i128)?),
                ("y", checked_extent(rules, (r as i128 - 1) * h as i128 + h2 as i128)?),
                ("x", checked_extent(rules, (m as i128 - 1) * w as i128 + w2 as i128)?),
            ];
            let x_len = if w2 == w { json!(w) } else if m > 1 { json!([[w, m - 1], w2]) } else { json!([w2]) };
            let lens = |a: &str| match a {
                "t" | "z" => json!(1),
                "c" => json!(p),
                "y" => y_len.clone(),
                _ => x_len.clone(),
            };
            let cells: Vec<J> = lv
                .cells
                .iter()
                .map(|&(k, c, j)| {
                    let s = &ps[k];
                    json!([s.el, s.plane.0 - lo[0], s.plane.1 - lo[1], s.plane.2 - lo[2], c, j, s.w, s.h])
                })
                .collect();
            // `declared_by`: each cell's subblock's struct, the key of its description
            let keys: Vec<u32> = lv.cells.iter().map(|c| ps[c.0].key).collect();
            lvs.push(json!({
                "shape": axes.iter().map(|a| json!(size.iter().find(|x| x.0 == *a).unwrap().1 as i64)).collect::<Vec<_>>(),
                "chunks": axes.iter().map(|a| lens(a)).collect::<Vec<_>>(),
                "factor": lv.factor, "grid": [m, r],
                "band": {"rows": rows, "per": per},
                "cells": cells,
                "declared_by": keys,
            }));
        }
        images_json.push(json!({
            "dims": dims, "name": name, "axes": axes, "lo": {"t": lo[0], "c": lo[1], "z": lo[2]},
            "extent": {"t": extent[0], "c": extent[1], "z": extent[2]},
            "form": [levels[0].form.0, levels[0].form.1, levels[0].form.2], "dtype": dtype,
            "units": units, "scales": scales, "translations": translations, "levels": lvs,
        }));
    }
    // tiles, one array per tile position and copy (§4.4
    // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#44-tiles>)
    tiles.sort_by_key(|&k| ps[k].index);
    type G = (Vec<(u8, i64)>, i64, i64, i64, i64, i64, i64, u64, u64, (i32, i32, bool));
    let mut copies: HashMap<(G, (i64, i64, i64)), u64> = HashMap::new();
    let mut order: Vec<(G, u64)> = Vec::new();
    let mut groups: HashMap<(G, u64), Vec<usize>> = HashMap::new();
    for &k in &tiles {
        let p = &ps[k];
        let g: G = (p.series.clone(), p.x, p.y, p.wl, p.hl, p.w, p.h, p.cw, p.ch, p.form);
        let c = copies.entry((g.clone(), p.plane)).or_insert(0);
        let copy = *c;
        *c += 1;
        let key = (g, copy);
        if !groups.contains_key(&key) {
            order.push(key.clone());
        }
        groups.entry(key).or_default().push(k);
    }
    let mut tiles_json = Vec::new();
    for key in &order {
        let members = &groups[key];
        let first = &ps[members[0]];
        let (dtype, p, q) = coding::pixel_type(first.form.0).unwrap();
        let planes: Vec<(i64, i64, i64)> = members.iter().map(|&k| ps[k].plane).collect();
        let (axes, lo, extent) = plane_axes(&planes, p);
        let (y_len, rows, per) = bands(first.ch, first.ch, 1, first.cw, q, first.form.1 == coding::UNCOMPRESSED, max_band, floor);
        let size = [
            ("t", checked_extent(rules, extent[0] as i128)?),
            ("c", checked_extent(rules, extent[1] as i128 * p as i128)?),
            ("z", checked_extent(rules, extent[2] as i128)?),
            ("y", checked_extent(rules, first.ch as i128)?),
            ("x", checked_extent(rules, first.cw as i128)?),
        ];
        let lens = |a: &str| match a {
            "t" | "z" => json!(1),
            "c" => json!(p),
            "y" => y_len.clone(),
            _ => json!(first.cw),
        };
        tiles_json.push(json!({
            "dims": dims_json(&first.series), "x": first.x, "y": first.y, "size": [first.wl, first.hl],
            "stored_size": [first.w, first.h], "planes": {"t": lo[0], "c": lo[1], "z": lo[2]}, "copy": key.1,
            "axes": axes, "form": [first.form.0, first.form.1, first.form.2], "dtype": dtype,
            "shape": axes.iter().map(|a| json!(size.iter().find(|x| x.0 == *a).unwrap().1 as i64)).collect::<Vec<_>>(),
            "chunks": axes.iter().map(|a| lens(a)).collect::<Vec<_>>(),
            "band": {"rows": rows, "per": per},
            "members": members.iter().map(|&k| {
                let s = &ps[k];
                json!([s.el, s.plane.0 - lo[0], s.plane.1 - lo[1], s.plane.2 - lo[2], 0, 0, s.w, s.h])
            }).collect::<Vec<_>>(),
            // `declared_by`: each member's subblock's struct, the key of its description
            "declared_by": members.iter().map(|&k| ps[k].key).collect::<Vec<_>>(),
        }));
    }
    let h = header;
    Ok(json!({
        "root": {"version": [h["major"], h["minor"]], "primary_file_guid": h["primary_file_guid"], "file_guid": h["file_guid"],
                 "file_part": h["file_part"], "update_pending": h["update_pending"]},
        "subblocks": subblocks,
        "images": images_json,
        "tiles": tiles_json,
        "values": v.to_json(),
    }))
}
