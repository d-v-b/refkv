//! refkv's plan of a LIF, LOF or LIFEXT file's images, and of a LAS X project's (spec/zarrify/lif.md
//! §3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#3-placement>, §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#4-output> and §8.4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#84-the-images>), made from the
//! parser's description alone (§9 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#9-the-description>):
//! each image's checks (§7.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#73-rejection>),
//! its data type, channels, axes, fields, bands, scales and units, the memory block its planes
//! lie in, and its source metadata; of a project, the walk over its documents, which images it
//! presents, and the LOFs no image uses. These are decisions of refkv's convention, which the
//! description does not hold.

use crate::error::Error;

use super::{given, integer};
use crate::expr::V;
use crate::out::MAX_PAYLOAD;
use crate::rules::{self, Rules, flag, num, text};
use serde_json::{Map, Value as J, json};
use std::collections::{HashMap, HashSet};

/// The bytes of a band of rows (spec/zarrify/lif.md §3.4
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#34-rows-and-bands>).
const BAND: u64 = 1 << 24;

fn n(v: &J) -> u64 {
    v.as_u64().unwrap_or(0)
}

fn arr(v: &J) -> &[J] {
    v.as_array().map_or(&[], Vec::as_slice)
}

/// An attribute of a description's attributes object.
fn attr(m: &J, name: &str) -> Option<String> {
    m.get(name).and_then(J::as_str).map(str::to_string)
}

/// An attribute's decimal (whitespace trimmed), when finite.
fn decimal(v: Option<&str>) -> Option<f64> {
    crate::xml::decimal_value(v?.trim_matches([' ', '\t', '\r', '\n']), false)
}

/// The bytes of a sample of `resolution` bits (spec/zarrify/lif.md §3.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#31-data-types>).
pub(super) fn item(resolution: u64) -> u64 {
    match resolution {
        1..=8 => 1,
        9..=16 => 2,
        17..=32 => 4,
        _ => 8,
    }
}

/// A unit symbol (conventions §5 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#5-units>):
/// its unit and, for a length, its size in meters.
fn unit(s: &str) -> Option<(&'static str, Option<f64>)> {
    Some(match s {
        "\u{b5}m" | "\u{3bc}m" | "um" => ("micrometer", Some(1e-6)),
        "nm" => ("nanometer", Some(1e-9)),
        "mm" => ("millimeter", Some(1e-3)),
        "cm" => ("centimeter", Some(1e-2)),
        "m" => ("meter", Some(1.0)),
        "\u{c5}" | "\u{212b}" => ("angstrom", Some(1e-10)),
        "pm" => ("picometer", Some(1e-12)),
        "in" => ("inch", Some(0.0254)),
        "ft" => ("foot", Some(0.3048)),
        "s" => ("second", None),
        "ms" => ("millisecond", None),
        "min" => ("minute", None),
        "h" => ("hour", None),
        _ => return None,
    })
}

/// A channel's color from its ChannelTag (1, 2, 3: red, green, blue) or its LUTName
/// (spec/zarrify/lif.md §4.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#43-image-arrays>).
fn color(tag: Option<u64>, lut: Option<&str>) -> &'static str {
    match tag {
        Some(1) => return "FF0000",
        Some(2) => return "00FF00",
        Some(3) => return "0000FF",
        _ => {}
    }
    match lut.map(|l| l.to_ascii_lowercase()).as_deref() {
        Some("red") => "FF0000",
        Some("green") => "00FF00",
        Some("blue") => "0000FF",
        Some("cyan") => "00FFFF",
        Some("magenta") => "FF00FF",
        Some("yellow") => "FFFF00",
        _ => "FFFFFF",
    }
}

/// One image of a description, as the plan reads it.
struct Image {
    name: Option<String>,
    memory: Option<String>,
    /// DimID, NumberOfElements, BytesInc, Origin, Length, Unit, BitInc (attributes as given)
    dims: Vec<[Option<String>; 7]>,
    /// DataType, Resolution, BytesInc, ChannelTag, LUTName, BitInc (as given)
    channels: Vec<[Option<String>; 6]>,
    /// path, parent, attributes, memory, dimensions and channels, as the image's source metadata
    /// gives them (spec/zarrify/lif.md §5.2
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#52-the-images>)
    meta: Map<String, J>,
    /// the TileScanInfo attachment's attributes, and its tiles'
    scan: Option<(J, Vec<J>)>,
}

