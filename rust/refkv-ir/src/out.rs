//! A projection's output (spec/zarrify.md §1.1
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#11-output-and-equivalence>, §1.2
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>): the entries of a refkv
//! archive before it is written. The archive writer stays per host (Python's `Output.write`,
//! the browser's `writeVzip`); an `Out` crosses to it as Python objects (python/src/lib.rs) or
//! as one binary buffer (`encode`, for wasm hosts).
//!
//! Also the shared parts of the conventions every projection uses (conventions §2
//! <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#2-attributes>–§5
//! <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#5-units>,
//! §7 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#7-source-values-as-arrays>;
//! `refkv/zarrify/common.py` in Python): arrays' and groups' documents,
//! OME-NGFF multiscales, the convention's declaration, reference sizes, and the
//! cuts of metadata arrays.

use serde_json::{json, Map, Value as J};
use std::collections::HashMap;

/// A range of an output: of source 0 (offset, length), of a data source (index ≥ 1,
/// offset, length), or literal bytes.
#[derive(Clone, Debug, PartialEq)]
pub enum Part {
    Src(u64, u64),
    Data(u32, u64, u64),
    Lit(Vec<u8>),
    /// (key, offset, length): bytes of a store's object (conventions §8.9
    /// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#89-stores>), whose url source
    /// the host makes (spec/zarrify.md §1.4
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>: one per
    /// object referenced, its size pinned, in order of the entries' keys).
    Obj(String, u64, u64),
}

pub const MAX_PAYLOAD: usize = 65519;

/// The largest reference payload of a chunk, past which a projection of contiguous values
/// copies an edge chunk (conventions §7
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#7-source-values-as-arrays>):
/// MAX_PAYLOAD, but in tests (`set_copy_threshold`). Each run reads it once, into its facts.
static COPY_THRESHOLD: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(MAX_PAYLOAD);

/// For tests: the payload past which an edge chunk is copied (None: MAX_PAYLOAD), so that
/// small files reach the copy.
pub fn set_copy_threshold(n: Option<usize>) {
    COPY_THRESHOLD.store(n.unwrap_or(MAX_PAYLOAD), std::sync::atomic::Ordering::Relaxed);
}

/// The payload past which an edge chunk is copied, for a run's facts.
pub fn copy_threshold() -> usize {
    COPY_THRESHOLD.load(std::sync::atomic::Ordering::Relaxed)
}
pub const SOURCE_NODE: &str = "refkv_source";

#[derive(Default)]
pub struct Out {
    pub url: String,
    /// documents and copied bytes, in order of first write (a later write replaces)
    pub entries: Vec<(String, Vec<u8>)>,
    entry_at: HashMap<String, usize>,
    /// references
    pub refs: Vec<(String, Vec<Part>)>,
    ref_at: HashMap<String, usize>,
    /// the data sources 1, 2, ... in order of first use
    pub data: Vec<Vec<u8>>,
    data_at: HashMap<Vec<u8>, u32>,
    /// documents under these prefixes are read only when asked for, like chunks
    pub lazy: Vec<String>,
    pub summary: J,
}

/// JSON text as the hosts write documents: compact, UTF-8 (no \u escapes but
/// for control characters), members in insertion order.
pub fn to_text(v: &J) -> String {
    serde_json::to_string(v).unwrap()
}

impl Out {
    pub fn new(url: &str) -> Out {
        Out {
            url: url.to_string(),
            lazy: vec![format!("{SOURCE_NODE}/")],
            summary: json!({}),
            ..Default::default()
        }
    }

    pub fn json(&mut self, key: &str, v: &J) {
        self.bytes(key, to_text(v).into_bytes());
    }

    pub fn bytes(&mut self, key: &str, b: Vec<u8>) {
        match self.entry_at.get(key) {
            Some(&k) => self.entries[k].1 = b,
            None => {
                self.entry_at.insert(key.to_string(), self.entries.len());
                self.entries.push((key.to_string(), b));
            }
        }
    }

    pub fn has(&self, key: &str) -> bool {
        self.entry_at.contains_key(key)
    }

    /// The parts of the reference `key`, if there is one.
    pub fn reference(&self, key: &str) -> Option<&[Part]> {
        self.ref_at.get(key).map(|&k| self.refs[k].1.as_slice())
    }

    pub fn refs(&mut self, key: &str, parts: Vec<Part>) {
        match self.ref_at.get(key) {
            Some(&k) => self.refs[k].1 = parts,
            None => {
                self.ref_at.insert(key.to_string(), self.refs.len());
                self.refs.push((key.to_string(), parts));
            }
        }
    }

    /// A range of all of `value`, as a data source: added the first time it is used.
    pub fn shared(&mut self, value: &[u8]) -> Part {
        let i = match self.data_at.get(value) {
            Some(&i) => i,
            None => {
                self.data.push(value.to_vec());
                let i = self.data.len() as u32;
                self.data_at.insert(value.to_vec(), i);
                i
            }
        };
        Part::Data(i, 0, value.len() as u64)
    }

    /// The whole output as one buffer (little-endian; strings and byte strings are a
    /// u32 length and the bytes): "VZO1", the url, the summary's JSON, the lazy
    /// prefixes, the data sources, the entries (key, bytes), and the references
    /// (key, then its parts: 0 source 0 (u64 offset, u64 length), 1 a data source
    /// (u32 index, u64, u64), 2 literal bytes).
    pub fn encode(&self) -> Vec<u8> {
        let mut b = Vec::new();
        let s = |b: &mut Vec<u8>, x: &[u8]| {
            b.extend_from_slice(&(x.len() as u32).to_le_bytes());
            b.extend_from_slice(x);
        };
        b.extend_from_slice(b"VZO1");
        s(&mut b, self.url.as_bytes());
        s(&mut b, to_text(&self.summary).as_bytes());
        b.extend_from_slice(&(self.lazy.len() as u32).to_le_bytes());
        for p in &self.lazy {
            s(&mut b, p.as_bytes());
        }
        b.extend_from_slice(&(self.data.len() as u32).to_le_bytes());
        for d in &self.data {
            s(&mut b, d);
        }
        b.extend_from_slice(&(self.entries.len() as u32).to_le_bytes());
        for (k, v) in &self.entries {
            s(&mut b, k.as_bytes());
            s(&mut b, v);
        }
        b.extend_from_slice(&(self.refs.len() as u32).to_le_bytes());
        for (k, parts) in &self.refs {
            s(&mut b, k.as_bytes());
            b.extend_from_slice(&(parts.len() as u32).to_le_bytes());
            for p in parts {
                match p {
                    Part::Src(o, n) => {
                        b.push(0);
                        b.extend_from_slice(&o.to_le_bytes());
                        b.extend_from_slice(&n.to_le_bytes());
                    }
                    Part::Data(i, o, n) => {
                        b.push(1);
                        b.extend_from_slice(&i.to_le_bytes());
                        b.extend_from_slice(&o.to_le_bytes());
                        b.extend_from_slice(&n.to_le_bytes());
                    }
                    Part::Lit(x) => {
                        b.push(2);
                        s(&mut b, x);
                    }
                    Part::Obj(k, o, n) => {
                        b.push(3);
                        s(&mut b, k.as_bytes());
                        b.extend_from_slice(&o.to_le_bytes());
                        b.extend_from_slice(&n.to_le_bytes());
                    }
                }
            }
        }
        b
    }

    /// A 64-bit digest of the output (entries, references, data sources, summary), for
    /// comparing builds.
    pub fn digest(&self) -> u64 {
        let mut h: u64 = 0xcbf29ce484222325;
        for x in self.encode() {
            h ^= x as u64;
            h = h.wrapping_mul(0x100000001b3);
        }
        h
    }
}

// ---- reference sizes (§1.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>)

fn varint_size(v: u64) -> usize {
    let bits = 64 - v.leading_zeros() as usize;
    bits.div_ceil(7).max(1)
}

fn range_size(r: &Part) -> usize {
    match r {
        Part::Lit(b) => 1 + varint_size(b.len() as u64) + b.len(),
        Part::Src(o, n) => [0, *o, *n].iter().filter(|&&v| v != 0).map(|&v| 1 + varint_size(v)).sum(),
        Part::Data(s, o, n) => [*s as u64, *o, *n].iter().filter(|&&v| v != 0).map(|&v| 1 + varint_size(v)).sum(),
        // the host numbers the objects' sources: at most the objects a store IR has
        Part::Obj(_, o, n) => [crate::ir::MAX_OBJECTS, *o, *n].iter().filter(|&&v| v != 0).map(|&v| 1 + varint_size(v)).sum(),
    }
}

/// The encoded size of a reference to `ranges`.
pub fn payload_size(ranges: &[Part]) -> usize {
    if ranges.len() == 1 {
        return range_size(&ranges[0]);
    }
    ranges.iter().map(|r| {
        let n = range_size(r);
        1 + varint_size(n as u64) + n
    }).sum()
}

// ---- documents (conventions §2
// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#2-attributes>–§5
// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#5-units>)

pub const CONVENTION_KEY: &str = "refkv_zarrified";

// The registry's entries, by name (build.rs)
include!(concat!(env!("OUT_DIR"), "/registry.rs"));