impl Image {
    fn of(d: &J) -> Image {
        let mut meta = Map::new();
        meta.insert("path".into(), d["path"].clone());
        if !d["parent"].is_null() {
            meta.insert("parent".into(), d["parent"].clone());
        }
        meta.insert("attributes".into(), d["attributes"].clone());
        if !d["memory"].is_null() {
            meta.insert("memory".into(), d["memory"].clone());
        }
        meta.insert("dimensions".into(), d["dimensions"].clone());
        meta.insert("channels".into(), d["channels"].clone());
        let dim_keys = ["DimID", "NumberOfElements", "BytesInc", "Origin", "Length", "Unit", "BitInc"];
        let channel_keys = ["DataType", "Resolution", "BytesInc", "ChannelTag", "LUTName", "BitInc"];
        Image {
            name: d["name"].as_str().map(str::to_string),
            memory: attr(&d["memory"], "MemoryBlockID"),
            dims: arr(&d["dimensions"]).iter().map(|c| dim_keys.map(|k| attr(c, k))).collect(),
            channels: arr(&d["channels"]).iter().map(|c| channel_keys.map(|k| attr(c, k))).collect(),
            meta,
            scan: d["tile_scan"].as_object().map(|s| (s["attributes"].clone(), arr(&s["tiles"]).to_vec())),
        }
    }
}

/// refkv's plan of a LIF, LOF or LIFEXT file's description: each image's layout and source
/// metadata (`images`), the series, and the file's version `V` (spec/zarrify/lif.md §5.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#51-the-root>): its root's, or 2 of
/// an older writer's LOF, whose root has none and which is read as of version 2.
pub fn plan(description: &J) -> crate::error::Res<J> {
    let (images, series) = images(description, 0)?;
    let version = description["version"].as_u64().unwrap_or(2);
    Ok(json!({"images": images, "series": series, "version": version}))
}

/// The images of a file's description, whose planes start `offset` bytes into their memory blocks'
/// data (a project's `Block`'s `Offset`; 0 of a file input), checked in document order; the series.
fn images(d: &J, offset: u64) -> crate::error::Res<(Vec<J>, u64)> {
    let rules = rules::of("lif");
    let lof = d["file"] == "lof";
    let size = n(&d["size"]);
    let blocks = arr(&d["blocks"]);
    let mut named: Vec<Option<usize>> = vec![None; blocks.len()];
    // the blocks of each ID, in the file's order (a null ID names no block)
    let mut by_id: HashMap<&str, Vec<usize>> = HashMap::new();
    for (i, b) in blocks.iter().enumerate() {
        if let Some(id) = b["id"].as_str() {
            by_id.entry(id).or_default().push(i);
        }
    }
    let mut out = Vec::new();
    let mut series = 0u64;
    for (k, j) in arr(&d["images"]).iter().enumerate() {
        let im = Image::of(j);
        let shown = im.name.clone().unwrap_or_default();
        let mut f = image(rules, k, &shown, &im, lof, size)?;
        // a LOF's one block has no ID: its XML's MemoryBlockID, if it has one, names nothing
        let id = if lof { "of the LOF".to_string() } else { im.memory.clone().unwrap_or_default() };
        let found: &[usize] = if lof { &[0] } else { by_id.get(id.as_str()).map_or(&[], Vec::as_slice) };
        let block = found.first().copied();
        let other = block.and_then(|b| named[b]);
        let held = block.map_or(0, |b| n(&blocks[b]["size"]));
        let need = f.end + offset as u128;
        rules.check(
            "memory",
            rules::env([
                ("k", num(k as f64)),
                ("name", text(&shown)),
                ("id", text(&id)),
                ("found", num(found.len() as f64)),
                ("other", other.map_or(V::Absent, |o| num(o as f64))),
                ("need", num(need as f64)),
                ("need_shown", text(&need.to_string())),
                ("held", num(held as f64)),
            ]),
        )?;
        let b = block.ok_or_else(|| Error::internal("no block"))?;
        named[b] = Some(k);
        f.place(n(&blocks[b]["data"]) + offset, b);
        series += f.fields.len() as u64;
        rules.check("series", rules::env([("count", num(series as f64))]))?;
        out.push(image_facts(&im, &f, k));
    }
    Ok((out, series))
}