/// The profile registry, spec/zarrify/profiles/<name>.json (the crate's profiles/ holds its
/// copies, which spec/tools/profiles.py writes and checks): one entry per profile and dataset
/// layout, with its `name`, convention `title`, `uuid` and `version`, `kind` ("file", "store",
/// "both" or "layout"), `sniff` precedence, fixture `suffixes` and the rest the registry
/// documents; by name. Parsed once.
pub fn registry() -> &'static [J] {
    static REGISTRY: std::sync::OnceLock<Vec<J>> = std::sync::OnceLock::new();
    REGISTRY.get_or_init(|| REGISTRY_TEXT.iter().map(|t| serde_json::from_str(t).expect("a profile of profiles/")).collect())
}

/// (uuid, version, title) of a profile's convention, from the registry.
pub fn profile(p: &str) -> (&'static str, u64, &'static str) {
    let e = registry().iter().find(|e| e["name"] == p).unwrap_or_else(|| panic!("no profile {p}"));
    (e["uuid"].as_str().unwrap(), e["version"].as_u64().unwrap(), e["title"].as_str().unwrap())
}

pub fn convention(p: &str) -> J {
    let (uuid, version, title) = profile(p);
    let (r, blob) = if version == 0 {
        ("heads/main".to_string(), "main".to_string())
    } else {
        let t = format!("tags/zarrify-{p}-v{version}");
        (t.clone(), t)
    };
    json!({
        "uuid": uuid,
        "schema_url": format!("https://raw.githubusercontent.com/d-v-b/refkv/refs/{r}/spec/zarrify/{p}/schema.json"),
        "spec_url": format!("https://github.com/d-v-b/refkv/blob/{blob}/spec/zarrify/{p}.md"),
        "name": CONVENTION_KEY,
        "description": format!("The Zarr layout of a {title} source zarrified by refkv, and the source's metadata"),
    })
}

pub fn root_property(p: &str, url: &str) -> Map<String, J> {
    let version = profile(p).1;
    let mut m = Map::new();
    m.insert("profile".into(), json!(p));
    m.insert("version".into(), json!(version));
    m.insert("source".into(), json!({"url": url}));
    m
}

/// A node's attributes: `attributes`, and the convention when the node is the root
/// (`url` given) or has source-specific metadata (`own` a nonempty object).
pub fn declare(attributes: Map<String, J>, p: &str, url: Option<&str>, own: Option<J>) -> Map<String, J> {
    let mut value = match url {
        Some(u) => root_property(p, u),
        None => Map::new(),
    };
    if let Some(o) = own {
        if o.as_object().is_some_and(|m| !m.is_empty()) {
            value.insert(p.to_string(), o);
        }
    }
    if value.is_empty() {
        return attributes;
    }
    let mut a = attributes;
    a.insert("zarr_conventions".into(), json!([convention(p)]));
    a.insert(CONVENTION_KEY.into(), J::Object(value));
    a
}

pub fn group_doc(attributes: Map<String, J>) -> J {
    json!({"zarr_format": 3, "node_type": "group", "attributes": attributes})
}

/// A group's zarr.json whose attributes are `attributes` and the convention of profile `p`
/// (`declare`).
pub fn declared_group(attributes: Map<String, J>, p: &str, url: Option<&str>, own: Option<J>) -> J {
    group_doc(declare(attributes, p, url, own))
}

/// An array's zarr.json `doc` declaring the convention of profile `p` with its own metadata `own`.
pub fn declare_array(doc: &mut J, p: &str, own: J) {
    doc["attributes"] = J::Object(declare(Map::new(), p, None, Some(own)));
}

/// The root and `OME/zarr.json` of a bioformats2raw layout (conventions §4
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#4-images>) of the images 0 to
/// `images` - 1: the root declares profile `p` for the source `url` with `own`, and, with
/// `layout`, names the layout, and `OME/zarr.json` lists the images; without, the root alone.
pub fn bioformats2raw(out: &mut Out, p: &str, url: &str, own: J, images: usize, layout: bool) {
    let mut root = Map::new();
    if layout {
        root.insert("ome".into(), json!({"version": "0.5", "bioformats2raw.layout": 3}));
    }
    out.json("zarr.json", &declared_group(root, p, Some(url), Some(own)));
    if layout {
        let series: Vec<String> = (0..images).map(|k| k.to_string()).collect();
        out.json("OME/zarr.json", &group_json(json!({"version": "0.5", "series": series})));
    }
}

pub fn group_json(ome: J) -> J {
    let mut a = Map::new();
    a.insert("ome".into(), ome);
    group_doc(a)
}

pub fn root_json(ome: J, p: &str, url: &str, own: Option<J>) -> J {
    let mut a = Map::new();
    a.insert("ome".into(), ome);
    group_doc(declare(a, p, Some(url), own))
}

/// An array's zarr.json (conventions §3
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#3-arrays>).
pub fn array_json(shape: &[u64], data_type: &str, chunk_shape: J, codecs: J, axes: &[&str]) -> J {
    json!({
        "zarr_format": 3,
        "node_type": "array",
        "shape": shape,
        "data_type": data_type,
        "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": chunk_shape}},
        "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
        "fill_value": 0,
        "codecs": codecs,
        "dimension_names": axes,
        "attributes": {},
    })
}

/// An array's zarr.json whose chunk grid is regular when every one of `lengths` (one per axis)
/// is one integer, and the zarr-extensions `rectilinear` grid otherwise (inline, `lengths` the
/// chunk shapes along each axis; conventions §3
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#3-arrays>); the fill value is 0,
/// or `[0, 0]` for complex64.
pub fn rectilinear_array_json(shape: &[u64], data_type: &str, lengths: &J, codecs: J, axes: &[&str]) -> J {
    let regular = lengths.as_array().is_some_and(|l| l.iter().all(|v| v.is_i64() || v.is_u64()));
    let mut doc = array_json(shape, data_type, if regular { lengths.clone() } else { json!([]) }, codecs, axes);
    if !regular {
        doc["chunk_grid"] = json!({"name": "rectilinear", "configuration": {"kind": "inline", "chunk_shapes": lengths}});
    }
    doc["fill_value"] = if data_type == "complex64" { json!([0.0, 0.0]) } else { json!(0) };
    doc
}

/// The `bytes` codec of elements of `item` bytes: with the byte order `endian` ("little" or
/// "big") for more than one byte, and without one for a single byte (Zarr v3).
pub fn bytes_codec(item: u64, endian: &str) -> J {
    if item > 1 {
        json!({"name": "bytes", "configuration": {"endian": endian}})
    } else {
        json!({"name": "bytes"})
    }
}

/// The bytes of an element of a numeric data type, from the bits its name ends with (`uint8`
/// 1, `int16` 2, `complex64` 8); 0 for a name without them.
pub fn item_size(data_type: &str) -> u64 {
    let bits = data_type.trim_start_matches(|c: char| !c.is_ascii_digit());
    bits.parse::<u64>().map_or(0, |b| b.div_ceil(8))
}

/// The codecs after `transpose` of a TIFF `compression` (the tag's value: JPEG 2000 and JPEG
/// as their imagecodecs codecs; else `bytes`, with the byte order for samples of more than 8
/// bits, then `imagecodecs_lzw` for LZW (which the QPTIFF convention reads,
/// spec/zarrify/qptiff.md §4.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/qptiff.md#41-data-types-and-codecs>),
/// `zlib` for Deflate or `zstd` for Zstandard).
pub fn tiff_codecs(compression: u64, bits: u64, little: bool) -> Vec<J> {
    match compression {
        33003 | 33004 | 33005 | 34712 => return vec![json!({"name": "imagecodecs_jpeg2k"})],
        7 => return vec![json!({"name": "imagecodecs_jpeg"})],
        _ => {}
    }
    let mut out = vec![bytes_codec(bits.div_ceil(8), if little { "little" } else { "big" })];
    match compression {
        5 => out.push(json!({"name": "imagecodecs_lzw"})),
        8 | 32946 => out.push(json!({"name": "zlib", "configuration": {"level": 1}})),
        50000 => out.push(json!({"name": "zstd", "configuration": {"level": 0, "checksum": false}})),
        _ => {}
    }
    out
}

/// The letters and colors of a pixel's red, green, blue and alpha samples, in that order and
/// in the order of BGR(A) data.
pub const RGBA: [(&str, &str); 4] = [("R", "FF0000"), ("G", "00FF00"), ("B", "0000FF"), ("A", "FFFFFF")];
pub const BGRA: [(&str, &str); 4] = [("B", "0000FF"), ("G", "00FF00"), ("R", "FF0000"), ("A", "FFFFFF")];

/// A channel's display window of `omero` (conventions §4
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#4-images>): the data's range
/// `min`..`max`, shown from `start` to `end`.
pub fn omero_window(min: J, max: J, start: J, end: J) -> J {
    json!({"min": min, "max": max, "start": start, "end": end})
}

/// The window of the range `lo`..`hi` shown whole.
pub fn full_window(lo: J, hi: J) -> J {
    omero_window(lo.clone(), hi.clone(), lo, hi)
}

/// An `omero` channel: its label, its color (`RRGGBB`), active, and its window when it has one.
pub fn omero_channel(label: &str, color: impl Into<J>, window: Option<&J>) -> J {
    let mut m = Map::new();
    m.insert("label".into(), json!(label));
    m.insert("color".into(), color.into());
    m.insert("active".into(), json!(true));
    if let Some(w) = window {
        m.insert("window".into(), w.clone());
    }
    J::Object(m)
}