/// An image's checks and layout (spec/zarrify/lif.md §3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#3-placement>), of a file of `size`
/// bytes.
fn image(rules: &Rules, k: usize, name: &str, im: &Image, lof: bool, size: u64) -> crate::error::Res<Layout> {
    let base = |extra: Vec<(&'static str, V)>| {
        let mut v = rules::env([("k", num(k as f64)), ("name", text(name))]);
        for (a, b) in extra {
            v.insert(a, b);
        }
        v
    };
    rules.check(
        "image",
        base(vec![
            ("memory", flag(im.memory.is_some() || lof)),
            ("dimensions", num(im.dims.len() as f64)),
            ("channels", num(im.channels.len() as f64)),
        ]),
    )?;
    // (DimID, NumberOfElements, BytesInc)
    let mut dims: Vec<(u64, u64, u64)> = Vec::new();
    for (j, d) in im.dims.iter().enumerate() {
        rules.check(
            "dimension",
            base(vec![
                ("j", num(j as f64)),
                ("dim_id", given(&d[0])),
                ("elements", given(&d[1])),
                ("bytes_inc", given(&d[2])),
                ("bit_inc", given(&d[6])),
            ]),
        )?;
        let int = |i: usize| integer(d[i].as_deref()).ok_or_else(|| Error::internal("a dimension's attribute is not an integer"));
        dims.push((int(0)?, int(1)?, int(2)?));
    }
    let mut channels: Vec<(u64, u64, u64)> = Vec::new(); // (DataType, item size, BytesInc)
    let mut resolutions = Vec::new();
    for (j, c) in im.channels.iter().enumerate() {
        rules.check(
            "channel",
            base(vec![
                ("j", num(j as f64)),
                ("data_type", given(&c[0])),
                ("resolution", given(&c[1])),
                ("bytes_inc", given(&c[2])),
                ("bit_inc", given(&c[5])),
            ]),
        )?;
        let int = |i: usize| integer(c[i].as_deref()).ok_or_else(|| Error::internal("a channel's attribute is not an integer"));
        let (dt, res) = (int(0)?, int(1)?);
        channels.push((dt, item(res), int(2)?));
        resolutions.push(res);
    }
    // the references a run may make (refs.rs), which bound the planes before they are listed
    let references = (1u128 << 22) + 4 * size as u128;
    Layout::of(rules, base(vec![("references", num(references as f64))]), &dims, &channels, &resolutions, im)
}

/// An image's layout (spec/zarrify/lif.md §3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#3-placement>).
struct Layout {
    data_type: &'static str,
    item: u64,
    /// the channels' BytesInc, ascending, and each one's index in the XML
    offsets: Vec<u64>,
    order: Vec<usize>,
    interleaved: bool,
    axes: Vec<&'static str>,
    shape: Vec<u64>,
    chunk_shape: Vec<u64>,
    /// (count, stride) of t and z (count 1 without the axis)
    t: (u64, u64),
    z: (u64, u64),
    rows: u64,
    row: u64,
    y_stride: u64,
    band: u64,
    /// the field dimensions (index in the list, DimID, count, stride), and each field's
    /// indexes and offset
    field_dims: Vec<(usize, u64, u64, u64)>,
    fields: Vec<(Vec<u64>, u64)>,
    scale: Vec<J>,
    units: Map<String, J>,
    resolution: Vec<u64>,
    /// the bytes of the block the image needs
    end: u128,
    base: u64,
    block: usize,
}