/// The `omero` object of an image: its channels.
pub fn omero_json(channels: Vec<J>) -> J {
    json!({"channels": channels})
}

/// The `omero` window of a channel of an integer data type (`uint<n>`, `int<n>`) whose file
/// records no display range and whose data is not sampled: the type's whole range, shown whole;
/// none for any other type, which has no such range.
pub fn type_window(data_type: &str) -> Option<J> {
    let (lo, hi) = match data_type {
        "uint8" => (json!(0), json!(u8::MAX)),
        "uint16" => (json!(0), json!(u16::MAX)),
        "uint32" => (json!(0), json!(u32::MAX)),
        "uint64" => (json!(0), json!(u64::MAX)),
        "int8" => (json!(i8::MIN), json!(i8::MAX)),
        "int16" => (json!(i16::MIN), json!(i16::MAX)),
        "int32" => (json!(i32::MIN), json!(i32::MAX)),
        "int64" => (json!(i64::MIN), json!(i64::MAX)),
        _ => return None,
    };
    Some(full_window(lo, hi))
}

/// The `omero` channels of an image whose channels are a pixel's `spp` samples: with `rgb`, the
/// first three `R`, `G` and `B`, colored so, and any other `S<s>`, white; each label after
/// `base`, sample `s` with `windows[s]` when there is one.
pub fn sample_channels(base: &str, spp: u64, rgb: bool, windows: &[Option<J>]) -> Vec<J> {
    let letters: &[(&str, &str)] = if rgb { &RGBA[..3] } else { &[] };
    (0..spp)
        .map(|s| {
            let window = windows.get(s as usize).and_then(Option::as_ref);
            match letters.get(s as usize) {
                Some((l, c)) => omero_channel(&format!("{base}{l}"), *c, window),
                None => omero_channel(&format!("{base}S{s}"), "FFFFFF", window),
            }
        })
        .collect()
}

/// The transpose codec for frames that hold the channel axis last.
pub fn transpose_codec(axes: &[&str]) -> J {
    let stored: Vec<&str> = axes.iter().copied().filter(|a| *a != "c").chain(["c"]).collect();
    let order: Vec<usize> = stored.iter().map(|a| axes.iter().position(|b| b == a).unwrap()).collect();
    json!({"name": "transpose", "configuration": {"order": order}})
}

fn axis_type(a: &str) -> &'static str {
    match a {
        "t" => "time",
        "c" => "channel",
        _ => "space",
    }
}

/// The OME-NGFF 0.5 object of an image, with a translation per level after its scale
/// when `translations` is given; a translation that is not finite rejects.
pub fn image_ome(axes: &[&str], units: &Map<String, J>, scales: &[J], name: Option<&J>, translations: Option<&[J]>) -> Result<J, String> {
    for t in translations.unwrap_or(&[]) {
        if t.as_array().is_some_and(|v| v.iter().any(|x| x.as_f64().is_some_and(|f| !f.is_finite()) || x.is_null())) {
            return Err("a translation is not finite".into());
        }
    }
    let mut ms = Map::new();
    if let Some(n) = name {
        if !n.is_null() {
            ms.insert("name".into(), n.clone());
        }
    }
    let ax: Vec<J> = axes
        .iter()
        .map(|a| {
            let mut m = Map::new();
            m.insert("name".into(), json!(a));
            m.insert("type".into(), json!(axis_type(a)));
            if let Some(u) = units.get(*a) {
                if u.as_str().is_some_and(|s| !s.is_empty()) {
                    m.insert("unit".into(), u.clone());
                }
            }
            J::Object(m)
        })
        .collect();
    ms.insert("axes".into(), J::Array(ax));
    let ds: Vec<J> = scales
        .iter()
        .enumerate()
        .map(|(i, s)| {
            let mut t = vec![json!({"type": "scale", "scale": s})];
            if let Some(tr) = translations {
                t.push(json!({"type": "translation", "translation": tr[i]}));
            }
            json!({"path": i.to_string(), "coordinateTransformations": t})
        })
        .collect();
    ms.insert("datasets".into(), J::Array(ds));
    Ok(json!({"version": "0.5", "multiscales": [J::Object(ms)]}))
}

// ---- metadata arrays (conventions §7
// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#7-source-values-as-arrays>)

/// A chunk of a metadata array: the ranges that hold it, or its bytes when copied.
pub enum Chunk {
    Ranges(Vec<Part>),
    Bytes(Vec<u8>),
    /// copied from the source: (offset, length), then that many zero bytes of padding
    Copy(u64, u64, u64),
}

pub fn metadata_group(out: &mut Out, path: &str, attributes: Option<Map<String, J>>) {
    out.json(&format!("{path}/zarr.json"), &group_doc(attributes.unwrap_or_default()));
}

/// An array of the source metadata node: `dims` None gives no dimension names.
#[allow(clippy::too_many_arguments)]
pub fn metadata_array(out: &mut Out, path: &str, data_type: &str, shape: &[u64], chunk_shape: &[u64], dims: Option<&[&str]>,
                      chunks: Vec<(Vec<u64>, Chunk)>, endian: &str, compressor: Option<J>) {
    let mut codecs = vec![bytes_codec(item_size(data_type), endian)];
    if let Some(c) = compressor {
        codecs.push(c);
    }
    let mut doc = array_json(shape, data_type, json!(chunk_shape), J::Array(codecs), dims.unwrap_or(&[]));
    if dims.is_none() {
        doc.as_object_mut().unwrap().remove("dimension_names");
    }
    out.json(&format!("{path}/zarr.json"), &doc);
    for (coords, c) in chunks {
        let key = std::iter::once(format!("{path}/c")).chain(coords.iter().map(|x| x.to_string())).collect::<Vec<_>>().join("/");
        match c {
            Chunk::Bytes(b) => out.bytes(&key, b),
            Chunk::Ranges(r) => out.refs(&key, r),
            Chunk::Copy(..) => unreachable!("a metadata array's chunks are ranges or bytes"),
        }
    }
}

/// The gzip level of the chunks a projection writes deflated (conventions §9
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#9-dataset-layouts>).
pub const GZIP_LEVEL: u8 = 6;

/// `raw` as a gzip member (RFC 1952): a header with no name, time or extra field, the
/// deflate stream at `GZIP_LEVEL`, the CRC-32 and the length.
pub fn gzip(raw: &[u8]) -> Vec<u8> {
    let mut out = vec![0x1f, 0x8b, 8, 0, 0, 0, 0, 0, 0, 255];
    out.extend(miniz_oxide::deflate::compress_to_vec(raw, GZIP_LEVEL));
    out.extend_from_slice(&crate::safe::zip::crc32(raw).to_le_bytes());
    out.extend_from_slice(&(raw.len() as u32).to_le_bytes());
    out
}

/// A 1-D array of `n` (≥ 1) values the archive holds whole (MRC's coordinates, MP4's times;
/// conventions §9 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#9-dataset-layouts>):
/// its zarr.json at `path`, with the dimension `dim`, `attributes`, and the codecs `filters`,
/// then `bytes` (little-endian) and gzip; in chunks of `c` = min(n, `limit`) values, the q-th
/// `raw(q, c)` (its values, padded to `c` by the caller) as gzip.
#[allow(clippy::too_many_arguments)]
pub fn gzip_array(out: &mut Out, path: &str, data_type: &str, n: u64, limit: u64, dim: &str, filters: Vec<J>, attributes: J,
                  raw: impl Fn(u64, u64) -> Vec<u8>) {
    let c = n.min(limit);
    let mut codecs = filters;
    codecs.push(bytes_codec(item_size(data_type), "little"));
    codecs.push(json!({"name": "gzip", "configuration": {"level": GZIP_LEVEL}}));
    let mut doc = array_json(&[n], data_type, json!([c]), J::Array(codecs), &[dim]);
    doc["attributes"] = attributes;
    out.json(&format!("{path}/zarr.json"), &doc);
    for q in 0..n.div_ceil(c) {
        out.bytes(&format!("{path}/c/{q}"), gzip(&raw(q, c)));
    }
}

/// `v` as a protobuf varint: 7 bits a byte, least significant first, the high bit set on every
/// byte but the last.
pub fn varint(out: &mut Vec<u8>, mut v: u64) {
    while v >= 0x80 {
        out.push((v as u8) | 0x80);
        v >>= 7;
    }
    out.push(v as u8);
}

/// A `refkv.stream` chunk (conventions §10
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#10-coded-streams>) of the coded
/// frames `units`, in decode order, each (offset, length, position): the literal header (the
/// count, then each frame's length and its position less the previous frame's, the first's less
/// -1, as a varint and a zigzag varint), then the frames as ranges of source 0, a frame that
/// starts where the one before it ends extending its range.
pub fn stream_parts(units: &[(u64, u64, i64)]) -> Vec<Part> {
    let mut head = Vec::with_capacity(2 + 4 * units.len());
    varint(&mut head, units.len() as u64);
    let mut prev = -1i64;
    for &(_, n, position) in units {
        let d = position - prev;
        varint(&mut head, n);
        varint(&mut head, ((d << 1) ^ (d >> 63)) as u64);
        prev = position;
    }
    let mut parts = vec![Part::Lit(head)];
    for &(o, n, _) in units {
        match parts.last_mut() {
            Some(Part::Src(po, pn)) if *po + *pn == o => *pn += n,
            _ => parts.push(Part::Src(o, n)),
        }
    }
    parts
}