impl Layout {
    fn of(rules: &Rules, mut env: rules::Vars<'static>, dims: &[(u64, u64, u64)], channels: &[(u64, u64, u64)],
          resolutions: &[u64], im: &Image) -> crate::error::Res<Layout> {
        let used: Vec<usize> = (0..dims.len()).filter(|&j| dims[j].1 > 1).collect();
        let x = used.iter().copied().find(|&j| dims[j].0 == 1).or_else(|| dims.iter().position(|d| d.0 == 1));
        let mut seen: HashMap<u64, usize> = HashMap::new();
        let repeated = used.iter().find_map(|&j| seen.insert(dims[j].0, j).map(|_| dims[j].0));
        let types: Vec<(u64, u64)> = {
            let mut v: Vec<(u64, u64)> = channels.iter().map(|c| (c.0, c.1)).collect();
            v.sort_unstable();
            v.dedup();
            v
        };
        let (dt, item) = (channels[0].0, channels[0].1);
        let mut order: Vec<usize> = (0..channels.len()).collect();
        order.sort_by_key(|&j| (channels[j].2, j));
        let offsets: Vec<u64> = order.iter().map(|&j| channels[j].2).collect();
        let distinct = offsets.windows(2).all(|w| w[0] < w[1]);
        let m = channels.len() as u64;
        let sx = x.map_or(0, |j| dims[j].2);
        let nx = x.map_or(1, |j| dims[j].1);
        let interleaved = m > 1 && sx == m * item && offsets.iter().enumerate().all(|(j, &o)| o == j as u64 * item);
        let planar = sx == item;
        let y = used.iter().copied().find(|&j| dims[j].0 == 2);
        let (ny, sy) = y.map_or((1, 0), |j| (dims[j].1, dims[j].2));
        let row = nx.saturating_mul(sx);
        let rows_overlap = ny > 1 && sy < row;
        let find = |id: u64| used.iter().copied().find(|&j| dims[j].0 == id).map(|j| (dims[j].1, dims[j].2));
        let (t, z) = (find(4), find(3));
        let mut axes = Vec::new();
        let mut shape = Vec::new();
        let mut chunk = Vec::new();
        if let Some((n, _)) = t {
            axes.push("t");
            shape.push(n);
            chunk.push(1);
        }
        if m > 1 {
            axes.push("c");
            shape.push(m);
            chunk.push(if interleaved { m } else { 1 });
        }
        if let Some((n, _)) = z {
            axes.push("z");
            shape.push(n);
            chunk.push(1);
        }
        axes.extend(["y", "x"]);
        shape.extend([ny, nx]);
        chunk.extend([1, nx]);
        let biggest = shape.iter().copied().max().unwrap_or(1);
        let end: u128 = offsets.last().copied().unwrap_or(0) as u128
            + dims.iter().map(|d| (d.1.saturating_sub(1)) as u128 * d.2 as u128).sum::<u128>()
            + item as u128;
        let field_dims: Vec<(usize, u64, u64, u64)> =
            used.iter().copied().filter(|&j| !(1..=4).contains(&dims[j].0)).map(|j| (j, dims[j].0, dims[j].1, dims[j].2)).collect();
        let field_count: u128 = field_dims.iter().map(|d| d.2 as u128).product();
        env.insert("x", flag(x.is_some()));
        env.insert("repeated", repeated.map_or(V::Absent, |r| num(r as f64)));
        env.insert("types", num(types.len() as f64));
        env.insert("distinct", flag(distinct));
        env.insert("offsets", text(&format!("{offsets:?}")));
        env.insert("x_bytes_inc", num(sx as f64));
        env.insert("arranged", flag(interleaved || planar));
        env.insert("rows_overlap", flag(rows_overlap));
        env.insert("row", num(row as f64));
        env.insert("y_bytes_inc", num(sy as f64));
        env.insert("extent", num(biggest as f64));
        env.insert("fields", num(field_count as f64));
        let chunked = if interleaved { 1 } else { m as u128 };
        let planes = t.map_or(1, |p| p.0 as u128) * chunked * z.map_or(1, |p| p.0 as u128) * field_count;
        env.insert("planes", num(planes as f64));
        rules.check("layout", env)?;
        let data_type = match (dt, item) {
            (0, 1) => "uint8",
            (0, 2) => "uint16",
            (0, 4) => "uint32",
            (0, _) => "uint64",
            (_, 2) => "float16",
            (_, 4) => "float32",
            _ => "float64",
        };
        // scales and units (spec/zarrify/lif.md §4.3
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#43-image-arrays>)
        let mut units = Map::new();
        let scale = axes
            .iter()
            .map(|&a| {
                let id = match a {
                    "t" => 4,
                    "z" => 3,
                    "y" => 2,
                    "x" => 1,
                    _ => return json!(1),
                };
                let Some(j) = used.iter().copied().find(|&j| dims[j].0 == id) else { return json!(1) };
                let d = &im.dims[j];
                let step = decimal(d[4].as_deref()).map(|l| (l / (dims[j].1 - 1) as f64).abs());
                let Some(step) = step.filter(|s| s.is_finite() && *s > 0.0) else { return json!(1) };
                match (unit(d[5].as_deref().unwrap_or("")), a) {
                    (Some((_, Some(m))), "x" | "y" | "z") => {
                        let v = step * (m / 1e-6);
                        if !v.is_finite() || v <= 0.0 {
                            return json!(1);
                        }
                        units.insert(a.into(), json!("micrometer"));
                        json!(v)
                    }
                    (Some((u, None)), "t") => {
                        units.insert(a.into(), json!(u));
                        json!(step)
                    }
                    _ => json!(step),
                }
            })
            .collect();
        let mut resolution = vec![0; channels.len()];
        for (c, &j) in order.iter().enumerate() {
            resolution[c] = resolutions[j];
        }
        Ok(Layout {
            data_type,
            item,
            offsets,
            order,
            interleaved,
            axes,
            shape,
            chunk_shape: chunk,
            t: t.unwrap_or((1, 0)),
            z: z.unwrap_or((1, 0)),
            rows: ny,
            row,
            y_stride: if ny == 1 { row } else { sy },
            band: 1,
            field_dims,
            fields: Vec::new(),
            scale,
            units,
            resolution,
            end,
            base: 0,
            block: 0,
        })
    }