pub const MAX_CHUNK: u64 = 1 << 24;
const MAX_PADDING: u64 = (1 << 16) - (1 << 10);
const SMALL_PADDING: u64 = 1 << 10;
const MAX_TOTAL_PADDING: u64 = 1 << 20;

/// A run of the source's bytes (length ≥ 1) as a 1-D uint8 array: k chunks of equal
/// size ceil(length / k), k = ceil(length / 2^24), the last padded with zero bytes.
pub fn blob_chunks(offset: u64, length: u64) -> (u64, Vec<(Vec<u64>, Chunk)>) {
    let k = length.div_ceil(MAX_CHUNK);
    let size = length.div_ceil(k);
    let mut chunks = Vec::new();
    for i in 0..k {
        let n = size.min(length - i * size);
        let mut parts = vec![Part::Src(offset + i * size, n)];
        if n < size {
            parts.push(Part::Lit(vec![0; (size - n) as usize]));
        }
        chunks.push((vec![i], Chunk::Ranges(parts)));
    }
    (size, chunks)
}

/// The C-order array of `shape`, of `item`-byte elements contiguous at `offset`, cut
/// as conventions §7
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#7-source-values-as-arrays> cuts
/// contiguous values (`common.grid_chunks`): its chunk shape and chunks, or None when an
/// element is larger than `limit` or the sizes overflow. An edge chunk whose reference
/// (its range and its zero padding) is a payload of more than `max_payload` bytes
/// (MAX_PAYLOAD, but in tests) is `Chunk::Copy`, for the caller to copy, as
/// `common.grid_chunks` copies it with its reader. Any number of chunks is cut (the
/// references' budget bounds them).
pub fn grid_chunks(offset: u64, shape: &[u64], item: u64, limit: u64, max_payload: usize) -> Option<(Vec<u64>, Vec<(Vec<u64>, Chunk)>)> {
    if item > limit {
        return None;
    }
    if shape.contains(&0) {
        return Some((shape.iter().map(|&v| v.max(1)).collect(), Vec::new()));
    }
    if shape.is_empty() {
        return Some((Vec::new(), vec![(Vec::new(), Chunk::Ranges(vec![Part::Src(offset, item)]))]));
    }
    let mut a = 0usize;
    let mut slab = item;
    for v in &shape[1..] {
        slab = slab.checked_mul(*v)?;
    }
    while slab > limit {
        a += 1;
        slab /= shape[a];
    }
    let total: u64 = shape.iter().try_fold(item, |acc, &v| acc.checked_mul(v))?;
    let (n, c) = loop {
        let n = shape[a];
        let k = n.div_ceil(limit / slab);
        let mut best: Option<(u64, u64)> = None;
        for q in k..=(2 * k).min(n) {
            let cq = n.div_ceil(q);
            let pad = (n.div_ceil(cq) * cq - n) * slab;
            if best.is_none_or(|b| pad < b.0) {
                best = Some((pad, cq));
            }
            if pad <= SMALL_PADDING {
                break;
            }
        }
        let best = best.unwrap();
        let outer: u64 = shape[..a].iter().product();
        let fits = best.0 <= MAX_PADDING && best.0 * outer <= MAX_TOTAL_PADDING.max(total / 64);
        if fits || a == shape.len() - 1 {
            break (n, if fits { best.1 } else { n.div_ceil(k) });
        }
        a += 1;
        slab /= shape[a];
    };
    let tail = vec![0u64; shape.len() - a - 1];
    let outer_count: u64 = shape[..a].iter().product();
    let mut chunks = Vec::new();
    for base in 0..outer_count {
        let mut o = Vec::with_capacity(a);
        let mut rest = base;
        for v in shape[..a].iter().rev() {
            o.push(rest % v);
            rest /= v;
        }
        o.reverse();
        for q in 0..n.div_ceil(c) {
            let m = c.min(n - q * c);
            let (start, length) = (offset + (base * n + q * c) * slab, m * slab);
            let mut chunk = Chunk::Ranges(vec![Part::Src(start, length)]);
            if m < c {
                let pad = (c - m) * slab;
                let parts = vec![Part::Src(start, length), Part::Lit(vec![0; pad as usize])];
                chunk = if payload_size(&parts) > max_payload { Chunk::Copy(start, length, pad) } else { Chunk::Ranges(parts) };
            }
            let mut coords = o.clone();
            coords.push(q);
            coords.extend_from_slice(&tail);
            chunks.push((coords, chunk));
        }
    }
    let mut cs = vec![1u64; a];
    cs.push(c);
    cs.extend_from_slice(&shape[a + 1..]);
    Some((cs, chunks))
}