    /// The fields' offsets and the band height (spec/zarrify/lif.md §3.4
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#34-rows-and-bands>), once
    /// the image is known to lie within its block, whose data starts at `base`.
    fn place(&mut self, base: u64, block: usize) {
        self.base = base;
        self.block = block;
        let count: u64 = self.field_dims.iter().map(|d| d.2).product();
        self.fields = (0..count)
            .map(|f| {
                let mut rest = f;
                let mut idx = Vec::new();
                let mut off = 0;
                for d in &self.field_dims {
                    idx.push(rest % d.2);
                    off += rest % d.2 * d.3;
                    rest /= d.2;
                }
                (idx, off)
            })
            .collect();
        let (ny, row, sy) = (self.rows, self.row, self.y_stride);
        // the farthest plane: its last time, channel, depth and field
        let far = base
            + if self.interleaved { 0 } else { self.offsets.last().copied().unwrap_or(0) }
            + (self.t.0 - 1) * self.t.1
            + (self.z.0 - 1) * self.z.1
            + self.fields.last().map_or(0, |f| f.1);
        let most = (BAND / row.max(1)).clamp(1, ny);
        self.band = (1..=most)
            .rev()
            .find(|&b| ny % b == 0 && (sy == row || rows_payload(far + (ny - b) * sy, sy, row, b) <= MAX_PAYLOAD as u64))
            .unwrap_or(1);
        let n = self.chunk_shape.len();
        self.chunk_shape[n - 2] = self.band;
    }
}

/// The size of a varint (spec/zarrify.md §1.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>).
fn varint_size(v: u64) -> u64 {
    (64 - v.leading_zeros() as u64).div_ceil(7).max(1)
}

/// The reference payload of `count` ranges of `length` bytes, `stride` bytes apart from `start`
/// (each offset and length above 0, `stride` too), as `out::payload_size` gives it, counted by
/// the offsets' varint sizes rather than built.
fn rows_payload(start: u64, stride: u64, length: u64, count: u64) -> u64 {
    if count == 1 {
        return 2 + varint_size(start) + varint_size(length);
    }
    // each range a Concat entry: tag, its length (one byte), then 0x18 varint(o) 0x20 varint(n)
    let mut offsets = 0;
    let mut below = 0;
    for k in 1..=9u64 {
        let limit = 1u64 << (7 * k);
        let n = if start >= limit { 0 } else { count.min((limit - start).div_ceil(stride)) };
        offsets += (n - below) * k;
        below = n;
    }
    offsets += (count - below) * 10;
    count * (4 + varint_size(length)) + offsets
}

/// An image's plan: its source metadata and its layout, for the projection.
fn image_facts(im: &Image, f: &Layout, k: usize) -> J {
    // a mosaic's tiles: when its one field dimension is the mosaic's (DimID 10)
    let tiles: &[J] = match &im.scan {
        Some((_, tiles)) if f.field_dims.len() == 1 && f.field_dims[0].1 == 10 => tiles,
        _ => &[],
    };
    let colors: Vec<&str> = f.order.iter().map(|&j| color(integer(im.channels[j][3].as_deref()), im.channels[j][4].as_deref())).collect();
    let mut meta = Map::new();
    meta.insert("image".into(), json!(k));
    meta.extend(im.meta.clone());
    if let Some((scan, _)) = im.scan.as_ref().filter(|_| !tiles.is_empty()) {
        meta.insert("tile_scan".into(), scan.clone());
    }
    json!({
        "name": im.name, "meta": meta, "data_type": f.data_type, "item": f.item,
        "interleaved": f.interleaved, "axes": f.axes, "shape": f.shape, "chunk_shape": f.chunk_shape,
        "offsets": f.offsets, "order": f.order, "colors": colors, "resolution": f.resolution,
        "t": [f.t.0, f.t.1], "z": [f.z.0, f.z.1], "rows": f.rows, "row": f.row, "y_stride": f.y_stride,
        "band": f.band, "base": f.base, "block": f.block, "end": f.end as u64,
        "field_dims": f.field_dims.iter().map(|d| d.1).collect::<Vec<_>>(),
        "fields": f.fields.iter().map(|(i, o)| json!([i, o])).collect::<Vec<_>>(),
        "tiles": tiles,
        "scale": f.scale, "units": f.units,
    })
}

// ---- a LAS X project (spec/zarrify/lif.md §8
// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#8-las-x-documents>)

/// Why a document's image is not presented (spec/zarrify/lif.md §8.4
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#84-the-images>).
fn left_out(doc: &J) -> String {
    let entries = arr(&doc["entries"]);
    let Some(first) = entries.first() else {
        return "its Memory names no file".into();
    };
    let file = first["file"].as_str().unwrap_or("");
    let tag = first["tag"].as_str().unwrap_or("");
    let suffix = file.rfind('.').map_or("", |k| &file[k..]).to_ascii_lowercase();
    let n = entries.len();
    let (s, verb) = if n == 1 { ("", "is") } else { ("s", "are") };
    format!("its {n} {tag}{s} of {suffix} file{s} {verb} not read: the profile presents one Block of a LOF")
}

/// refkv's plan of a LAS X project's description: each LOF's images and its `Block`'s checks,
/// then the walk over the documents, a document before those it names (spec/zarrify/lif.md §8.4
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#84-the-images>): the images it
/// presents (`images`), those it leaves out (`not_presented`), the LOFs no image uses (`unused`),
/// the series and the LOFs' blocks.
pub fn plan_set(d: &J) -> crate::error::Res<J> {
    let rules = rules::of("lif");
    let docs = arr(&d["documents"]);
    let lofs = arr(&d["lofs"]);
    // each LOF's images, checked as a file's, whose planes count from its Block's Offset
    let mut planned: Vec<Vec<J>> = Vec::with_capacity(lofs.len());
    for l in lofs {
        let key = l["key"].as_str().unwrap_or("");
        let entry = &docs[n(&l["document"]) as usize]["entries"][0];
        let offset = integer(entry["offset"].as_str()).unwrap_or(0);
        let (images, _) = images(&l["description"], offset).map_err(|e| e.context(|m| format!("the LOF {key:?}: {m}")))?;
        planned.push(images);
    }
    // each LOF against the Block that names it
    for (l, images) in lofs.iter().zip(&planned) {
        let doc = &docs[n(&l["document"]) as usize];
        let e = &doc["entries"][0];
        let offset = integer(e["offset"].as_str()).unwrap_or(0) as u128;
        let size = integer(e["size"].as_str()).unwrap_or(0) as u128;
        let held = arr(&l["description"]["blocks"]).first().map_or(0, |b| n(&b["size"])) as u128;
        let need = images.first().map_or(0, |im| n(&im["end"])) as u128;
        let image = doc["image"].as_bool().unwrap_or(false);
        rules.check(
            "block_fits",
            rules::env([
                ("document", text(doc["key"].as_str().unwrap_or(""))),
                ("key", text(l["key"].as_str().unwrap_or(""))),
                ("end", text(&(offset + size).to_string())),
                ("fits", flag(offset + size <= held)),
                ("held", text(&held.to_string())),
                ("size", text(&size.to_string())),
                ("need", text(&need.to_string())),
                ("covers", flag(!image || need <= size)),
            ]),
        )?;
    }
    let mut walk = Walk { rules, docs, lofs, planned, used: vec![false; lofs.len()], images: Vec::new(), not_presented: Vec::new(), series: 0 };
    let mut seen = HashSet::from([0usize]);
    let mut stack: Vec<(usize, usize)> = vec![(0, 0)];
    walk.visit(&stack)?;
    while let Some(top) = stack.last_mut() {
        let Some(r) = arr(&docs[top.0]["references"]).get(top.1) else {
            stack.pop();
            continue;
        };
        top.1 += 1;
        let target = r["document"].as_u64().ok_or_else(|| Error::internal("a reference names no document"))? as usize;
        if !seen.insert(target) {
            continue;
        }
        if stack.len() as u32 >= crate::beside::MAX_DEPTH {
            return Err(crate::xlef::depth());
        }
        stack.push((target, 0));
        walk.visit(&stack)?;
    }
    // the LOFs that no image's chunks reference, in the order they were found
    let files = arr(&d["files"]);
    let listed = |key: &J| files.iter().position(|f| f == key).map_or(0, |i| i + 1);
    let mut unused: Vec<&J> = lofs.iter().zip(&walk.used).filter(|(_, u)| !**u).map(|(l, _)| l).collect();
    unused.sort_by_key(|l| listed(&l["key"]));
    let unused: Vec<J> = unused.iter().map(|l| json!([l["key"], l["size"]])).collect();
    let blocks: usize = lofs.iter().map(|l| arr(&l["description"]["blocks"]).len()).sum();
    Ok(json!({"images": walk.images, "not_presented": walk.not_presented, "unused": unused, "series": walk.series,
              "blocks": blocks}))
}