// ---- JSON sizes, as ECMAScript's JSON.stringify writes values (spec/zarrify/nd2.md §5.1
// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nd2.md#51-decoded-chunks>,
// conventions §8.7 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#87-the-view>)

/// The length of ECMAScript's Number::toString of a finite binary64.
pub fn js_number_size(x: f64) -> usize {
    if x == 0.0 {
        return 1;
    }
    let sign = usize::from(x < 0.0);
    // the shortest digits that round-trip, as d.ddde±x
    let s = format!("{:e}", x.abs());
    let (mantissa, exp) = s.split_once('e').unwrap();
    let k = mantissa.bytes().filter(u8::is_ascii_digit).count();
    let n = exp.parse::<i64>().unwrap() + 1;
    let k_i = k as i64;
    sign + if k_i <= n && n <= 21 {
        n as usize
    } else if 0 < n && n <= 21 {
        k + 1
    } else if -6 < n && n <= 0 {
        2 + (-n) as usize + k
    } else {
        let e = n - 1;
        1 + if k > 1 { k } else { 0 } + 2 + e.unsigned_abs().to_string().len()
    }
}

pub fn js_string_size(s: &str) -> usize {
    2 + s
        .chars()
        .map(|c| match c {
            '"' | '\\' | '\u{8}' | '\u{c}' | '\n' | '\r' | '\t' => 2,
            c if (c as u32) < 0x20 => 6,
            c => c.len_utf8(),
        })
        .sum::<usize>()
}

/// The length in UTF-8 bytes of `v` as JSON.stringify writes it, without whitespace.
pub fn json_size(v: &J) -> usize {
    match v {
        J::Null | J::Bool(true) => 4,
        J::Bool(false) => 5,
        J::Number(x) => match x.as_f64() {
            Some(f) if x.is_f64() => js_number_size(f),
            _ => x.to_string().len(),
        },
        J::String(s) => js_string_size(s),
        J::Array(a) => 2 + a.iter().map(json_size).sum::<usize>() + a.len().saturating_sub(1),
        J::Object(o) => 2 + o.iter().map(|(k, x)| js_string_size(k) + 1 + json_size(x)).sum::<usize>() + o.len().saturating_sub(1),
    }
}


#[cfg(test)]
mod tests {
    use super::*;

    /// Three 1-byte elements, at most 2 a chunk: the edge chunk is its byte and one byte
    /// of padding, a reference of 11 bytes, copied past a payload of 4.
    /// Three frames, the first a lead-in and the last two consecutive: the header, then two
    /// ranges.
    #[test]
    fn a_stream_chunk_is_its_header_and_the_frames_ranges() {
        // the positions' steps -1 - -1 = 0, 0 - -1 = 1, 576 - 0 = 576: zigzag 0, 2, 1152
        let parts = stream_parts(&[(10, 4, -1), (20, 5, 0), (25, 300, 576)]);
        let head = vec![3, 4, 0, 5, 2, 0xAC, 0x02, 0x80, 0x09];
        assert_eq!(parts, vec![Part::Lit(head), Part::Src(10, 4), Part::Src(20, 305)]);
    }

    #[test]
    fn the_bytes_codec_has_a_byte_order_past_one_byte() {
        assert_eq!(bytes_codec(1, "big"), json!({"name": "bytes"}));
        assert_eq!(bytes_codec(2, "big"), json!({"name": "bytes", "configuration": {"endian": "big"}}));
        let sizes: Vec<u64> = ["uint8", "int8", "int16", "uint32", "float32", "int64", "float64", "complex64", "bool"].iter().map(|t| item_size(t)).collect();
        assert_eq!(sizes, vec![1, 1, 2, 4, 4, 8, 8, 8, 0]);
    }

    #[test]
    fn grid_chunks_copies_an_edge_chunk_past_the_payload() {
        let (shape, chunks) = grid_chunks(0, &[3], 1, 2, 4).unwrap();
        assert_eq!(shape, vec![2]);
        assert!(matches!(chunks[1], (ref c, Chunk::Copy(2, 1, 1)) if *c == vec![1]));
        let (_, chunks) = grid_chunks(0, &[3], 1, 2, MAX_PAYLOAD).unwrap();
        assert!(matches!(&chunks[1].1, Chunk::Ranges(p) if *p == vec![Part::Src(2, 1), Part::Lit(vec![0])]));
    }
}