/// The walk over a project's documents.
struct Walk<'a> {
    rules: &'static Rules,
    docs: &'a [J],
    lofs: &'a [J],
    /// each LOF's images, and whether an image of the output is its
    planned: Vec<Vec<J>>,
    used: Vec<bool>,
    images: Vec<J>,
    not_presented: Vec<J>,
    series: u64,
}

impl Walk<'_> {
    /// The document on the top of `stack`: its image's plan, or why it is not presented.
    fn visit(&mut self, stack: &[(usize, usize)]) -> crate::error::Res<()> {
        let d = stack.last().ok_or_else(|| Error::internal("no document"))?.0;
        let doc = &self.docs[d];
        if !doc["image"].as_bool().unwrap_or(false) {
            return Ok(());
        }
        let path: Vec<J> = stack.iter().map(|&(s, _)| self.docs[s]["name"].clone()).collect();
        let lof = doc["lof"].as_u64().map(|l| l as usize).filter(|&l| !self.used[l]);
        let Some(l) = lof else {
            self.not_presented.push(json!({"path": path, "document": doc["key"], "reason": left_out(doc)}));
            return Ok(());
        };
        self.used[l] = true;
        let mut image = self.planned[l].first_mut().ok_or_else(|| Error::internal("a presented LOF has no image"))?.take();
        let meta = &mut image["meta"];
        meta["image"] = json!(self.images.len());
        meta["path"] = J::Array(path);
        meta["attributes"] = doc["attributes"].clone();
        if !doc["memory"].is_null() {
            meta["memory"] = doc["memory"].clone();
        }
        image["object"] = self.lofs[l]["object"].clone();
        image["name"] = doc["name"].clone();
        self.series += arr(&image["fields"]).len() as u64;
        self.rules.check("series", rules::env([("count", num(self.series as f64))]))?;
        self.images.push(image);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::out::{payload_size, Part};

    #[test]
    fn rows_payload_is_the_references() {
        for (start, stride, length, count) in [(1, 9, 6, 1), (100, 9, 6, 50), (16000, 9, 6, 200), (73728, 9, 6, 8192), (1 << 40, 1 << 20, 3, 7), (5, 300, 200, 4096)] {
            let parts: Vec<Part> = (0..count).map(|r| Part::Src(start + r * stride, length)).collect();
            assert_eq!(rows_payload(start, stride, length, count), payload_size(&parts) as u64, "{start} {stride} {count}");
        }
    }
}
