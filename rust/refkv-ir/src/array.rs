//! Arrays as the source stores them, at the byte level (spec/stored-array.md
//! <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md>): each array's description
//! ([`StoredArray`]), the table of codec chains the descriptions share, and the chunk map, a row
//! for each unit the source holds ([`ChunkMap`]), held as plain columns and made an Arrow record
//! batch only on export (the feature `arrow`, which only the Python bindings enable).
//!
//! A parser builds them from its finished IR ([`Ir::describe`]); afterwards only a graft
//! ([`Stored::graft`]) and an unfold ([`Stored::unfolded`]) change the ids they hold. [`check`]
//! tests what a projection computes on, in every `check::finish` (spec/stored-array.md §5
//! <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#5-checks>).

use crate::ir::*;
use serde_json::{Map, Value as J, json};
use std::collections::{BTreeMap, HashMap, HashSet};

/// The chunk map's schema version, in its Arrow schema's metadata under `refkv.chunk_map`.
pub const CHUNK_MAP_VERSION: &str = "0";

/// An array as the source stores it, at the byte level (spec/stored-array.md §2
/// <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#2-the-description>).
#[derive(Clone, Debug, PartialEq)]
pub struct StoredArray {
    /// the IR element that declares the array
    pub element: u32,
    /// the array's index among those `element` declares, for a kind of element that may declare
    /// several
    pub index: Option<u64>,
    /// where the source declares what the array's elements are, as the format's document lists
    /// them
    pub declarations: Vec<Place>,
    /// the axes in storage order, the slowest-varying first; None where the source declares no
    /// extent (an HDF5 null dataspace), which holds no element, unlike a scalar's `Some([])`
    pub shape: Option<Vec<u64>>,
    /// the source's own order of the axes, where it differs from storage order (never the
    /// identity): `axes[i]` is storage axis i's position in the source's list
    pub axes: Option<Vec<u32>>,
    /// the bits of one element, decoded; None only for an array with no grid and no given fill
    pub item_bits: Option<u64>,
    /// for elements that are not whole bytes: the first axis from which the elements of a decoded
    /// unit pack unbroken, each run over this axis and those after it ending on a byte boundary;
    /// None: the whole unit packs unbroken
    pub pad_from: Option<u32>,
    /// how the source cuts the array into units; None where the IR does not model its storage
    pub grid: Option<Grid>,
    /// the codec chains a unit may be encoded with, by position in the chain table: a unit's
    /// chain is `chains[0]` unless its row gives another position in this list
    pub chains: Vec<u32>,
    pub fill: Fill,
    /// the parameters whose values differ from unit to unit, each a column of the chunk map
    pub unit_parameters: Vec<UnitParameter>,
}

/// How the source cuts an array into units (spec/stored-array.md §2.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#23-the-grid>).
#[derive(Clone, Debug, PartialEq)]
pub struct Grid {
    pub axes: Vec<AxisUnits>,
    pub edge: Edge,
}

/// The units along one axis: one length for all, the last cut by `edge` where the length does not
/// divide the axis; or each unit's length in order, runs of equal ones kept as `Repeat`.
#[derive(Clone, Debug, PartialEq)]
pub enum AxisUnits {
    Each(u64),
    List(Vec<Run>),
}

/// Lengths of a `List` axis: one unit, or `(length, count)` units of one length.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Run {
    One(u64),
    Repeat(u64, u64),
}

/// How a unit past an `Each` axis's end is stored, as the source declares its layout.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Edge {
    /// at the full unit length, the part past the end filled; `Each(n)` is the declared length
    Full,
    /// cut to the axis; `Each(n)` is the declared length clamped to the axis's
    Short,
}

/// A codec that takes a unit's bytes toward its elements' bytes: by name (one of
/// [`CODECS`]), and its parameters.
#[derive(Clone, Debug, PartialEq)]
pub struct Codec {
    pub name: String,
    pub parameters: BTreeMap<String, Param>,
}

/// A chain: every step the source encodes a unit with, in encoding order.
pub type Chain = Vec<Codec>;

/// A parameter: a value, or bytes of the source that the codec makes it from.
#[derive(Clone, Debug, PartialEq)]
pub enum Param {
    Value(J),
    Source(Place),
}

/// Bytes of the source (spec/stored-array.md §2.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#22-places-and-parameters>): an
/// IR element (with `member`, for an element in a run's subtree, the position j of the run's
/// member it is in), or `range`, an (offset, length) within the element's bytes (its extent);
/// or, where no element holds them, a range of an object by its key (None: the parsed source
/// itself), the offset counted from the object's start.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum Place {
    Element { element: u32, member: Option<u64>, range: Option<(u64, u64)> },
    Extent { object: Option<String>, offset: u64, length: u64 },
}

/// The source's fill value (spec/stored-array.md §2.4
/// <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#24-the-fill-value>).
#[derive(Clone, Debug, PartialEq)]
pub enum Fill {
    /// the source defines none, or defines one the IR cannot express, whose place is then among
    /// the description's `declarations` (the general fallback): a profile tells the two apart
    /// through the format's list of declaration places
    Undefined,
    /// the source says its format's default
    FormatDefault,
    /// one decoded element's bytes, ceil(item_bits / 8) of them: a base64 string, or the place
    /// that holds them
    Given(Param),
}

/// A parameter named `name` of codec `codec` (its position in the chain) of the array's chain
/// `chain` (its position in the array's `chains`), whose value each unit of that chain gives in
/// the chunk map's column `p.<codec name>.<name>`.
#[derive(Clone, Debug, PartialEq)]
pub struct UnitParameter {
    pub chain: u32,
    pub codec: u32,
    pub name: String,
}

/// The Arrow type of a parameter column, as the codec's entry gives it.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum ColumnType {
    UInt64,
    Int64,
    Float64,
}

/// One parameter of a codec's entry: its name, and its column's type where a unit may give it.
pub struct ParamEntry {
    pub name: &'static str,
    pub column: Option<ColumnType>,
}

/// An entry of the list of the IR's codecs (spec/stored-array.md §3.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#32-the-list>): its name and its
/// parameters. What its decoding makes, and how a parameter given as a `Source` is made from its
/// bytes, are the list's prose.
pub struct CodecEntry {
    pub name: &'static str,
    pub parameters: &'static [ParamEntry],
}

/// A parameter no unit gives as a column.
const fn fixed(name: &'static str) -> ParamEntry {
    ParamEntry { name, column: None }
}

/// The IR's codecs. `source` is the general fallback: a source encoding refkv has no name for,
/// by the format, the source's id for it, the source's own name for it where it gives one (an
/// HDF5 filter's optional name), and its parameters as the source gives them.
pub const CODECS: &[CodecEntry] = &[
    CodecEntry { name: "source", parameters: &[fixed("format"), fixed("id"), fixed("name"), fixed("parameters")] },
    CodecEntry { name: "zlib", parameters: &[] },
    CodecEntry { name: "gzip", parameters: &[] },
    CodecEntry { name: "zstd", parameters: &[] },
    CodecEntry { name: "bzip2", parameters: &[] },
    CodecEntry { name: "lzf", parameters: &[] },
    CodecEntry { name: "blosc", parameters: &[] },
    CodecEntry { name: "skip", parameters: &[fixed("length")] },
    CodecEntry { name: "shuffle", parameters: &[fixed("element_size")] },
    CodecEntry { name: "lsb_first", parameters: &[fixed("bits")] },
    CodecEntry { name: "h5_fletcher32", parameters: &[] },
    CodecEntry { name: "h5_lz4", parameters: &[] },
    CodecEntry { name: "h5_szip", parameters: &[fixed("options"), fixed("pixels_per_block"), fixed("bits_per_pixel"), fixed("pixels_per_scanline")] },
    CodecEntry { name: "h5_nbit", parameters: &[fixed("parameters")] },
    CodecEntry { name: "h5_scaleoffset", parameters: &[fixed("parameters")] },
    CodecEntry { name: "h5_bitshuffle", parameters: &[fixed("element_size"), fixed("block_size"), fixed("compression")] },
    CodecEntry { name: "xz", parameters: &[] },
    CodecEntry { name: "tiff_lzw", parameters: &[] },
    CodecEntry { name: "packbits", parameters: &[] },
    CodecEntry { name: "tiff_predictor", parameters: &[fixed("predictor"), fixed("planar_configuration"), fixed("byte_order")] },
    CodecEntry { name: "ccitt_t6", parameters: &[] },
    CodecEntry { name: "jpegls", parameters: &[] },
    CodecEntry { name: "dicom_rle", parameters: &[] },
    CodecEntry { name: "jpeg", parameters: &[fixed("tables")] },
    CodecEntry { name: "jpeg_restart_interval", parameters: &[fixed("header")] },
    CodecEntry { name: "jpeg2k", parameters: &[] },
    CodecEntry { name: "jpegxl", parameters: &[] },
    CodecEntry { name: "jpegxr", parameters: &[] },
    CodecEntry { name: "webp", parameters: &[] },
    CodecEntry { name: "lerc", parameters: &[] },
    CodecEntry { name: "eer", parameters: &[fixed("skip_bits"), fixed("horizontal_bits"), fixed("vertical_bits")] },
    CodecEntry { name: "h264", parameters: PICTURE },
    CodecEntry { name: "hevc", parameters: PICTURE },
    CodecEntry { name: "av1", parameters: PICTURE },
    CodecEntry { name: "vp9", parameters: PICTURE },
    CodecEntry { name: "fits_rice", parameters: &[fixed("block_size"), fixed("byte_pix")] },
    CodecEntry { name: "fits_quantize", parameters: FITS_QUANTIZE },
];

/// The parameters of FITS's quantization of floats: its dither's name, and for each tile, as a
/// column, its scale and zero, where it enters the dither's random numbers, and its blank value
/// (which a header gives once instead).
const FITS_QUANTIZE: &[ParamEntry] = &[
    fixed("method"),
    ParamEntry { name: "scale", column: Some(ColumnType::Float64) },
    ParamEntry { name: "zero", column: Some(ColumnType::Float64) },
    ParamEntry { name: "seed", column: Some(ColumnType::Int64) },
    ParamEntry { name: "zblank", column: Some(ColumnType::Int64) },
];

/// The parameters of a codec of a video stream's pictures: its configuration, the plane of a
/// picture a unit decodes to, and, a column, whether a decoder may start at the unit.
const PICTURE: &[ParamEntry] = &[fixed("setup"), fixed("plane"), ParamEntry { name: "sync", column: Some(ColumnType::UInt64) }];

/// A value of a parameter column.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Scalar {
    UInt64(u64),
    Int64(i64),
    Float64(f64),
}

/// A parameter column of the chunk map: a value or null for each row.
#[derive(Clone, Debug, PartialEq)]
pub enum Column {
    UInt64(Vec<Option<u64>>),
    Int64(Vec<Option<i64>>),
    Float64(Vec<Option<f64>>),
}

impl Column {
    fn of(t: ColumnType, n: usize) -> Column {
        match t {
            ColumnType::UInt64 => Column::UInt64(vec![None; n]),
            ColumnType::Int64 => Column::Int64(vec![None; n]),
            ColumnType::Float64 => Column::Float64(vec![None; n]),
        }
    }

    pub fn kind(&self) -> ColumnType {
        match self {
            Column::UInt64(_) => ColumnType::UInt64,
            Column::Int64(_) => ColumnType::Int64,
            Column::Float64(_) => ColumnType::Float64,
        }
    }

    pub fn len(&self) -> usize {
        match self {
            Column::UInt64(v) => v.len(),
            Column::Int64(v) => v.len(),
            Column::Float64(v) => v.len(),
        }
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    pub fn get(&self, k: usize) -> Option<Scalar> {
        match self {
            Column::UInt64(v) => v[k].map(Scalar::UInt64),
            Column::Int64(v) => v[k].map(Scalar::Int64),
            Column::Float64(v) => v[k].map(Scalar::Float64),
        }
    }

    /// A value, or null (None); a value of another type is an error.
    fn push(&mut self, x: Option<Scalar>) -> Res<()> {
        match (self, x) {
            (Column::UInt64(v), Some(Scalar::UInt64(a))) => v.push(Some(a)),
            (Column::Int64(v), Some(Scalar::Int64(a))) => v.push(Some(a)),
            (Column::Float64(v), Some(Scalar::Float64(a))) => v.push(Some(a)),
            (Column::UInt64(v), None) => v.push(None),
            (Column::Int64(v), None) => v.push(None),
            (Column::Float64(v), None) => v.push(None),
            _ => return Err("internal: a parameter column given a value of another type".into()),
        }
        Ok(())
    }
}

/// A unit's row, as a parser adds it to the chunk map (spec/stored-array.md §4
/// <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#4-the-chunk-map>).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Row {
    pub array: u32,
    pub coords: Vec<u64>,
    pub element: Option<u32>,
    pub member: Option<u64>,
    /// where the unit is part of `element`: its (offset, length) within the element's bytes
    pub range: Option<(u64, u64)>,
    /// its bytes, in order, as (object key, None for the parsed source; offset; length)
    pub ranges: Option<Vec<(Option<String>, u64, u64)>>,
    pub chain: Option<u32>,
    pub skips: Option<u64>,
    /// its parameter columns' values, by column name (`p.<codec name>.<name>`)
    pub params: Vec<(String, Scalar)>,
}

/// The positions of the keys of a chunk map's `objects`, beside them (not compared: it follows
/// from them).
#[derive(Clone, Debug, Default)]
pub struct ObjectIndex(HashMap<String, u32>);

impl PartialEq for ObjectIndex {
    fn eq(&self, _: &Self) -> bool {
        true
    }
}

/// The chunk map as columns: row k's coordinates are `coords[coords_end[k-1]..coords_end[k]]`,
/// and its ranges, when `has_ranges[k]`, the range columns from `ranges_end[k-1]` to
/// `ranges_end[k]`, each object an index into the dictionary `objects` (None: the parsed source).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct ChunkMap {
    pub array: Vec<u32>,
    pub coords_end: Vec<u64>,
    pub coords: Vec<u64>,
    pub element: Vec<Option<u32>>,
    pub member: Vec<Option<u64>>,
    /// where the unit is part of its element: its (offset, length) within the element's bytes
    pub range: Vec<Option<(u64, u64)>>,
    pub has_ranges: Vec<bool>,
    pub ranges_end: Vec<u64>,
    pub range_object: Vec<Option<u32>>,
    pub range_offset: Vec<u64>,
    pub range_length: Vec<u64>,
    pub objects: Vec<String>,
    /// each key's position in `objects`
    pub object_index: ObjectIndex,
    pub chain: Vec<Option<u32>>,
    pub skips: Vec<Option<u64>>,
    /// the parameter columns, by name
    pub params: BTreeMap<String, Column>,
}

impl ChunkMap {
    pub fn len(&self) -> usize {
        self.array.len()
    }

    pub fn is_empty(&self) -> bool {
        self.array.is_empty()
    }

    fn start(ends: &[u64], k: usize) -> usize {
        if k == 0 { 0 } else { ends[k - 1] as usize }
    }

    /// Row k's coordinates.
    pub fn coords_of(&self, k: usize) -> &[u64] {
        &self.coords[Self::start(&self.coords_end, k)..self.coords_end[k] as usize]
    }

    /// The positions of row k's ranges in the range columns, when it has ranges.
    pub fn ranges_of(&self, k: usize) -> Option<std::ops::Range<usize>> {
        self.has_ranges[k].then(|| Self::start(&self.ranges_end, k)..self.ranges_end[k] as usize)
    }

    /// The position of `key` in `objects`, which it joins if it is not there.
    fn object(&mut self, key: &str) -> u32 {
        if self.object_index.0.len() != self.objects.len() {
            self.object_index.0 = self.objects.iter().enumerate().map(|(k, o)| (o.clone(), k as u32)).collect();
        }
        if let Some(&k) = self.object_index.0.get(key) {
            return k;
        }
        let k = self.objects.len() as u32;
        self.objects.push(key.to_string());
        self.object_index.0.insert(key.to_string(), k);
        k
    }

    /// Adds a row (in any order: [`ChunkMap::sort`] orders them). A parameter column takes the
    /// type of its first value.
    pub fn push(&mut self, row: Row) -> Res<()> {
        let n = self.len();
        self.array.push(row.array);
        self.coords.extend_from_slice(&row.coords);
        self.coords_end.push(self.coords.len() as u64);
        self.element.push(row.element);
        self.member.push(row.member);
        self.range.push(row.range);
        self.has_ranges.push(row.ranges.is_some());
        for (o, offset, length) in row.ranges.into_iter().flatten() {
            let o = o.map(|k| self.object(&k));
            self.range_object.push(o);
            self.range_offset.push(offset);
            self.range_length.push(length);
        }
        self.ranges_end.push(self.range_offset.len() as u64);
        self.chain.push(row.chain);
        self.skips.push(row.skips);
        for (name, x) in &row.params {
            if !self.params.contains_key(name) {
                let t = match x {
                    Scalar::UInt64(_) => ColumnType::UInt64,
                    Scalar::Int64(_) => ColumnType::Int64,
                    Scalar::Float64(_) => ColumnType::Float64,
                };
                self.params.insert(name.clone(), Column::of(t, n));
            }
        }
        for (name, col) in self.params.iter_mut() {
            col.push(row.params.iter().find(|p| &p.0 == name).map(|p| p.1))?;
        }
        Ok(())
    }

    /// The map with its rows in the order `order` gives (positions of rows of this one).
    fn take(&self, order: &[usize]) -> ChunkMap {
        let mut out = ChunkMap { objects: self.objects.clone(), object_index: self.object_index.clone(), ..ChunkMap::default() };
        for &k in order {
            out.array.push(self.array[k]);
            out.coords.extend_from_slice(self.coords_of(k));
            out.coords_end.push(out.coords.len() as u64);
            out.element.push(self.element[k]);
            out.member.push(self.member[k]);
            out.range.push(self.range[k]);
            out.has_ranges.push(self.has_ranges[k]);
            if let Some(r) = self.ranges_of(k) {
                out.range_object.extend_from_slice(&self.range_object[r.clone()]);
                out.range_offset.extend_from_slice(&self.range_offset[r.clone()]);
                out.range_length.extend_from_slice(&self.range_length[r]);
            }
            out.ranges_end.push(out.range_offset.len() as u64);
            out.chain.push(self.chain[k]);
            out.skips.push(self.skips[k]);
        }
        for (name, col) in &self.params {
            let c = match col {
                Column::UInt64(v) => Column::UInt64(order.iter().map(|&k| v[k]).collect()),
                Column::Int64(v) => Column::Int64(order.iter().map(|&k| v[k]).collect()),
                Column::Float64(v) => Column::Float64(order.iter().map(|&k| v[k]).collect()),
            };
            out.params.insert(name.clone(), c);
        }
        out
    }

    /// The rows grouped by array, in order, and within an array in row-major order of their
    /// coordinates.
    pub fn sort(&mut self) {
        let sorted = (1..self.len()).all(|k| (self.array[k - 1], self.coords_of(k - 1)) <= (self.array[k], self.coords_of(k)));
        if sorted {
            return;
        }
        let mut order: Vec<usize> = (0..self.len()).collect();
        order.sort_by(|&a, &b| (self.array[a], self.coords_of(a)).cmp(&(self.array[b], self.coords_of(b))));
        *self = self.take(&order);
    }

    /// `from`'s rows after these, their arrays `base` on, their elements renumbered by `at`, the
    /// parsed source's null key `key`, and each row's chain the position `locals[array]` gives its
    /// array's chain (a graft).
    fn append(&mut self, from: &ChunkMap, base: u32, at: &dyn Fn(u32) -> u32, key: &str, locals: &[Vec<u32>], keep: &[bool], skip: &HashSet<String>) {
        let n = self.len();
        // object keys join the dictionary as the kept rows first name them
        let mut objects: Vec<Option<u32>> = vec![None; from.objects.len()];
        let mut parsed: Option<u32> = None;
        // the rows of the arrays that keep them (the others fell back, Stored::plan_graft)
        let rows: Vec<usize> = (0..from.len()).filter(|&k| keep.get(from.array[k] as usize).copied().unwrap_or(true)).collect();
        for &k in &rows {
            self.array.push(from.array[k] + base);
            self.coords.extend_from_slice(from.coords_of(k));
            self.coords_end.push(self.coords.len() as u64);
            self.element.push(from.element[k].map(at));
            self.member.push(from.member[k]);
            self.range.push(from.range[k]);
            self.has_ranges.push(from.has_ranges[k]);
            if let Some(r) = from.ranges_of(k) {
                for x in r {
                    let o = match from.range_object[x] {
                        Some(i) => match objects[i as usize] {
                            Some(o) => o,
                            None => {
                                let o = self.object(&from.objects[i as usize]);
                                objects[i as usize] = Some(o);
                                o
                            }
                        },
                        None => match parsed {
                            Some(o) => o,
                            None => {
                                let o = self.object(key);
                                parsed = Some(o);
                                o
                            }
                        },
                    };
                    self.range_object.push(Some(o));
                    self.range_offset.push(from.range_offset[x]);
                    self.range_length.push(from.range_length[x]);
                }
            }
            self.ranges_end.push(self.range_offset.len() as u64);
            let chain = from.chain[k].unwrap_or(0);
            let chain = locals.get(from.array[k] as usize).and_then(|l| l.get(chain as usize)).copied().unwrap_or(chain);
            self.chain.push((chain != 0).then_some(chain));
            self.skips.push(from.skips[k]);
        }
        for (name, col) in from.params.iter().filter(|(name, _)| !skip.contains(*name)) {
            self.params.entry(name.clone()).or_insert_with(|| Column::of(col.kind(), n));
        }
        for (name, col) in self.params.iter_mut() {
            // a column of one name is of one type (Stored::plan_graft): no push fails
            match from.params.get(name).filter(|_| !skip.contains(name)) {
                Some(c) => {
                    for &k in &rows {
                        let _ = col.push(c.get(k));
                    }
                }
                None => {
                    for _ in &rows {
                        let _ = col.push(None);
                    }
                }
            }
        }
    }

    /// The parameter columns `drop` names removed, and the object keys no row names dropped from
    /// the dictionary (after an array fell back, taking its rows).
    fn prune(&mut self, drop: &HashSet<String>) {
        self.params.retain(|name, _| !drop.contains(name));
        let mut used = vec![false; self.objects.len()];
        for &o in self.range_object.iter().flatten() {
            if let Some(u) = used.get_mut(o as usize) {
                *u = true;
            }
        }
        if used.iter().all(|&u| u) {
            return;
        }
        let mut at = vec![u32::MAX; used.len()];
        let mut kept = Vec::new();
        for (k, &u) in used.iter().enumerate() {
            if u {
                at[k] = kept.len() as u32;
                kept.push(self.objects[k].clone());
            }
        }
        for o in self.range_object.iter_mut().flatten() {
            *o = at[*o as usize];
        }
        self.objects = kept;
        self.object_index.0.clear();
    }

    /// Each row's element and member as `f` moves them.
    fn moved(&mut self, f: &dyn Fn(u32, Option<u64>) -> (u32, Option<u64>)) {
        for k in 0..self.len() {
            if let Some(e) = self.element[k] {
                let (e, m) = f(e, self.member[k]);
                self.element[k] = Some(e);
                self.member[k] = m;
            }
        }
    }

    fn memory(&self) -> usize {
        let n = self.len();
        n * (4 + 8 + 8 + 16 + 24 + 1 + 8 + 8 + 16)
            + self.coords.len() * 8
            + self.range_offset.len() * (8 + 8 + 8)
            + self.objects.iter().map(|o| o.len() + 48).sum::<usize>()
            + self.params.values().map(|c| c.len() * 16).sum::<usize>()
    }

    /// Each row's values, in a fixed column order (the named columns, then the parameter columns
    /// by name), a dictionary's keys as their strings.
    fn digest(&self, eat: &mut dyn FnMut(&[u8])) {
        let opt = |eat: &mut dyn FnMut(&[u8]), x: Option<u64>| match x {
            Some(v) => {
                eat(&[1]);
                eat(&v.to_le_bytes());
            }
            None => eat(&[0]),
        };
        for k in 0..self.len() {
            eat(&self.array[k].to_le_bytes());
            let c = self.coords_of(k);
            eat(&(c.len() as u64).to_le_bytes());
            for x in c {
                eat(&x.to_le_bytes());
            }
            opt(eat, self.element[k].map(u64::from));
            opt(eat, self.member[k]);
            match self.range[k] {
                Some((o, n)) => {
                    eat(&[1]);
                    eat(&o.to_le_bytes());
                    eat(&n.to_le_bytes());
                }
                None => eat(&[0]),
            }
            match self.ranges_of(k) {
                Some(r) => {
                    eat(&[1]);
                    eat(&(r.len() as u64).to_le_bytes());
                    for x in r {
                        match self.range_object[x] {
                            Some(o) => {
                                let s = self.objects[o as usize].as_bytes();
                                eat(&[1]);
                                eat(&(s.len() as u64).to_le_bytes());
                                eat(s);
                            }
                            None => eat(&[0]),
                        }
                        eat(&self.range_offset[x].to_le_bytes());
                        eat(&self.range_length[x].to_le_bytes());
                    }
                }
                None => eat(&[0]),
            }
            opt(eat, self.chain[k].map(u64::from));
            opt(eat, self.skips[k]);
            for (name, col) in &self.params {
                eat(&(name.len() as u64).to_le_bytes());
                eat(name.as_bytes());
                match col.get(k) {
                    Some(Scalar::UInt64(v)) => opt(eat, Some(v)),
                    Some(Scalar::Int64(v)) => opt(eat, Some(v as u64)),
                    Some(Scalar::Float64(v)) => opt(eat, Some(v.to_bits())),
                    None => opt(eat, None),
                }
            }
        }
    }
}

/// The positions of the chains of a chain table by their JSON text, beside it (not compared: it
/// follows from it).
#[derive(Clone, Debug, Default)]
pub struct ChainIndex(HashMap<String, u32>);

impl PartialEq for ChainIndex {
    fn eq(&self, _: &Self) -> bool {
        true
    }
}

/// What a graft adds to a store's stored arrays ([`Stored::plan_graft`]): the file's chains the
/// table has not (with their texts), its arrays renumbered, and each one's chains' positions among
/// its own chains in the store.
#[derive(Debug)]
pub(crate) struct GraftPlan {
    added: Vec<(Chain, String)>,
    arrays: Vec<StoredArray>,
    locals: Vec<Vec<u32>>,
    /// whether each array keeps its rows (one that would pass the map's room falls back)
    keep: Vec<bool>,
    /// the file's parameter columns only its fallen-back arrays listed, which the store does not get
    skip: HashSet<String>,
    /// the descriptions' and the chunk map's entries the graft adds
    pub(crate) weights: (u64, u64),
    /// whether the file joins the store with no descriptions: it kept none at its own describe, or
    /// even its arrays' fallen-back descriptions pass the store's descriptions' limit
    undescribed: bool,
}

impl StoredArray {
    /// The general fallback for an array whose units the map has no room for, or whose full
    /// description the descriptions' limit has none for (spec/stored-array.md §6
    /// <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#6-in-the-ir>): no grid,
    /// chains or unit parameters, and so no rows.
    fn fall_back(&mut self) {
        self.grid = None;
        self.chains.clear();
        self.unit_parameters.clear();
    }

    /// The description's entries the budget counts: itself, each axis of its shape, each run of a
    /// listed axis, each of its places, each of its chains and each unit parameter.
    fn weight(&self) -> u64 {
        let listed: usize = self.grid.as_ref().map_or(0, |g| {
            g.axes.iter().map(|x| match x {
                AxisUnits::Each(_) => 0,
                AxisUnits::List(r) => r.len(),
            }).sum()
        });
        let fill = usize::from(matches!(self.fill, Fill::Given(Param::Source(_))));
        (1 + self.shape.as_ref().map_or(0, Vec::len) + listed + self.declarations.len() + fill + self.chains.len() + self.unit_parameters.len()) as u64
    }

    /// [`StoredArray::weight`] once it has fallen back.
    fn fallen_back_weight(&self) -> u64 {
        let fill = usize::from(matches!(self.fill, Fill::Given(Param::Source(_))));
        (1 + self.shape.as_ref().map_or(0, Vec::len) + self.declarations.len() + fill) as u64
    }
}

/// Which arrays keep their full descriptions within `room` entries of the descriptions' limit
/// (spec/stored-array.md §6 <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#6-in-the-ir>),
/// or None where even every array's fallen-back description passes it: first each array is charged
/// its fallen-back cost; then, in order, each keeps its full description (its chains, unit
/// parameters and grid, and the chains of `cost` it is the first kept description to name) while
/// what remains allows, and falls back otherwise. An array that fell back already costs nothing
/// more, and stays fallen back.
fn fit_descriptions(arrays: &[StoredArray], cost: &dyn Fn(u32) -> u64, room: u64) -> Option<Vec<bool>> {
    let fallen: Vec<u64> = arrays.iter().map(StoredArray::fallen_back_weight).collect();
    let mut left = room.checked_sub(fallen.iter().sum())?;
    let mut named: HashSet<u32> = HashSet::new();
    let keep = arrays
        .iter()
        .zip(&fallen)
        .map(|(a, &f)| {
            let new: HashSet<u32> = a.chains.iter().copied().filter(|c| !named.contains(c)).collect();
            let extra = (a.weight() - f).saturating_add(new.iter().map(|&c| cost(c)).sum());
            if extra <= left {
                left -= extra;
                named.extend(new);
                true
            } else {
                false
            }
        })
        .collect();
    Some(keep)
}

/// A chain's entries the budget counts: itself, each codec and each place a codec's parameter names.
fn chain_weight(c: &Chain) -> u64 {
    (1 + c.iter().map(|x| 1 + x.parameters.values().filter(|p| matches!(p, Param::Source(_))).count()).sum::<usize>()) as u64
}

/// Each of `n` arrays' chunk map entries in `map`: its rows, their coordinates, their `ranges`
/// items and their set unit-parameter values (a row's `range` is part of it, and costs none).
fn map_weights(map: &ChunkMap, n: usize) -> Vec<u64> {
    let mut w = vec![0u64; n];
    for k in 0..map.len() {
        if let Some(x) = w.get_mut(map.array[k] as usize) {
            let set = map.params.values().filter(|c| c.get(k).is_some()).count();
            *x += 1 + map.coords_of(k).len() as u64 + map.ranges_of(k).map_or(0, |r| r.len() as u64) + set as u64;
        }
    }
    w
}

/// The parameter columns the arrays that do not `keep` their rows list, and no array that keeps
/// them lists: the columns a fallback leaves no array to give.
fn orphaned_columns(arrays: &[StoredArray], chains: &[Chain], keep: &[bool]) -> HashSet<String> {
    let listed = |kept: bool| -> HashSet<String> {
        arrays
            .iter()
            .zip(keep)
            .filter(|(a, k)| **k == kept && a.grid.is_some())
            .flat_map(|(a, _)| {
                a.unit_parameters.iter().filter_map(|u| {
                    let codec = a.chains.get(u.chain as usize).and_then(|&c| chains.get(c as usize)).and_then(|c| c.get(u.codec as usize))?;
                    Some(format!("p.{}.{}", codec.name, u.name))
                })
            })
            .collect()
    };
    let kept = listed(true);
    listed(false).into_iter().filter(|c| !kept.contains(c)).collect()
}

/// Which of the arrays keep their rows within `room` entries of the chunk map (`weights`, each
/// array's), and the entries those take: arrays are tried in their order; one whose rows do not
/// fit in what remains keeps none of them; later arrays are still tried.
fn fit(weights: &[u64], room: u64) -> (Vec<bool>, u64) {
    let mut used = 0u64;
    let keep = weights
        .iter()
        .map(|&w| match used.checked_add(w) {
            Some(u) if u <= room => {
                used = u;
                true
            }
            _ => false,
        })
        .collect();
    (keep, used)
}

/// An IR's stored arrays: the descriptions, the chain table they name and the chunk map.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Stored {
    pub arrays: Vec<StoredArray>,
    pub chains: Vec<Chain>,
    /// each chain's position in `chains`, which a graft looks the file's chains up in
    pub chain_index: ChainIndex,
    pub map: ChunkMap,
    /// a file IR's source kept no descriptions at all: even its arrays' fallen-back descriptions
    /// passed the descriptions' limit (spec/stored-array.md §6
    /// <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#6-in-the-ir>)
    pub undescribed: bool,
    /// a store IR's objects whose arrays kept no descriptions, by key: each grafted file marked
    /// `undescribed`, or whose descriptions the graft's fallback dropped whole, and each object
    /// whose arrays the store's own parser describes where its describe kept none
    pub undescribed_keys: Vec<String>,
}

impl Place {
    fn renumber(&mut self, at: &dyn Fn(u32) -> u32, key: &str) {
        match self {
            Place::Element { element, .. } => *element = at(*element),
            Place::Extent { object, .. } => {
                if object.is_none() {
                    *object = Some(key.to_string());
                }
            }
        }
    }

    fn moved(&mut self, f: &dyn Fn(u32, Option<u64>) -> (u32, Option<u64>)) {
        if let Place::Element { element, member, .. } = self {
            (*element, *member) = f(*element, *member);
        }
    }

    pub fn to_json(&self) -> J {
        match self {
            Place::Element { element, member, range } => {
                json!({"element": element, "member": member, "range": range.map(|(o, n)| [o, n])})
            }
            Place::Extent { object, offset, length } => json!({"object": object, "offset": offset, "length": length}),
        }
    }
}

impl Param {
    fn places(&mut self) -> Option<&mut Place> {
        match self {
            Param::Source(p) => Some(p),
            Param::Value(_) => None,
        }
    }

    pub fn to_json(&self) -> J {
        match self {
            Param::Value(v) => json!({"value": v}),
            Param::Source(p) => json!({"source": p.to_json()}),
        }
    }
}

impl Codec {
    pub fn to_json(&self) -> J {
        let p: Map<String, J> = self.parameters.iter().map(|(k, v)| (k.clone(), v.to_json())).collect();
        json!({"name": self.name, "parameters": p})
    }
}

fn chain_json(c: &Chain) -> J {
    J::Array(c.iter().map(Codec::to_json).collect())
}

impl Grid {
    pub fn to_json(&self) -> J {
        let axes: Vec<J> = self
            .axes
            .iter()
            .map(|a| match a {
                AxisUnits::Each(n) => json!({"each": n}),
                AxisUnits::List(runs) => {
                    let v: Vec<J> = runs.iter().map(|r| match r {
                        Run::One(n) => json!(n),
                        Run::Repeat(n, c) => json!([n, c]),
                    }).collect();
                    json!({"list": v})
                }
            })
            .collect();
        json!({"axes": axes, "edge": match self.edge { Edge::Full => "full", Edge::Short => "short" }})
    }
}

impl StoredArray {
    /// Every place the description holds: its declarations and its fill's.
    fn places(&mut self) -> Vec<&mut Place> {
        let mut v: Vec<&mut Place> = self.declarations.iter_mut().collect();
        if let Fill::Given(p) = &mut self.fill {
            v.extend(p.places());
        }
        v
    }

    /// Its ids as a graft gives them: its element and every place's by `at`, the parsed source's
    /// null key `key`.
    pub fn renumber(&mut self, at: &dyn Fn(u32) -> u32, key: &str) {
        self.element = at(self.element);
        for p in self.places() {
            p.renumber(at, key);
        }
    }

    /// Its JSON form (spec/stored-array.md §2.5
    /// <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#25-the-json-form>), without
    /// the element's path.
    pub fn to_json(&self) -> J {
        let fill = match &self.fill {
            Fill::Undefined => json!("undefined"),
            Fill::FormatDefault => json!("format_default"),
            Fill::Given(p) => json!({"given": p.to_json()}),
        };
        let ups: Vec<J> = self.unit_parameters.iter().map(|u| json!({"chain": u.chain, "codec": u.codec, "name": u.name})).collect();
        json!({
            "element": self.element,
            "index": self.index,
            "declarations": self.declarations.iter().map(Place::to_json).collect::<Vec<J>>(),
            "shape": self.shape,
            "axes": self.axes,
            "item_bits": self.item_bits,
            "pad_from": self.pad_from,
            "grid": self.grid.as_ref().map(Grid::to_json),
            "chains": self.chains,
            "fill": fill,
            "unit_parameters": ups,
        })
    }
}

impl Stored {
    /// No descriptions, chains or rows, and no mark: an IR that described nothing (a source marked
    /// `undescribed` is not empty, so that a graft carries its mark).
    pub fn is_empty(&self) -> bool {
        self.arrays.is_empty() && self.chains.is_empty() && self.map.is_empty() && !self.undescribed && self.undescribed_keys.is_empty()
    }

    /// Every place the descriptions and the chain table hold.
    fn places(&mut self) -> Vec<&mut Place> {
        let mut v: Vec<&mut Place> = self.arrays.iter_mut().flat_map(StoredArray::places).collect();
        for c in self.chains.iter_mut().flatten() {
            v.extend(c.parameters.values_mut().filter_map(Param::places));
        }
        v
    }

    /// The chain index, made again where it does not hold the chain table's chains (it is
    /// derived from them: refreshing it changes nothing a reader sees).
    pub fn index_chains(&mut self) {
        if self.chain_index.0.len() != self.chains.len() {
            self.chain_index.0 = self.chains.iter().enumerate().map(|(k, c)| (chain_json(c).to_string(), k as u32)).collect();
        }
    }

    /// What a graft of the file IR's arrays `from` adds to these (spec/stored-array.md §6
    /// <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#6-in-the-ir>), or why it
    /// cannot, with nothing changed: their element ids by `at`, the parsed source's null key the
    /// file's key in the store, `key`, their chains looked up in this table's index (made current
    /// by [`Stored::index_chains`]), each array's chains once each (two its chains the renumbering
    /// makes one are one, and its rows and unit parameters name that one, so long as they give one
    /// set of unit parameters), and the map's parameter columns of one name of one type. The file's
    /// arrays are fitted first to the chunk map's `room`, then to the descriptions' `descriptions`,
    /// as at describe: where even their fallen-back descriptions do not fit, the file joins the
    /// store with none, its key marked `undescribed`.
    pub(crate) fn plan_graft(&self, from: &Stored, at: &dyn Fn(u32) -> u32, key: &str, room: u64, descriptions: u64) -> Res<GraftPlan> {
        let n = from.arrays.len();
        let w = map_weights(&from.map, n);
        let (keep, _) = fit(&w, room);
        let plan = self.plan_kept(from, at, key, keep, &w)?;
        if plan.weights.0 <= descriptions {
            return Ok(plan);
        }
        // a chain the store's table has costs nothing more; an added one, its weight
        let first = self.chains.len() as u32;
        let cost = |c: u32| c.checked_sub(first).and_then(|k| plan.added.get(k as usize)).map_or(0, |x| chain_weight(&x.0));
        match fit_descriptions(&plan.arrays, &cost, descriptions) {
            Some(fits) => {
                let keep = plan.keep.iter().zip(&fits).map(|(&a, &b)| a && b).collect();
                self.plan_kept(from, at, key, keep, &w)
            }
            None => Ok(GraftPlan {
                added: Vec::new(),
                arrays: Vec::new(),
                locals: vec![Vec::new(); n],
                keep: vec![false; n],
                skip: from.map.params.keys().cloned().collect(),
                weights: (0, 0),
                undescribed: true,
            }),
        }
    }

    /// [`Stored::plan_graft`] of the file's arrays whose rows `keep` keeps (`w`, each one's map
    /// entries), the others fallen back.
    fn plan_kept(&self, from: &Stored, at: &dyn Fn(u32) -> u32, key: &str, keep: Vec<bool>, w: &[u64]) -> Res<GraftPlan> {
        let rows: u64 = w.iter().zip(&keep).filter(|p| *p.1).map(|p| *p.0).sum();
        // the chains the remaining descriptions name, in order of first use, each renumbered
        // and looked up by its text: one the table has keeps its position, a new one is appended
        let mut added: Vec<(Chain, String)> = Vec::new();
        let mut added_at: HashMap<String, u32> = HashMap::new();
        let mut position: HashMap<u32, u32> = HashMap::new();
        for (a, &k) in from.arrays.iter().zip(&keep) {
            if !k {
                continue;
            }
            for &c in &a.chains {
                if position.contains_key(&c) {
                    continue;
                }
                let Some(chain) = from.chains.get(c as usize) else { continue };
                let mut chain = chain.clone();
                for p in chain.iter_mut().flat_map(|x| x.parameters.values_mut().filter_map(Param::places)) {
                    p.renumber(at, key);
                }
                let text = chain_json(&chain).to_string();
                let k = match self.chain_index.0.get(&text).or_else(|| added_at.get(&text)) {
                    Some(&k) => k,
                    None => {
                        let k = (self.chains.len() + added.len()) as u32;
                        added_at.insert(text.clone(), k);
                        added.push((chain, text));
                        k
                    }
                };
                position.insert(c, k);
            }
        }
        let mut arrays = Vec::with_capacity(from.arrays.len());
        let mut locals = Vec::with_capacity(from.arrays.len());
        for (a, &k) in from.arrays.iter().zip(&keep) {
            let mut a = a.clone();
            a.renumber(at, key);
            if !k {
                a.fall_back();
                locals.push(Vec::new());
                arrays.push(a);
                continue;
            }
            // an array's chains in the store's table, once each: two of its chains the renumbering
            // makes one are one, and its rows and unit parameters name that one
            let mut mine: Vec<u32> = Vec::with_capacity(a.chains.len());
            let local: Vec<u32> = a
                .chains
                .iter()
                .map(|&c| {
                    let c = position.get(&c).copied().unwrap_or(c);
                    match mine.iter().position(|&m| m == c) {
                        Some(k) => k as u32,
                        None => {
                            mine.push(c);
                            (mine.len() - 1) as u32
                        }
                    }
                })
                .collect();
            // chains made one must give one set of unit parameters, or their rows' columns could
            // not all be what the one chain's list says
            let given = |old: usize| -> Vec<(u32, &str)> {
                let mut v: Vec<(u32, &str)> = a.unit_parameters.iter().filter(|u| u.chain as usize == old).map(|u| (u.codec, u.name.as_str())).collect();
                v.sort_unstable();
                v
            };
            for (old, &new) in local.iter().enumerate() {
                if let Some(first) = local.iter().position(|&n| n == new).filter(|&f| f < old) {
                    if given(first) != given(old) {
                        return Err(format!("internal: a graft makes chains {first} and {old} of the stored array of element {} one, and they give other unit parameters", a.element));
                    }
                }
            }
            a.chains = mine;
            for u in a.unit_parameters.iter_mut() {
                u.chain = local.get(u.chain as usize).copied().unwrap_or(u.chain);
            }
            let mut seen = Vec::with_capacity(a.unit_parameters.len());
            a.unit_parameters.retain(|u| {
                let k = (u.chain, u.codec, u.name.clone());
                !seen.contains(&k) && {
                    seen.push(k);
                    true
                }
            });
            locals.push(local);
            arrays.push(a);
        }
        let skip = orphaned_columns(&from.arrays, &from.chains, &keep);
        for (name, col) in from.map.params.iter().filter(|(name, _)| !skip.contains(*name)) {
            if let Some(mine) = self.map.params.get(name) {
                if mine.kind() != col.kind() {
                    return Err(format!("internal: a graft joins the parameter column {name:?} of type {:?} to one of {:?}", col.kind(), mine.kind()));
                }
            }
        }
        // last, what the descriptions add, which the descriptions' limit is checked against
        let d = arrays.iter().map(StoredArray::weight).sum::<u64>() + added.iter().map(|c| chain_weight(&c.0)).sum::<u64>();
        Ok(GraftPlan { added, arrays, locals, keep, skip, weights: (d, rows), undescribed: from.undescribed })
    }

    /// The graft [`Stored::plan_graft`] planned, of the file's arrays `from` with the same `at`
    /// and `key`: it cannot fail. Only [`Ir::graft`], which plans and applies with one `from`,
    /// `at` and `key`, calls it.
    pub(crate) fn graft(&mut self, plan: GraftPlan, from: &Stored, at: &dyn Fn(u32) -> u32, key: &str) {
        self.index_chains();
        for (c, text) in plan.added {
            self.chain_index.0.insert(text, self.chains.len() as u32);
            self.chains.push(c);
        }
        let base = self.arrays.len() as u32;
        self.arrays.extend(plan.arrays);
        self.map.append(&from.map, base, at, key, &plan.locals, &plan.keep, &plan.skip);
        if plan.undescribed {
            self.undescribed_keys.push(key.to_string());
        }
    }

    /// A column, of its entry's type in `codecs`, for every parameter a description with a grid
    /// lists as a unit parameter, whether or not a row gives it a value (spec/stored-array.md §4.1
    /// <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#41-the-arrow-schema>): of the
    /// descriptions from position `from` on (a graft's, the ones it added).
    pub fn add_columns(&mut self, codecs: &[CodecEntry], from: usize) {
        let n = self.map.len();
        for a in self.arrays[from.min(self.arrays.len())..].iter().filter(|a| a.grid.is_some()) {
            for u in &a.unit_parameters {
                let codec = a.chains.get(u.chain as usize).and_then(|&c| self.chains.get(c as usize)).and_then(|c| c.get(u.codec as usize));
                let Some(codec) = codec else { continue };
                let t = codecs.iter().find(|e| e.name == codec.name).and_then(|e| e.parameters.iter().find(|p| p.name == u.name)).and_then(|p| p.column);
                if let Some(t) = t {
                    self.map.params.entry(format!("p.{}.{}", codec.name, u.name)).or_insert_with(|| Column::of(t, n));
                }
            }
        }
    }

    /// What the budget counts of them (spec/stored-array.md §6
    /// <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#6-in-the-ir>): of the
    /// descriptions, each description, each axis of its shape, each run of a listed axis, each of
    /// its places (its declarations, and a fill given at a place), each of its chains and each unit
    /// parameter; of the chain table, each chain, each codec and each place a codec's parameter
    /// names; of the chunk map, each row, each of its coordinates, each of its `ranges` items and
    /// each set unit-parameter value.
    pub fn weights(&self) -> (u64, u64) {
        let d = self.arrays.iter().map(StoredArray::weight).sum::<u64>() + self.chains.iter().map(chain_weight).sum::<u64>();
        // a row's `range` is part of it: only `ranges` items count, and each set parameter value
        let set: usize = self.map.params.values().map(|c| (0..c.len()).filter(|&k| c.get(k).is_some()).count()).sum();
        let r = self.map.len() + self.map.coords.len() + self.map.range_offset.len() + set;
        (d, r as u64)
    }

    /// The descriptions in `(element, index)` order, the rows' `array` renumbered with them, so
    /// that which arrays fall back ([`Stored::fit_map`]) does not depend on the order a parser
    /// walked its source: a file IR's, and a store parser's own, at describe. A store's grafted
    /// arrays are not sorted: each graft's follow those already there, in their order.
    pub fn sort_arrays(&mut self) {
        let mut order: Vec<usize> = (0..self.arrays.len()).collect();
        order.sort_by_key(|&k| (self.arrays[k].element, self.arrays[k].index));
        if order.iter().enumerate().all(|(k, &o)| k == o) {
            return;
        }
        let mut at = vec![0u32; order.len()];
        for (new, &old) in order.iter().enumerate() {
            at[old] = new as u32;
        }
        let mut arrays: Vec<Option<StoredArray>> = std::mem::take(&mut self.arrays).into_iter().map(Some).collect();
        self.arrays = order.iter().map(|&k| arrays[k].take().unwrap()).collect();
        for a in self.map.array.iter_mut() {
            if let Some(&n) = at.get(*a as usize) {
                *a = n;
            }
        }
    }

    /// The chains no description names dropped from the table, and the others renumbered (after
    /// [`Stored::fit_map`], whose fallen-back arrays name none).
    pub fn drop_unnamed_chains(&mut self) {
        let mut named = vec![false; self.chains.len()];
        for &c in self.arrays.iter().flat_map(|a| a.chains.iter()) {
            if let Some(x) = named.get_mut(c as usize) {
                *x = true;
            }
        }
        if named.iter().all(|&x| x) {
            return;
        }
        let mut at = vec![u32::MAX; named.len()];
        let mut kept = 0u32;
        for (k, &n) in named.iter().enumerate() {
            if n {
                at[k] = kept;
                kept += 1;
            }
        }
        let chains = std::mem::take(&mut self.chains);
        self.chains = chains.into_iter().zip(&named).filter(|(_, n)| **n).map(|(c, _)| c).collect();
        for c in self.arrays.iter_mut().flat_map(|a| a.chains.iter_mut()) {
            if let Some(&n) = at.get(*c as usize) {
                *c = n;
            }
        }
        self.chain_index.0.clear();
    }

    /// The general fallback of the arrays whose rows the map has no room for (spec/stored-array.md
    /// §6 <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#6-in-the-ir>): arrays are
    /// tried in the order of `arrays` (a file IR's sorted by [`Stored::sort_arrays`]); one whose
    /// rows do not fit in what remains of `room` keeps none of them (no grid, chains, unit
    /// parameters or rows); later arrays are still tried. The entries the rows kept take.
    pub fn fit_map(&mut self, room: u64) -> u64 {
        let (keep, used) = fit(&map_weights(&self.map, self.arrays.len()), room);
        self.fall_back_all(&keep);
        used
    }

    /// The descriptions' limit's fallback, after [`Stored::fit_map`] and
    /// [`Stored::drop_unnamed_chains`] (spec/stored-array.md §6
    /// <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#6-in-the-ir>): where the
    /// descriptions and the chain table pass `room` entries, the arrays [`fit_descriptions`] finds
    /// no room for fall back, their rows, columns and chains dropped; where even every fallen-back
    /// description passes it, none is kept, and false is returned (the caller marks the source).
    pub fn fit_descriptions(&mut self, room: u64) -> bool {
        if self.weights().0 <= room {
            return true;
        }
        let chains = &self.chains;
        let cost = |c: u32| chains.get(c as usize).map_or(0, chain_weight);
        match fit_descriptions(&self.arrays, &cost, room) {
            Some(keep) => {
                self.fall_back_all(&keep);
                self.drop_unnamed_chains();
                true
            }
            None => {
                *self = Stored::default();
                false
            }
        }
    }

    /// The arrays `keep` does not keep fallen back, with their rows, the parameter columns only
    /// they listed and the object keys only their rows named.
    fn fall_back_all(&mut self, keep: &[bool]) {
        if keep.iter().all(|&k| k) {
            return;
        }
        let orphaned = orphaned_columns(&self.arrays, &self.chains, keep);
        for (a, k) in self.arrays.iter_mut().zip(keep) {
            if !k {
                a.fall_back();
            }
        }
        let rows: Vec<usize> = (0..self.map.len()).filter(|&k| keep.get(self.map.array[k] as usize).copied().unwrap_or(true)).collect();
        self.map = self.map.take(&rows);
        self.map.prune(&orphaned);
    }

    /// The ids after run `r` (its subtree `[r, end)`, `count` members) was unfolded, its members
    /// after the first appended from `base` on: an element in member j's subtree takes member j's
    /// copy's id, and no element is a run's member any more.
    pub fn unfolded(&mut self, r: u32, end: u32, base: u32, count: u64) {
        let size = end - r;
        let f = move |e: u32, member: Option<u64>| -> (u32, Option<u64>) {
            match member {
                Some(j) if e >= r && e < end && j >= 1 && j < count => (base + (j - 1) as u32 * size + (e - r), None),
                Some(_) if e >= r && e < end => (e, None),
                m => (e, m),
            }
        };
        for p in self.places() {
            p.moved(&f);
        }
        self.map.moved(&f);
        // the chains' places may have moved, and with them their texts: the next graft indexes them
        // again
        self.chain_index.0.clear();
    }

    pub fn memory(&self) -> usize {
        let d: usize = self.arrays.iter().map(|a| 96 + a.shape.as_ref().map_or(0, Vec::len) * 8 + a.declarations.len() * 48 + a.chains.len() * 4 + a.unit_parameters.len() * 48).sum();
        let c: usize = self.chains.iter().map(|c| 24 + c.len() * 96).sum();
        d + c + self.map.memory()
    }

    /// The descriptions (their JSON forms), the chain table, each row's values and the
    /// `undescribed` mark.
    pub fn digest(&self, eat: &mut dyn FnMut(&[u8])) {
        eat(&[u8::from(self.undescribed)]);
        for k in &self.undescribed_keys {
            eat(k.as_bytes());
            eat(&[0]);
        }
        for c in &self.chains {
            eat(chain_json(c).to_string().as_bytes());
        }
        for a in &self.arrays {
            eat(a.to_json().to_string().as_bytes());
        }
        self.map.digest(eat);
    }

    /// The JSON document of the descriptions (spec/stored-array.md §2.5
    /// <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#25-the-json-form>): the
    /// chain table, the list, each description with its element's path in `ir`, and the
    /// `undescribed` mark (a bool in a file IR, a list of object keys in a store IR).
    pub fn document(&self, ir: &Ir) -> J {
        let arrays: Vec<J> = self
            .arrays
            .iter()
            .map(|a| {
                let mut j = a.to_json();
                let path = if (a.element as usize) < ir.len() { J::String(ir.path(a.element)) } else { J::Null };
                j.as_object_mut().unwrap().insert("path".into(), path);
                j
            })
            .collect();
        let undescribed = if ir.store { json!(self.undescribed_keys) } else { json!(self.undescribed) };
        json!({"chains": self.chains.iter().map(chain_json).collect::<Vec<J>>(), "arrays": arrays, "undescribed": undescribed})
    }
}

// ---- the checks

/// What both checks look up in the IR.
struct Ctx<'a> {
    ir: &'a Ir,
    codecs: &'a [CodecEntry],
    /// the run root each element belongs to (NONE: none)
    roots: Vec<u32>,
    /// a store IR's objects by key (None: a file IR, where any key is one)
    keys: Option<HashMap<String, u32>>,
}

fn named(ir: &Ir, e: u32) -> String {
    if (e as usize) < ir.len() { format!("element {e} ({:?})", ir.path(e)) } else { format!("element {e}") }
}

impl Ctx<'_> {
    /// Element `e`, of member `member` where it is in a run's subtree.
    fn element(&self, e: u32, member: Option<u64>) -> Res<()> {
        if e as usize >= self.ir.len() {
            return Err(format!("names element {e}, which the IR has not"));
        }
        let r = self.roots[e as usize];
        match member {
            None if r == NONE => Ok(()),
            Some(j) if r == NONE => Err(format!("gives member {j} of {}, which is in no run", named(self.ir, e))),
            None => Err(format!("names {}, in the run {}, without its member", named(self.ir, e), named(self.ir, r))),
            Some(j) => {
                let (count, _) = self.ir.run(r).unwrap_or((1, 0));
                if j >= count {
                    return Err(format!("gives member {j} of {}, whose run has {count}", named(self.ir, e)));
                }
                Ok(())
            }
        }
    }

    /// A range of an object by its key, None for the parsed source: in a store IR, one of its
    /// objects; within the object where its size is known (the parsed source's in a file IR, any
    /// object's in a store IR).
    fn object(&self, key: Option<&str>, offset: u64, length: u64) -> Res<()> {
        let end = offset.checked_add(length).ok_or_else(|| format!("names a range at {offset} of {length} bytes, past 2^64"))?;
        let size = match (&self.keys, key) {
            (None, None) => Some(self.ir.size),
            (None, Some(_)) => None,
            (Some(_), None) => return Err("names the parsed source (a null key) in a store IR".into()),
            (Some(keys), Some(k)) => match keys.get(k) {
                None => return Err(format!("names the object {k:?}, which the store has not")),
                Some(&o) => Some(self.ir.len[o as usize]),
            },
        };
        match size {
            Some(n) if end > n => Err(format!("names [{offset}, {end}) of {}, past its {n} bytes", key.map_or("the parsed source".to_string(), |k| format!("{k:?}")))),
            _ => Ok(()),
        }
    }

    /// A place: its element, and a range within the element's bytes (its extent's length), or a
    /// range of an object.
    fn place(&self, p: &Place) -> Res<()> {
        match p {
            Place::Element { element, member, range } => {
                self.element(*element, *member)?;
                if let Some((o, n)) = range {
                    let len = self.ir.len[*element as usize];
                    if o.checked_add(*n).is_none_or(|end| end > len) {
                        return Err(format!("names a range at {o} of {n} bytes of {}, past its {len} bytes", named(self.ir, *element)));
                    }
                }
                Ok(())
            }
            Place::Extent { object, offset, length } => self.object(object.as_deref(), *offset, *length),
        }
    }

    /// Each parameter column the codecs' entries allow, by its name, with its type.
    fn columns(&self) -> HashMap<String, ColumnType> {
        self.codecs
            .iter()
            .flat_map(|c| c.parameters.iter().filter_map(move |p| Some((format!("p.{}.{}", c.name, p.name), p.column?))))
            .collect()
    }

    fn entry(&self, name: &str) -> Option<&CodecEntry> {
        self.codecs.iter().find(|c| c.name == name)
    }
}

/// Whether element `e`'s recipe, over its `len` bytes, gives its extent as it is (spec/stored-array.md
/// §5.2 <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#52-the-chunk-map>), by the
/// recipe grammar of conventions §8.5
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#85-forms-and-recipes>: no form,
/// no recipe or a null one, or a recipe of only `["src", o, n]` parts (`n` null or left out: to
/// the element's end) and `["rows", o, s, n, c]` parts that, read as ranges of the extent, cover
/// `[0, len)` in order with no gap or overlap: `src` parts end to end, `rows` runs of n bytes each
/// n apart, or a single run of any stride. A part of no bytes covers none, and is skipped before
/// its offset and stride are looked at; a `src` part of null length whose offset is past the
/// element's end has no end, and is not the identity. Then the element alone names the whole
/// extent; any other recipe gives other bytes, and a `range` of the whole extent is the only way
/// to name the raw ones.
fn identity_recipe(ir: &Ir, e: u32, len: u64) -> bool {
    let Some(form) = ir.form(e) else { return true };
    let Ok(j) = serde_json::from_str::<J>(form) else { return true };
    let recipe = match j.get("recipe") {
        None | Some(J::Null) => return true,
        Some(r) => r,
    };
    let Some(parts) = recipe.as_array() else { return false };
    let mut pos = 0u64;
    for p in parts {
        let p = p.as_array().map(Vec::as_slice).unwrap_or(&[]);
        let at = |k: usize| p.get(k).and_then(J::as_u64);
        // the part's start and its bytes, by the grammar; anything else is not the identity
        let (o, n, contiguous) = match (p.first().and_then(J::as_str), p.len()) {
            (Some("src"), 2 | 3) => {
                let Some(o) = at(1) else { return false };
                let n = match p.get(2) {
                    // a null length runs to the element's end, which an offset past it lacks
                    None | Some(J::Null) => match len.checked_sub(o) {
                        Some(n) => n,
                        None => return false,
                    },
                    Some(n) => match n.as_u64() {
                        Some(n) => n,
                        None => return false,
                    },
                };
                (o, n, true)
            }
            (Some("rows"), 5) => {
                let (Some(o), Some(s), Some(n), Some(c)) = (at(1), at(2), at(3), at(4)) else { return false };
                let Some(t) = n.checked_mul(c) else { return false };
                (o, t, c <= 1 || s == n)
            }
            _ => return false,
        };
        if n == 0 {
            continue;
        }
        if o != pos || !contiguous {
            return false;
        }
        // the part's end, which may not pass the extent's
        match o.checked_add(n).filter(|&end| end <= len) {
            Some(end) => pos = end,
            None => return false,
        }
    }
    pos == len
}

/// The units along an axis of `len` elements.
fn units(a: &AxisUnits, len: u64) -> u64 {
    match a {
        AxisUnits::Each(_) if len == 0 => 0,
        AxisUnits::Each(n) => len.div_ceil((*n).max(1)),
        AxisUnits::List(runs) => runs.iter().map(|r| match r {
            Run::One(_) => 1,
            Run::Repeat(_, c) => *c,
        }).sum(),
    }
}

/// Why axis `k` of `len` elements is not in canonical form under `edge`, if it is not.
fn axis_problem(k: usize, a: &AxisUnits, len: u64, edge: Edge) -> Option<String> {
    match a {
        AxisUnits::Each(0) => Some(format!("axis {k}'s units are 0 long")),
        AxisUnits::Each(n) if edge == Edge::Short && len > 0 && *n > len => {
            Some(format!("axis {k}'s units are {n} long under Short, past the axis's {len}"))
        }
        AxisUnits::Each(_) => None,
        AxisUnits::List(runs) => {
            let length = |r: &Run| match r {
                Run::One(n) | Run::Repeat(n, _) => *n,
            };
            for r in runs {
                if length(r) == 0 {
                    return Some(format!("axis {k} lists a unit 0 long"));
                }
                if let Run::Repeat(n, c) = r {
                    if *c < 2 {
                        return Some(format!("axis {k} lists a run of {c} units {n} long, not One"));
                    }
                }
            }
            if let Some(w) = runs.windows(2).find(|w| length(&w[0]) == length(&w[1])) {
                return Some(format!("axis {k} lists two runs of units {} long side by side", length(&w[0])));
            }
            let sum = runs.iter().try_fold(0u64, |s, r| match r {
                Run::One(n) => s.checked_add(*n),
                Run::Repeat(n, c) => n.checked_mul(*c).and_then(|x| s.checked_add(x)),
            });
            if sum != Some(len) {
                return Some(format!("axis {k} lists units that do not sum to its {len}"));
            }
            // all of one length: Each; and under Short, all of one length but a shorter last, which
            // Short cuts as the axis's end does (under Full, Each would say the last is stored full)
            let each = match runs.as_slice() {
                [_] => true,
                [a, Run::One(m)] => edge == Edge::Short && *m < length(a),
                _ => false,
            };
            if each {
                return Some(format!("axis {k} lists units that are Each({})", length(&runs[0])));
            }
            None
        }
    }
}

impl StoredArray {
    /// The checks of one description (spec/stored-array.md §5.1
    /// <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#51-each-description>)
    /// against the chain table `chains`: the problem, if any.
    fn check(&self, ctx: &Ctx, chains: &[Chain]) -> Res<()> {
        let shape: &[u64] = self.shape.as_deref().unwrap_or(&[]);
        let rank = shape.len();
        if self.element as usize >= ctx.ir.len() {
            return Err(format!("names element {}, which the IR has not", self.element));
        }
        if ctx.roots[self.element as usize] != NONE {
            return Err(format!("is keyed by an element in the run {}, which an unfold would leave behind", named(ctx.ir, ctx.roots[self.element as usize])));
        }
        for p in &self.declarations {
            ctx.place(p).map_err(|e| format!("a declaration {e}"))?;
        }
        if self.shape.is_none() {
            let what = [(self.grid.is_some(), "a grid"), (self.axes.is_some(), "axes"), (self.pad_from.is_some(), "pad_from")];
            if let Some((_, w)) = what.iter().find(|w| w.0) {
                return Err(format!("its source declares no extent, and it has {w}"));
            }
        }
        if let Some(g) = &self.grid {
            if g.axes.len() != rank {
                return Err(format!("its grid has {} axes, its shape {rank}", g.axes.len()));
            }
            for (k, (a, &len)) in g.axes.iter().zip(shape).enumerate() {
                if let Some(p) = axis_problem(k, a, len, g.edge) {
                    return Err(format!("its grid is not in canonical form: {p}"));
                }
            }
        }
        match (&self.grid, self.chains.is_empty()) {
            (Some(_), true) => return Err("it has a grid and no chain".into()),
            (None, false) => return Err("it has chains and no grid".into()),
            _ => {}
        }
        if let Some(&c) = self.chains.iter().find(|&&c| c as usize >= chains.len()) {
            return Err(format!("its chain {c} is past the chain table's {}", chains.len()));
        }
        if let Some((k, &c)) = self.chains.iter().enumerate().find(|(k, c)| self.chains[..*k].contains(c)) {
            return Err(format!("its chains name chain {c} twice (at {k})"));
        }
        if let Some(axes) = &self.axes {
            let mut seen = vec![false; rank];
            let perm = axes.len() == rank && axes.iter().all(|&a| (a as usize) < rank && !std::mem::replace(&mut seen[a as usize], true));
            if !perm {
                return Err(format!("its axes {axes:?} are not a permutation of its {rank}"));
            }
            if axes.iter().enumerate().all(|(k, &a)| a as usize == k) {
                return Err(format!("its axes {axes:?} are storage order, which no axes say"));
            }
        }
        let given = matches!(self.fill, Fill::Given(_));
        // elements that `source` makes are of the size the source's encoding gives them, which
        // the IR need not know: every chain of the array then begins with it
        let sourced = self.chains.iter().all(|&c| chains[c as usize].first().is_some_and(|k| k.name == "source"));
        match self.item_bits {
            None if self.grid.is_some() && !sourced => return Err("it has a grid and no item_bits, and a chain that does not begin with `source`".into()),
            None if given => return Err("its fill is given and it has no item_bits".into()),
            Some(0) => return Err("its item_bits is 0".into()),
            _ => {}
        }
        if let Some(p) = self.pad_from {
            match self.item_bits {
                None => return Err("it has pad_from and no item_bits".into()),
                Some(b) if b % 8 == 0 => return Err(format!("it has pad_from with item_bits {b}, whole bytes")),
                _ if p as usize >= rank => return Err(format!("its pad_from {p} is not below its rank {rank}")),
                _ => {}
            }
        }
        if let (Fill::Given(f), Some(bits)) = (&self.fill, self.item_bits) {
            let want = bits.div_ceil(8);
            let got = match f {
                Param::Value(J::String(s)) => crate::zarr2::base64(s).map(|b| b.len() as u64).ok_or("its fill value is not base64")?,
                Param::Value(_) => return Err("its fill value is not a base64 string".into()),
                Param::Source(p) => {
                    ctx.place(p).map_err(|e| format!("its fill {e}"))?;
                    match p {
                        Place::Element { range: Some((_, n)), .. } | Place::Extent { length: n, .. } => *n,
                        Place::Element { element, .. } => ctx.ir.len[*element as usize],
                    }
                }
            };
            if got != want {
                return Err(format!("its fill value is {got} bytes, not {want}"));
            }
        }
        for (k, u) in self.unit_parameters.iter().enumerate() {
            if self.unit_parameters[..k].contains(u) {
                return Err(format!("its unit parameter {:?} of chain {} and codec {} is listed twice", u.name, u.chain, u.codec));
            }
            let chain = self.chains.get(u.chain as usize).and_then(|&c| chains.get(c as usize));
            let Some(chain) = chain else {
                return Err(format!("its unit parameter {:?} names its chain {}, which it has not", u.name, u.chain));
            };
            let Some(codec) = chain.get(u.codec as usize) else {
                return Err(format!("its unit parameter {:?} names codec {} of a chain of {}", u.name, u.codec, chain.len()));
            };
            if chain.iter().filter(|c| c.name == codec.name).count() > 1 {
                return Err(format!("its unit parameter {:?} is of the codec {}, which its chain has twice", u.name, codec.name));
            }
            let column = ctx.entry(&codec.name).and_then(|e| e.parameters.iter().find(|p| p.name == u.name)).and_then(|p| p.column);
            if column.is_none() {
                return Err(format!("its unit parameter {:?} is not one the codec {} allows as a column", u.name, codec.name));
            }
            if codec.parameters.contains_key(&u.name) {
                return Err(format!("its unit parameter {:?} is one the codec {} gives itself", u.name, codec.name));
            }
        }
        Ok(())
    }
}

/// The checks of the chain table: each codec one of the list, with only its entry's parameters,
/// their places the IR's.
fn check_chains(ctx: &Ctx, chains: &[Chain]) -> Res<()> {
    let mut seen: HashMap<String, usize> = HashMap::with_capacity(chains.len());
    for (k, c) in chains.iter().enumerate() {
        if let Some(j) = seen.insert(chain_json(c).to_string(), k) {
            return Err(format!("internal: chains {j} and {k} of the chain table are one"));
        }
        for codec in c {
            let Some(entry) = ctx.entry(&codec.name) else {
                return Err(format!("internal: chain {k} has the codec {:?}, which the IR's list has not", codec.name));
            };
            for (name, p) in &codec.parameters {
                if !entry.parameters.iter().any(|e| e.name == name) {
                    return Err(format!("internal: chain {k}'s codec {} has the parameter {name:?}, which its entry has not", codec.name));
                }
                if let Param::Source(p) = p {
                    ctx.place(p).map_err(|e| format!("internal: chain {k}'s codec {}'s parameter {name:?} {e}", codec.name))?;
                }
            }
        }
    }
    Ok(())
}

impl ChunkMap {
    /// The checks of the chunk map (spec/stored-array.md §5.2
    /// <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#52-the-chunk-map>) against
    /// the descriptions `arrays` and the chain table `chains`.
    fn check(&self, ctx: &Ctx, arrays: &[StoredArray], chains: &[Chain]) -> Res<()> {
        let n = self.len();
        // its schema: the columns of one length, the offsets in order, the parameter columns the
        // codecs' entries'
        let lengths = [self.coords_end.len(), self.element.len(), self.member.len(), self.range.len(), self.has_ranges.len(), self.ranges_end.len(), self.chain.len(), self.skips.len()];
        if lengths.iter().any(|&l| l != n) || self.params.values().any(|c| c.len() != n) {
            return Err("internal: the chunk map's columns are not all of one length".into());
        }
        let r = self.range_offset.len();
        let ends_ok = |ends: &[u64], total: usize| ends.windows(2).all(|w| w[0] <= w[1]) && ends.last().map_or(0, |&e| e as usize) == total;
        if !ends_ok(&self.coords_end, self.coords.len()) || !ends_ok(&self.ranges_end, r) || self.range_object.len() != r || self.range_length.len() != r {
            return Err("internal: the chunk map's list offsets do not match its values".into());
        }
        if (0..n).any(|k| !self.has_ranges[k] && Self::start(&self.ranges_end, k) != self.ranges_end[k] as usize) {
            return Err("internal: the chunk map has ranges in a null row".into());
        }
        if self.range_object.iter().any(|o| o.is_some_and(|o| o as usize >= self.objects.len())) {
            return Err("internal: the chunk map names an object past its dictionary".into());
        }
        let columns = ctx.columns();
        for (name, col) in &self.params {
            match columns.get(name).copied() {
                None => return Err(format!("internal: the chunk map's column {name:?} is no codec's parameter column")),
                Some(t) if t != col.kind() => return Err(format!("internal: the chunk map's column {name:?} is a {:?}, not {t:?}", col.kind())),
                _ => {}
            }
        }
        // a column for every unit parameter a description with a grid lists
        for d in arrays.iter().filter(|d| d.grid.is_some()) {
            for u in &d.unit_parameters {
                let codec = &chains[d.chains[u.chain as usize] as usize][u.codec as usize];
                let name = format!("p.{}.{}", codec.name, u.name);
                if !self.params.contains_key(&name) {
                    return Err(format!("internal: the chunk map has no column {name:?}, which the stored array of {} gives", named(ctx.ir, d.element)));
                }
            }
        }
        // the parameter columns each (array, chain) sets, with the codec each is of
        let mut sets: HashMap<(u32, u32), Vec<(String, u32)>> = HashMap::new();
        // whether each form's recipe is the identity, parsed once per form
        let mut identity: HashMap<(Option<u32>, u64), bool> = HashMap::new();
        // each gridded array's units along each axis, counted once
        let grid_units: Vec<Vec<u64>> = arrays
            .iter()
            .map(|d| d.grid.as_ref().map_or_else(Vec::new, |g| g.axes.iter().zip(d.shape.as_deref().unwrap_or(&[])).map(|(a, &len)| units(a, len)).collect()))
            .collect();
        for k in 0..n {
            let a = self.array[k];
            let Some(d) = arrays.get(a as usize) else {
                return Err(format!("internal: chunk map row {k} is of array {a}, past the {} described", arrays.len()));
            };
            let row = |e: String| format!("internal: chunk map row {k}, of the array of {}: {e}", named(ctx.ir, d.element));
            let Some(g) = &d.grid else {
                return Err(row("the array has no grid".into()));
            };
            let c = self.coords_of(k);
            if c.len() != g.axes.len() {
                return Err(row(format!("its coordinates {c:?} are not of the array's rank {}", g.axes.len())));
            }
            if let Some(i) = (0..c.len()).find(|&i| c[i] >= grid_units[a as usize][i]) {
                return Err(row(format!("its coordinates {c:?} are outside the grid on axis {i}")));
            }
            if k > 0 {
                let prev = (self.array[k - 1], self.coords_of(k - 1));
                if prev == (a, c) {
                    return Err(row(format!("its coordinates {c:?} repeat")));
                }
                if prev > (a, c) {
                    return Err(row("it is out of order".into()));
                }
            }
            match (self.element[k], self.ranges_of(k)) {
                (Some(_), Some(_)) => return Err(row("both its element and its ranges are set".into())),
                (None, None) => return Err(row("neither its element nor its ranges is set".into())),
                (Some(e), None) => {
                    ctx.element(e, self.member[k]).map_err(|e| row(format!("it {e}")))?;
                    if let Some((o, n)) = self.range[k] {
                        if n == 0 {
                            return Err(row(format!("its range at {o} of its element is 0 bytes long")));
                        }
                        let len = ctx.ir.len[e as usize];
                        if o.checked_add(n).is_none_or(|end| end > len) {
                            return Err(row(format!("its range at {o} of {n} bytes is past its element's {len} bytes")));
                        }
                        if (o, n) == (0, len) && *identity.entry((ctx.ir.form_id(e), len)).or_insert_with(|| identity_recipe(ctx.ir, e, len)) {
                            return Err(row("its range is its element's whole extent, which the element alone names".into()));
                        }
                    }
                }
                (None, Some(rs)) => {
                    if self.member[k].is_some() {
                        return Err(row("it has a member and no element".into()));
                    }
                    if self.range[k].is_some() {
                        return Err(row("it has a range of an element and no element".into()));
                    }
                    if rs.is_empty() {
                        return Err(row("its ranges are empty".into()));
                    }
                    for x in rs {
                        if self.range_length[x] == 0 {
                            return Err(row(format!("its range at {} is 0 bytes long", self.range_offset[x])));
                        }
                        let key = self.range_object[x].map(|o| self.objects[o as usize].as_str());
                        ctx.object(key, self.range_offset[x], self.range_length[x]).map_err(|e| row(format!("a range {e}")))?;
                    }
                }
            }
            let chain = match self.chain[k] {
                Some(0) => return Err(row("its chain is 0, not null".into())),
                Some(ch) if ch as usize >= d.chains.len() => return Err(row(format!("its chain {ch} is past the array's {}", d.chains.len()))),
                ch => ch.unwrap_or(0),
            };
            let codecs = &chains[d.chains[chain as usize] as usize];
            let skips = match self.skips[k] {
                Some(0) => return Err(row("it skips nothing, not null".into())),
                Some(m) if codecs.len() < 64 && m >> codecs.len() != 0 => {
                    return Err(row(format!("its skips {m:#b} name codecs past its chain's {}", codecs.len())));
                }
                m => m.unwrap_or(0),
            };
            let expected = sets.entry((a, chain)).or_insert_with(|| {
                d.unit_parameters.iter().filter(|u| u.chain == chain).filter_map(|u| {
                    let codec = codecs.get(u.codec as usize)?;
                    Some((format!("p.{}.{}", codec.name, u.name), u.codec))
                }).collect()
            });
            for (name, codec) in expected.iter() {
                let want = *codec >= 64 || skips >> codec & 1 == 0;
                let set = self.params.get(name).is_some_and(|col| col.get(k).is_some());
                if want != set {
                    return Err(row(format!("its column {name:?} is {}", if want { "null, but its array gives it" } else { "set, but its codec is skipped" })));
                }
            }
            for (name, col) in &self.params {
                if col.get(k).is_some() && !expected.iter().any(|e| &e.0 == name) {
                    return Err(row(format!("its column {name:?} is set, but its array does not give it for its chain")));
                }
            }
        }
        Ok(())
    }
}

/// Both checks of an IR's stored arrays, with the IR's codecs (spec/stored-array.md §5
/// <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#5-checks>): a description or a
/// row they refuse is a bug in its parser, an internal error naming the array's element.
pub fn check(ir: &Ir) -> Res<()> {
    check_with(ir, CODECS)
}

/// The `undescribed` mark's invariants (spec/stored-array.md §5
/// <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#5-checks>): a file IR lists no
/// keys, and one marked `undescribed` keeps no descriptions, chains or rows; a store IR is not
/// marked itself, and its list names its objects (`keys`), once each, none of which holds a
/// description's key or a row's `element` (another object's rows may read its bytes).
fn check_undescribed(ir: &Ir, keys: Option<&HashMap<String, u32>>) -> Res<()> {
    let s = &ir.stored;
    let Some(keys) = keys else {
        if !s.undescribed_keys.is_empty() {
            return Err("internal: a file IR lists objects as undescribed".into());
        }
        if s.undescribed && !(s.arrays.is_empty() && s.chains.is_empty() && s.map.is_empty()) {
            return Err("internal: a source marked undescribed keeps descriptions".into());
        }
        return Ok(());
    };
    if s.undescribed {
        return Err("internal: a store IR marked undescribed itself rather than by its objects' keys".into());
    }
    let mut listed: HashSet<u32> = HashSet::new();
    for k in &s.undescribed_keys {
        let Some(&o) = keys.get(k) else {
            return Err(format!("internal: the undescribed key {k:?} is no object of the store"));
        };
        if !listed.insert(o) {
            return Err(format!("internal: the undescribed key {k:?} is listed twice"));
        }
    }
    if listed.is_empty() {
        return Ok(());
    }
    let of = ir.sources().of;
    let inside = |e: u32| of.get(e as usize).is_some_and(|o| listed.contains(o));
    if let Some(a) = s.arrays.iter().find(|a| inside(a.element)) {
        return Err(format!("internal: the stored array of {} is in an object marked undescribed", named(ir, a.element)));
    }
    if let Some(e) = s.map.element.iter().flatten().find(|&&e| inside(e)) {
        return Err(format!("internal: a chunk map row names {}, in an object marked undescribed", named(ir, *e)));
    }
    Ok(())
}

/// [`check`], with the codec list `codecs`.
pub fn check_with(ir: &Ir, codecs: &[CodecEntry]) -> Res<()> {
    let s = &ir.stored;
    if s.is_empty() {
        return Ok(());
    }
    let keys: Option<HashMap<String, u32>> = ir.store.then(|| (0..ir.len() as u32).filter(|&i| ir.kind[i as usize] == OBJECT).map(|o| (ir.key(o), o)).collect());
    check_undescribed(ir, keys.as_ref())?;
    let ctx = Ctx { ir, codecs, roots: ir.run_roots(), keys };
    for a in &s.arrays {
        a.check(&ctx, &s.chains).map_err(|e| format!("internal: the stored array of {}: {e}", named(ir, a.element)))?;
    }
    // the keys: (element, index) once each, and on each element no index or 0 to n - 1
    let mut on: BTreeMap<u32, Vec<Option<u64>>> = BTreeMap::new();
    for a in &s.arrays {
        on.entry(a.element).or_default().push(a.index);
    }
    for (e, mut v) in on {
        let who = named(ir, e);
        v.sort();
        if v.windows(2).any(|w| w[0] == w[1]) {
            return Err(format!("internal: the stored arrays of {who}: two have one index"));
        }
        if v.len() > 1 && v[0].is_none() {
            return Err(format!("internal: the stored arrays of {who}: some have an index and some have none"));
        }
        if v.iter().enumerate().any(|(k, x)| x.is_some_and(|x| x != k as u64)) {
            return Err(format!("internal: the stored arrays of {who}: their indexes are not 0 to {}", v.len() - 1));
        }
    }
    check_chains(&ctx, &s.chains)?;
    s.map.check(&ctx, &s.arrays, &s.chains)
}

#[cfg(feature = "arrow")]
pub mod arrow {
    //! The chunk map as one Arrow record batch (spec/stored-array.md §4.1
    //! <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#41-the-arrow-schema>).

    use super::{ChunkMap, Column, CHUNK_MAP_VERSION};
    use arrow_array::types::Int32Type;
    use arrow_array::{
        ArrayRef, DictionaryArray, Float64Array, Int32Array, Int64Array, LargeListArray, RecordBatch, StringArray, StructArray, UInt32Array,
        UInt64Array,
    };
    use arrow_schema::{ArrowError, DataType, Field, Fields, Schema};
    use std::collections::HashMap;
    use std::sync::Arc;

    // the crates, for the bindings that export the batch, at the versions the batch is built with
    pub use arrow_array;
    pub use arrow_schema;

    fn part_fields() -> Fields {
        Fields::from(vec![Field::new("offset", DataType::UInt64, false), Field::new("length", DataType::UInt64, false)])
    }

    fn range_fields() -> Fields {
        Fields::from(vec![
            Field::new("object", DataType::Dictionary(Box::new(DataType::Int32), Box::new(DataType::Utf8)), true),
            Field::new("offset", DataType::UInt64, false),
            Field::new("length", DataType::UInt64, false),
        ])
    }

    /// The offsets of a list column (64-bit: a map the budget admits may hold past 2^31 values)
    /// from each row's end.
    fn offsets(ends: &[u64]) -> Result<arrow_buffer::OffsetBuffer<i64>, ArrowError> {
        let mut v = Vec::with_capacity(ends.len() + 1);
        v.push(0i64);
        for &e in ends {
            v.push(i64::try_from(e).map_err(|_| ArrowError::ComputeError("a chunk map list column past 2^63 values".into()))?);
        }
        Ok(arrow_buffer::OffsetBuffer::new(v.into()))
    }

    /// The chunk map's schema: its named columns, then its parameter columns by name, and the
    /// metadata `refkv.chunk_map`, the schema's version.
    pub fn schema(map: &ChunkMap) -> Schema {
        let mut fields = vec![
            Field::new("array", DataType::UInt32, false),
            Field::new("coords", DataType::LargeList(Arc::new(Field::new("item", DataType::UInt64, false))), false),
            Field::new("element", DataType::UInt32, true),
            Field::new("member", DataType::UInt64, true),
            Field::new("range", DataType::Struct(part_fields()), true),
            Field::new("ranges", DataType::LargeList(Arc::new(Field::new("item", DataType::Struct(range_fields()), false))), true),
            Field::new("chain", DataType::UInt32, true),
            Field::new("skips", DataType::UInt64, true),
        ];
        for (name, col) in &map.params {
            let t = match col {
                Column::UInt64(_) => DataType::UInt64,
                Column::Int64(_) => DataType::Int64,
                Column::Float64(_) => DataType::Float64,
            };
            fields.push(Field::new(name, t, true));
        }
        let meta = HashMap::from([("refkv.chunk_map".to_string(), CHUNK_MAP_VERSION.to_string())]);
        Schema::new_with_metadata(fields, meta)
    }

    /// The chunk map as one record batch.
    pub fn batch(map: &ChunkMap) -> Result<RecordBatch, ArrowError> {
        let schema = Arc::new(schema(map));
        let item = |f: &Field| match f.data_type() {
            DataType::LargeList(i) => i.clone(),
            _ => unreachable!(),
        };
        let coords = LargeListArray::try_new(item(schema.field(1)), offsets(&map.coords_end)?, Arc::new(UInt64Array::from(map.coords.clone())), None)?;
        let keys: Vec<Option<i32>> = map.range_object.iter().map(|o| o.map(|o| o as i32)).collect();
        let values = StringArray::from(map.objects.iter().map(String::as_str).collect::<Vec<&str>>());
        let object = DictionaryArray::<Int32Type>::try_new(Int32Array::from(keys), Arc::new(values))?;
        let ranges = StructArray::try_new(
            range_fields(),
            vec![Arc::new(object), Arc::new(UInt64Array::from(map.range_offset.clone())), Arc::new(UInt64Array::from(map.range_length.clone()))],
            None,
        )?;
        let nulls = arrow_buffer::NullBuffer::from(map.has_ranges.clone());
        let ranges = LargeListArray::try_new(item(schema.field(5)), offsets(&map.ranges_end)?, Arc::new(ranges), Some(nulls))?;
        let part = |f: fn(&(u64, u64)) -> u64| -> ArrayRef { Arc::new(UInt64Array::from(map.range.iter().map(|r| r.as_ref().map_or(0, f)).collect::<Vec<u64>>())) };
        let range = StructArray::try_new(
            part_fields(),
            vec![part(|r| r.0), part(|r| r.1)],
            Some(arrow_buffer::NullBuffer::from(map.range.iter().map(Option::is_some).collect::<Vec<bool>>())),
        )?;
        let mut columns: Vec<ArrayRef> = vec![
            Arc::new(UInt32Array::from(map.array.clone())),
            Arc::new(coords),
            Arc::new(UInt32Array::from(map.element.clone())),
            Arc::new(UInt64Array::from(map.member.clone())),
            Arc::new(range),
            Arc::new(ranges),
            Arc::new(UInt32Array::from(map.chain.clone())),
            Arc::new(UInt64Array::from(map.skips.clone())),
        ];
        for col in map.params.values() {
            columns.push(match col {
                Column::UInt64(v) => Arc::new(UInt64Array::from(v.clone())),
                Column::Int64(v) => Arc::new(Int64Array::from(v.clone())),
                Column::Float64(v) => Arc::new(Float64Array::from(v.clone())),
            });
        }
        RecordBatch::try_new(schema, columns)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The test list: the IR's codecs, and two whose parameters a unit may give.
    const TEST_CODECS: &[CodecEntry] = &[
        CodecEntry {
            name: "source",
            parameters: &[ParamEntry { name: "format", column: None }, ParamEntry { name: "id", column: None }, ParamEntry { name: "name", column: None }, ParamEntry { name: "parameters", column: None }],
        },
        CodecEntry { name: "quantize", parameters: &[ParamEntry { name: "scale", column: Some(ColumnType::Float64) }, ParamEntry { name: "seed", column: Some(ColumnType::UInt64) }, ParamEntry { name: "method", column: None }] },
        CodecEntry { name: "squash", parameters: &[ParamEntry { name: "level", column: None }] },
        CodecEntry { name: "acme.zfp", parameters: &[ParamEntry { name: "rate", column: Some(ColumnType::Float64) }] },
    ];

    fn codec(name: &str, params: &[(&str, Param)]) -> Codec {
        Codec { name: name.into(), parameters: params.iter().map(|(k, v)| (k.to_string(), v.clone())).collect() }
    }

    /// A file IR: the root, a header value (element 1), a run of 3 chunks (element 3, stride 10)
    /// under a struct (element 2), and a data element (element 4), finished.
    fn file_ir() -> Ir {
        let mut ir = Ir::new(100);
        ir.value(0, "header", NO_INDEX, "bytes[8]", 0, (0, 8), None).unwrap();
        let s = ir.struct_(0, "chunks", NO_INDEX, 0, Some((10, 30))).unwrap();
        let first = ir.data(s, "c", 0, 0, (10, 10), "{}").unwrap();
        let second = ir.data(s, "c", 1, 0, (20, 10), "{}").unwrap();
        assert!(ir.fold(first, second));
        let third = ir.data(s, "c", 2, 0, (30, 10), "{}").unwrap();
        assert!(ir.fold(first, third));
        ir.data(0, "data", NO_INDEX, 0, (40, 60), "{}").unwrap();
        crate::check::finish(&mut ir).unwrap();
        ir
    }

    fn el(element: u32) -> Place {
        Place::Element { element, member: None, range: None }
    }

    /// A description of every kind the checks accept, over `file_ir`: a chunked array whose two
    /// chains quantize their units by a column, a rank-0 array stored as is, an array with no
    /// grid, two arrays one element declares, and one of an empty axis.
    fn valid() -> Stored {
        let chains = vec![
            vec![codec("quantize", &[("method", Param::Value(json!(1)))]), codec("squash", &[("level", Param::Value(json!(3)))])],
            vec![],
            vec![codec("source", &[("format", Param::Value(json!("hdf5"))), ("id", Param::Value(json!(307))), ("parameters", Param::Source(Place::Extent { object: None, offset: 0, length: 4 }))])],
        ];
        let base = StoredArray {
            element: 1,
            index: None,
            declarations: vec![el(1)],
            shape: Some(vec![25, 12]),
            axes: Some(vec![1, 0]),
            item_bits: Some(12),
            pad_from: Some(1),
            grid: Some(Grid { axes: vec![AxisUnits::List(vec![Run::One(5), Run::Repeat(10, 2)]), AxisUnits::Each(8)], edge: Edge::Full }),
            chains: vec![0, 2],
            fill: Fill::Given(Param::Value(json!("AAA="))),
            unit_parameters: vec![UnitParameter { chain: 0, codec: 0, name: "scale".into() }],
        };
        let scalar = StoredArray {
            element: 4,
            index: Some(0),
            declarations: vec![Place::Extent { object: None, offset: 0, length: 8 }],
            shape: Some(vec![]),
            axes: None,
            item_bits: Some(64),
            pad_from: None,
            grid: Some(Grid { axes: vec![], edge: Edge::Short }),
            chains: vec![1],
            fill: Fill::Given(Param::Source(Place::Element { element: 3, member: Some(1), range: Some((0, 8)) })),
            unit_parameters: vec![],
        };
        let ungridded = StoredArray {
            element: 4,
            index: Some(1),
            declarations: vec![el(4)],
            shape: Some(vec![3]),
            axes: None,
            item_bits: None,
            pad_from: None,
            grid: None,
            chains: vec![],
            fill: Fill::Undefined,
            unit_parameters: vec![],
        };
        let empty = StoredArray {
            element: 2,
            index: None,
            declarations: vec![],
            shape: Some(vec![0, 7, 0]),
            axes: None,
            item_bits: Some(8),
            pad_from: None,
            grid: Some(Grid { axes: vec![AxisUnits::Each(16), AxisUnits::List(vec![Run::One(3), Run::One(4)]), AxisUnits::List(vec![])], edge: Edge::Short }),
            chains: vec![1],
            fill: Fill::FormatDefault,
            unit_parameters: vec![],
        };
        let mut map = ChunkMap::default();
        let rows = vec![
            Row { array: 1, coords: vec![], element: Some(4), ..Row::default() },
            Row { array: 0, coords: vec![1, 0], element: Some(3), member: Some(2), params: vec![("p.quantize.scale".into(), Scalar::Float64(0.5))], ..Row::default() },
            Row { array: 0, coords: vec![0, 0], element: Some(3), member: Some(0), params: vec![("p.quantize.scale".into(), Scalar::Float64(2.0))], ..Row::default() },
            Row { array: 0, coords: vec![2, 0], ranges: Some(vec![(None, 40, 4), (Some("beside.bin".into()), 0, 4)]), skips: Some(1), ..Row::default() },
            Row { array: 0, coords: vec![0, 1], element: Some(1), range: Some((2, 4)), chain: Some(1), ..Row::default() },
        ];
        for r in rows {
            map.push(r).unwrap();
        }
        map.sort();
        Stored { arrays: vec![base, scalar, ungridded, empty], chains, map, ..Stored::default() }
    }

    fn with(stored: Stored) -> Ir {
        let mut ir = file_ir();
        ir.stored = std::sync::Arc::new(stored);
        ir
    }

    fn refusal(f: impl FnOnce(&mut Stored)) -> String {
        let mut s = valid();
        f(&mut s);
        check_with(&with(s), TEST_CODECS).unwrap_err()
    }

    /// The list names each codec once, and a codec each of its parameters once: two formats' PRs
    /// that add one codec must add one entry.
    #[test]
    fn the_list_names_each_codec_and_parameter_once() {
        let mut names: Vec<&str> = CODECS.iter().map(|c| c.name).collect();
        names.sort_unstable();
        assert_eq!(names.windows(2).find(|w| w[0] == w[1]), None);
        for c in CODECS {
            let mut parameters: Vec<&str> = c.parameters.iter().map(|p| p.name).collect();
            parameters.sort_unstable();
            assert_eq!(parameters.windows(2).find(|w| w[0] == w[1]), None, "{}", c.name);
        }
    }

    /// Valid descriptions and maps of every kind pass; the map is sorted when built, and each
    /// grid's axes count their units; its JSON form and the Arrow batch carry the same rows.
    #[test]
    fn valid_descriptions_and_maps() {
        let s = valid();
        let ir = with(s.clone());
        check_with(&ir, TEST_CODECS).unwrap();
        let coords: Vec<(u32, Vec<u64>)> = (0..s.map.len()).map(|k| (s.map.array[k], s.map.coords_of(k).to_vec())).collect();
        assert_eq!(coords, vec![(0, vec![0, 0]), (0, vec![0, 1]), (0, vec![1, 0]), (0, vec![2, 0]), (1, vec![])]);
        let g = s.arrays[0].grid.as_ref().unwrap();
        assert_eq!((units(&g.axes[0], 25), units(&g.axes[1], 12)), (3, 2));
        assert_eq!(units(&AxisUnits::Each(16), 0), 0);
        let doc = s.document(&ir);
        assert_eq!(doc["arrays"][0]["path"], json!("header"));
        assert_eq!(doc["arrays"][0]["grid"], json!({"axes": [{"list": [5, [10, 2]]}, {"each": 8}], "edge": "full"}));
        assert_eq!(doc["arrays"][1]["fill"], json!({"given": {"source": {"element": 3, "member": 1, "range": [0, 8]}}}));
        assert_eq!(doc["chains"][0][0], json!({"name": "quantize", "parameters": {"method": {"value": 1}}}));
        assert_eq!(doc["arrays"][2]["fill"], json!("undefined"));
        assert_eq!(doc["arrays"][3]["fill"], json!("format_default"));
        // elements `source` makes need no size
        let mut sourced = valid();
        (sourced.arrays[3].item_bits, sourced.arrays[3].chains, sourced.arrays[3].fill) = (None, vec![2], Fill::Undefined);
        check_with(&with(sourced), TEST_CODECS).unwrap();
    }

    #[cfg(feature = "arrow")]
    #[test]
    fn the_arrow_batch_holds_the_rows() {
        use arrow_array::Array;
        let s = valid();
        let b = arrow::batch(&s.map).unwrap();
        assert_eq!(b.num_rows(), 5);
        assert_eq!(b.schema().metadata()["refkv.chunk_map"], CHUNK_MAP_VERSION);
        let names: Vec<String> = b.schema().fields().iter().map(|f| f.name().clone()).collect();
        assert_eq!(names, ["array", "coords", "element", "member", "range", "ranges", "chain", "skips", "p.quantize.scale"]);
        assert_eq!(b.column(4).null_count(), 4);
        assert_eq!(b.column(5).null_count(), 4);
        assert_eq!(b.column(8).null_count(), 3);
    }

    #[test]
    fn a_graft_renumbers_and_names_the_parsed_source() {
        let file = with(valid());
        let mut store = Ir::new_store();
        let objs = store.listing(&[("a/f.bin".into(), 100), ("a/beside.bin".into(), 4)]).unwrap();
        let at = store.graft(&file, objs[0]).unwrap();
        // a second graft of the same file: its chains are the first's
        let at2 = store.graft(&file, objs[0]).unwrap();
        let s = &store.stored;
        assert_eq!(s.chains.len(), 3);
        assert_eq!(s.arrays.len(), 8);
        assert_eq!(s.arrays[0].element, at[1]);
        assert_eq!(s.arrays[4].element, at2[1]);
        assert_eq!(s.arrays[1].declarations[0], Place::Extent { object: Some("a/f.bin".into()), offset: 0, length: 8 });
        assert_eq!(s.map.len(), 10);
        assert_eq!(s.map.array[5..], [4, 4, 4, 4, 5]);
        assert_eq!(s.map.element[0], Some(at[3]));
        assert_eq!((s.map.element[1], s.map.range[1], s.map.range[6]), (Some(at[1]), Some((2, 4)), Some((2, 4))));
        let k = s.map.ranges_of(3).unwrap();
        let keys: Vec<&str> = s.map.range_object[k].iter().map(|o| s.map.objects[o.unwrap() as usize].as_str()).collect();
        assert_eq!(keys, ["a/f.bin", "beside.bin"]);
    }

    #[test]
    fn an_unfold_moves_a_members_rows_and_places() {
        let mut ir = with(valid());
        let before = ir.len() as u32;
        ir.unfold(3).unwrap();
        let s = &ir.stored;
        // member 2 of the run (root 3, subtree [3, 4)) is the second copy
        assert_eq!((s.map.element[2], s.map.member[2]), (Some(before + 1), None));
        assert_eq!((s.map.element[0], s.map.member[0]), (Some(3), None));
        assert_eq!(s.map.range[1], Some((2, 4)));
        assert_eq!(s.arrays[1].fill, Fill::Given(Param::Source(Place::Element { element: before, member: None, range: Some((0, 8)) })));
    }

    #[test]
    fn mirror_canonical_leaves_the_arrays_out() {
        let ir = with(valid());
        assert!(crate::mirror::canonical(&ir).unwrap().stored.is_empty());
        assert!(!ir.stored.is_empty());
    }

    #[test]
    fn describe_sorts_and_checks_the_rows() {
        let mut ir = file_ir();
        let s = valid();
        let order: Vec<usize> = (0..s.map.len()).rev().collect();
        // the IR's list has no `quantize`: a description of the test list's codecs is refused,
        // and the IR keeps none
        ir.describe(s.arrays, s.chains, s.map.take(&order)).unwrap_err();
        assert!(ir.stored.is_empty());
        let mut map = ChunkMap::default();
        map.push(Row { array: 1, coords: vec![], element: Some(4), ..Row::default() }).unwrap();
        map.push(Row { array: 0, coords: vec![0], element: Some(1), ..Row::default() }).unwrap();
        let one = StoredArray { element: 1, index: None, shape: Some(vec![8]), grid: Some(Grid { axes: vec![AxisUnits::Each(8)], edge: Edge::Short }), chains: vec![0], ..valid().arrays[1].clone() };
        let scalar = StoredArray { index: None, chains: vec![0], ..valid().arrays[1].clone() };
        ir.describe(vec![one, scalar], vec![vec![]], map).unwrap();
        assert_eq!(ir.stored.map.element, [Some(1), Some(4)]);
    }

    // ---- one test per refusal of each check, and of describing and building

    #[test]
    fn refuses_to_describe_an_unfinished_ir() {
        let mut ir = Ir::new(10);
        assert_eq!(ir.describe(vec![], vec![], ChunkMap::default()).unwrap_err(), "internal: arrays described before the IR is finished");
    }

    #[test]
    fn refuses_to_describe_twice() {
        let mut ir = file_ir();
        let a = StoredArray { index: None, ..valid().arrays[2].clone() };
        ir.describe(vec![a.clone()], vec![], ChunkMap::default()).unwrap();
        assert_eq!(ir.describe(vec![a], vec![], ChunkMap::default()).unwrap_err(), "internal: the IR's arrays described twice");
    }

    /// An array of 2000 listed runs keyed by `element`, of chains `chains`, with a declaration: its
    /// fallen-back description costs 3 entries, and its full one 2000 and a chain's more.
    fn heavy(element: u32, chains: Vec<u32>) -> StoredArray {
        let runs: Vec<Run> = (0..2000).map(|k| Run::One(1 + k % 2)).collect();
        StoredArray { element, index: None, shape: Some(vec![3000]), grid: Some(Grid { axes: vec![AxisUnits::List(runs)], edge: Edge::Short }), chains, fill: Fill::Undefined, ..valid().arrays[1].clone() }
    }

    /// An array of four units of a byte each, of chain `chain`, keyed by `element`, with its rows
    /// (array `a`, element 4): its full description costs one entry more than its fallen-back one.
    fn light(element: u32, chain: u32, a: u32, map: &mut ChunkMap) -> StoredArray {
        for k in 0..4u64 {
            map.push(Row { array: a, coords: vec![k], element: Some(4), range: Some((k, 1)), ..Row::default() }).unwrap();
        }
        StoredArray {
            element, index: None, declarations: vec![], shape: Some(vec![4]), axes: None, item_bits: Some(8), pad_from: None,
            grid: Some(Grid { axes: vec![AxisUnits::Each(1)], edge: Edge::Short }), chains: vec![chain], fill: Fill::Undefined,
            unit_parameters: vec![],
        }
    }

    /// Describe fits the descriptions to their limit, never refusing: three arrays (a heavy one
    /// with a chain and a row of its own into another object, then two light ones sharing the
    /// empty chain) cost 3, 2 and 2 entries fallen back, and the light ones 2 and 1 more (the
    /// shared chain charged once, to the first). With room for exactly that, the heavy one falls back,
    /// its row, object key and chain dropped, and the light ones keep their grids; with one entry
    /// less, the second light one falls back too; with less than
    /// the fallen-back total, the source keeps none and is marked, which changes its digest; a
    /// store parser's own describe that keeps none lists each object whose arrays it described.
    #[test]
    fn the_descriptions_limit_falls_back_at_describe() {
        let source = vec![codec("source", &[("format", Param::Value(json!("hdf5")))])];
        let build = || {
            let mut map = ChunkMap::default();
            map.push(Row { array: 0, coords: vec![0], ranges: Some(vec![(Some("other.bin".into()), 0, 1)]), ..Row::default() }).unwrap();
            let b = light(2, 0, 1, &mut map);
            let c = light(4, 0, 2, &mut map);
            (vec![heavy(1, vec![1]), b, c], vec![vec![], source.clone()], map)
        };
        let described = |room: u64| {
            let mut ir = file_ir();
            ir.budget.as_mut().unwrap().descriptions = room;
            let (arrays, chains, map) = build();
            ir.describe(arrays, chains, map).unwrap();
            ir
        };
        let ir = described(10);
        let s = &ir.stored;
        assert!(!s.undescribed);
        assert!(s.arrays[0].grid.is_none() && s.arrays[0].chains.is_empty());
        assert!(s.arrays[1].grid.is_some() && s.arrays[2].grid.is_some());
        assert_eq!(s.chains, vec![Vec::<Codec>::new()]);
        assert_eq!(s.map.array, [1, 1, 1, 1, 2, 2, 2, 2]);
        assert!(s.map.objects.is_empty());
        assert_eq!(ir.budget.as_ref().unwrap().spent_descriptions, 10);
        assert_eq!(s.weights().0, 10);
        // one entry less: the first light array pays for the shared chain, and the second falls back
        let short = described(9);
        assert!(short.stored.arrays[1].grid.is_some() && short.stored.arrays[2].grid.is_none());
        // the whole description fits with room to spare
        assert!(described(10_000).stored.arrays.iter().all(|a| a.grid.is_some()));
        // less than the fallen-back total: none kept, the source marked
        let marked = described(6);
        assert!(marked.stored.undescribed && marked.stored.arrays.is_empty() && marked.stored.map.is_empty());
        assert_eq!(marked.budget.as_ref().unwrap().spent_descriptions, 0);
        assert_eq!(marked.stored.document(&marked)["undescribed"], json!(true));
        assert_ne!(marked.digest(), file_ir().digest());
        check(&marked).unwrap();
        // a store's own describe: two arrays of one object, past the limit
        let mut own = Ir::new_store();
        let o = own.listing(&[("x.raw".into(), 100), ("y.raw".into(), 100)]).unwrap();
        let header = own.value(o[1], "header", NO_INDEX, "bytes[8]", 0, (0, 8), None).unwrap();
        crate::check::finish(&mut own).unwrap();
        own.budget.as_mut().unwrap().descriptions = own.budget.as_ref().unwrap().spent_descriptions;
        let a = |index: u64| StoredArray { element: header, index: Some(index), ..valid().arrays[2].clone() };
        own.describe(vec![a(0), a(1)], vec![], ChunkMap::default()).unwrap();
        assert_eq!(own.stored.undescribed_keys, ["y.raw"]);
        assert!(!own.stored.undescribed);
    }

    /// A graft fits the file's descriptions to what remains of the store's limit as describe
    /// does, a chain the store already has costing nothing: a heavy array falls back and a light
    /// one keeps its grid where the room is its fallen-back cost plus one. A file marked at its own
    /// describe, and one whose fallen-back descriptions do not fit, join with no descriptions,
    /// rows or chains, their keys listed; the lists change the store's digest.
    #[test]
    fn the_descriptions_limit_falls_back_at_graft() {
        let source = vec![codec("source", &[("format", Param::Value(json!("hdf5")))])];
        let mut store = Ir::new_store();
        let o = store.listing(&[("a.bin".into(), 100), ("b.bin".into(), 100), ("c.bin".into(), 100), ("d.bin".into(), 100)]).unwrap();
        // a: one light array of the source chain, which the store's table then has
        let mut first = file_ir();
        let mut map = ChunkMap::default();
        let a = light(4, 0, 0, &mut map);
        first.describe(vec![a], vec![source.clone()], map).unwrap();
        store.graft(&first, o[0]).unwrap();
        // b: a heavy array of a new chain, then a light one of the source chain
        let mut second = file_ir();
        let mut map = ChunkMap::default();
        let x = light(4, 1, 1, &mut map);
        second.describe(vec![heavy(1, vec![0]), x], vec![vec![], source.clone()], map).unwrap();
        let b = store.budget.as_mut().unwrap();
        b.descriptions = b.spent_descriptions + 3 + 2 + 1;
        store.graft(&second, o[1]).unwrap();
        let s = &store.stored;
        assert!(s.arrays[1].grid.is_none() && s.arrays[2].grid.is_some());
        assert_eq!((s.chains.clone(), s.arrays[2].chains.clone()), (vec![source.clone()], vec![0]));
        assert_eq!(s.map.array, [0, 0, 0, 0, 2, 2, 2, 2]);
        assert!(s.undescribed_keys.is_empty());
        // c: a file marked at its describe; d: no room left at all
        let mut marked = file_ir();
        marked.budget.as_mut().unwrap().descriptions = 0;
        let mut map = ChunkMap::default();
        let a = light(4, 0, 0, &mut map);
        marked.describe(vec![a.clone()], vec![vec![]], map.clone()).unwrap();
        assert!(marked.stored.undescribed);
        let mut whole = file_ir();
        whole.describe(vec![a], vec![vec![]], map).unwrap();
        let before = store.digest();
        store.graft(&marked, o[2]).unwrap();
        let b = store.budget.as_mut().unwrap();
        b.descriptions = b.spent_descriptions;
        store.graft(&whole, o[3]).unwrap();
        let s = &store.stored;
        assert_eq!((s.arrays.len(), s.map.len(), s.chains.len()), (3, 8, 1));
        assert_eq!(s.undescribed_keys, ["c.bin", "d.bin"]);
        assert_eq!(s.document(&store)["undescribed"], json!(["c.bin", "d.bin"]));
        assert_ne!(store.digest(), before);
        crate::check::finish(&mut store).unwrap();
    }

    /// The chunk map's limit is 2^22 plus four entries per byte of the source, so a map of one
    /// unit of rank 2 per byte, each held by a range of one byte (four entries a row), fits even
    /// without the 2^22.
    #[test]
    fn a_map_of_a_rank_2_unit_per_byte_fits() {
        assert_eq!(Budget::new(100).rows, (1 << 22) + 400);
        let mut ir = file_ir();
        ir.budget.as_mut().unwrap().rows = 400;
        let mut map = ChunkMap::default();
        for k in 0..100u64 {
            map.push(Row { array: 0, coords: vec![k / 10, k % 10], ranges: Some(vec![(None, k, 1)]), ..Row::default() }).unwrap();
        }
        let a = StoredArray {
            element: 1, index: None, declarations: vec![], shape: Some(vec![10, 10]), axes: None, item_bits: Some(8), pad_from: None,
            grid: Some(Grid { axes: vec![AxisUnits::Each(1), AxisUnits::Each(1)], edge: Edge::Short }), chains: vec![0],
            fill: Fill::Undefined, unit_parameters: vec![],
        };
        ir.describe(vec![a], vec![vec![]], map).unwrap();
        assert_eq!(ir.budget.as_ref().unwrap().spent_rows, 400);
    }

    /// Two arrays of four units each, of rank 1 (eight entries each), held by elements, as the
    /// IR's codec list allows: `first` keyed by the header (element 1), `second` by the chunks
    /// struct (element 2).
    fn two_arrays() -> (Vec<StoredArray>, ChunkMap) {
        let array = |element: u32| StoredArray {
            element, index: None, declarations: vec![], shape: Some(vec![4]), axes: None, item_bits: Some(8), pad_from: None,
            grid: Some(Grid { axes: vec![AxisUnits::Each(1)], edge: Edge::Short }), chains: vec![0], fill: Fill::Undefined,
            unit_parameters: vec![],
        };
        let mut map = ChunkMap::default();
        for a in 0..2u32 {
            for k in 0..4u64 {
                map.push(Row { array: a, coords: vec![k], element: Some(4), range: Some((k, 1)), ..Row::default() }).unwrap();
            }
        }
        (vec![array(1), array(2)], map)
    }

    /// The map's limit never rejects a run: arrays in order keep their rows while they fit, and the
    /// second, whose rows would pass the limit, falls back (no grid, chains or rows).
    #[test]
    fn an_array_past_the_maps_limit_falls_back_and_the_first_keeps_its_rows() {
        let mut ir = file_ir();
        ir.budget.as_mut().unwrap().rows = 12;
        let (arrays, map) = two_arrays();
        ir.describe(arrays, vec![vec![]], map).unwrap();
        let s = &ir.stored;
        assert_eq!((s.arrays[0].grid.is_some(), s.arrays[1].grid.is_none(), s.arrays[1].chains.len()), (true, true, 0));
        assert_eq!(s.map.array, [0, 0, 0, 0]);
        assert_eq!(ir.budget.as_ref().unwrap().spent_rows, 8);
    }

    /// A map past its limit from its first array on: every array falls back, and the run goes on.
    #[test]
    fn a_map_past_its_limit_falls_back_whole() {
        let mut ir = file_ir();
        ir.budget.as_mut().unwrap().rows = 4;
        let (arrays, map) = two_arrays();
        ir.describe(arrays, vec![vec![]], map).unwrap();
        assert!(ir.stored.arrays.iter().all(|a| a.grid.is_none() && a.chains.is_empty()));
        assert!(ir.stored.map.is_empty());
        assert_eq!(ir.budget.as_ref().unwrap().spent_rows, 0);
    }

    /// An array of one row of many ranges past the limit falls back too: each `ranges` item is an
    /// entry.
    #[test]
    fn an_array_of_ranges_past_the_maps_limit_falls_back() {
        let mut ir = file_ir();
        ir.budget.as_mut().unwrap().rows = 1000;
        let mut map = ChunkMap::default();
        map.push(Row { array: 0, coords: vec![], ranges: Some(vec![(None, 0, 1); 2000]), ..Row::default() }).unwrap();
        let a = StoredArray { element: 1, index: None, chains: vec![0], fill: Fill::Undefined, ..valid().arrays[1].clone() };
        ir.describe(vec![a], vec![vec![]], map).unwrap();
        assert!(ir.stored.arrays[0].grid.is_none() && ir.stored.map.is_empty());
    }

    /// Two files whose maps each fit alone: grafted into one store, the second file's array, whose
    /// rows would take the store's map past its limit, falls back there, though it kept them in its
    /// file.
    #[test]
    fn a_second_files_array_falls_back_in_a_store_its_rows_do_not_fit() {
        let file = |element: u32| {
            let mut f = file_ir();
            let (mut arrays, mut map) = two_arrays();
            arrays.truncate(1);
            arrays[0].element = element;
            map = map.take(&(0..4).collect::<Vec<usize>>());
            f.describe(arrays, vec![vec![]], map).unwrap();
            f
        };
        let (one, two) = (file(1), file(2));
        assert!(one.stored.arrays[0].grid.is_some() && two.stored.arrays[0].grid.is_some());
        let mut store = Ir::new_store();
        let o = store.listing(&[("f.bin".into(), 100), ("g.bin".into(), 100)]).unwrap();
        let b = store.budget.as_mut().unwrap();
        b.rows = b.spent_rows + 12;
        store.graft(&one, o[0]).unwrap();
        store.graft(&two, o[1]).unwrap();
        let s = &store.stored;
        assert_eq!((s.arrays[0].grid.is_some(), s.arrays[1].grid.is_none()), (true, true));
        assert_eq!(s.map.array, [0, 0, 0, 0]);
        crate::check::finish(&mut store).unwrap();
    }

    /// Two files whose chains name one offset of their own source (`object: None`) keep two chains
    /// in the store: a graft compares chains only after their null keys are the files' keys.
    #[test]
    fn two_files_chains_at_one_offset_of_their_sources_stay_two() {
        let chain = vec![codec("source", &[("parameters", Param::Source(Place::Extent { object: None, offset: 8, length: 4 }))])];
        let (mut arrays, map) = two_arrays();
        arrays.truncate(1);
        let file = with(Stored { arrays, chains: vec![chain], map: map.take(&(0..4).collect::<Vec<usize>>()), ..Stored::default() });
        let mut store = Ir::new_store();
        let o = store.listing(&[("f.bin".into(), 100), ("g.bin".into(), 100)]).unwrap();
        store.graft(&file, o[0]).unwrap();
        store.graft(&file, o[1]).unwrap();
        let s = &store.stored;
        assert_eq!(s.chains.len(), 2);
        let object = |c: &Chain| match c[0].parameters.get("parameters") {
            Some(Param::Source(Place::Extent { object, .. })) => object.clone(),
            _ => None,
        };
        assert_eq!((object(&s.chains[0]), object(&s.chains[1])), (Some("f.bin".to_string()), Some("g.bin".to_string())));
        assert_eq!((s.arrays[0].chains.clone(), s.arrays[1].chains.clone()), (vec![0], vec![1]));
    }

    /// Describe sorts a file IR's arrays by `(element, index)`, their rows following them; in a
    /// store, each graft's arrays keep their relative order after those already there, so a
    /// store's arrays are in graft order. A file's root becomes its object element, whose id is
    /// below the earlier grafts' ids: a description it keys, grafted second, comes second, and the
    /// store is not sorted again.
    #[test]
    fn the_arrays_are_in_key_order_in_a_file_and_in_graft_order_in_a_store() {
        let (mut arrays, mut map) = two_arrays();
        arrays.reverse();
        // the second array's rows to element 1, the first's to element 4, to follow them
        for k in 0..map.len() {
            map.element[k] = Some(if map.array[k] == 0 { 1 } else { 4 });
            map.range[k] = None;
        }
        let mut file = file_ir();
        file.describe(arrays, vec![vec![]], map).unwrap();
        let s = &file.stored;
        assert_eq!(s.arrays.iter().map(|a| a.element).collect::<Vec<_>>(), [1, 2]);
        assert_eq!(s.map.array, [0, 0, 0, 0, 1, 1, 1, 1]);
        assert_eq!(s.map.element[..4], [Some(4); 4]);
        let mut store = Ir::new_store();
        let o = store.listing(&[("f.bin".into(), 100), ("g.bin".into(), 100)]).unwrap();
        store.graft(&file, o[0]).unwrap();
        store.graft(&file, o[1]).unwrap();
        let keys: Vec<(u32, Option<u64>)> = store.stored.arrays.iter().map(|a| (a.element, a.index)).collect();
        assert!(keys.windows(2).all(|w| w[0] < w[1]), "{keys:?}");
        // a description keyed by a file's root, grafted after the others
        let root = StoredArray { element: 0, index: None, shape: Some(vec![2]), grid: None, chains: vec![], ..valid().arrays[2].clone() };
        let rooted = with(Stored { arrays: vec![root], ..Stored::default() });
        let mut store2 = Ir::new_store();
        let o2 = store2.listing(&[("f.bin".into(), 100), ("g.bin".into(), 100)]).unwrap();
        store2.graft(&file, o2[0]).unwrap();
        store2.graft(&rooted, o2[1]).unwrap();
        let s = &store2.stored;
        assert_eq!(s.arrays.iter().map(|a| a.element).collect::<Vec<_>>()[2], o2[1]);
        assert!(s.arrays[2].element < s.arrays[0].element);
        check_with(&store2, TEST_CODECS).unwrap();
        crate::check::finish(&mut store2).unwrap();
        assert_eq!(store2.stored.arrays[2].element, o2[1]);
    }

    /// A `range` of its element's whole extent is legal where the element's recipe is not the
    /// identity: the element alone gives the recipe's bytes, and the range the raw extent.
    #[test]
    fn a_whole_extent_range_of_an_element_of_another_recipe() {
        // the data element (element 4, 60 bytes) under each recipe that is not the identity
        for recipe in [
            r#"[["shared",0],["src",0,null]]"#,
            r#"[["src",4,null]]"#,
            r#"[["src",0,30],["src",31,null]]"#,
            r#"[["rows",0,12,10,5]]"#,
            r#"[["src",0,30],["src",0,30]]"#,
            r#"[["src",0,59]]"#,
            r#"[["src",0,18446744073709551615],["src",18446744073709551615,61]]"#,
            r#"[["rows",0,18446744073709551615,18446744073709551615,2]]"#,
            r#"[["src",0,30],["rows",30,18446744073709551615,18446744073709551615,1]]"#,
            r#"[["src",0,null,"more"]]"#,
            r#"[["rows",0,60,60,1,"more"]]"#,
            r#"[["src",0,null],["src",100,null]]"#,
        ] {
            let mut ir = with(valid());
            let f = ir.forms.id(&format!(r#"{{"recipe":{recipe}}}"#));
            let k = ir.form_of.binary_search_by_key(&4, |e| e.0).unwrap();
            ir.form_of[k].1 = f;
            std::sync::Arc::make_mut(&mut ir.stored).map.range[4] = Some((0, 60));
            check_with(&ir, TEST_CODECS).unwrap_or_else(|e| panic!("{recipe}: {e}"));
        }
    }

    /// An array that falls back leaves no parameter column of its own, nor object key, in a file
    /// IR's map at describe and in a store's at graft; a graft with no room left in the
    /// descriptions' limit adds no description, row or column, and lists the file's key. A column only a fallen-back array lists is
    /// skipped though the store has one of its name and another type, the store's rows padded with
    /// nulls in it; one that a fallen-back and a kept array of a file both list survives.
    #[test]
    fn a_fallback_leaves_no_column_or_key_of_its_own() {
        let quantize = vec![codec("quantize", &[])];
        let (mut arrays, _) = two_arrays();
        arrays[1].chains = vec![1];
        arrays[1].unit_parameters = vec![UnitParameter { chain: 0, codec: 0, name: "scale".into() }];
        let mut map = ChunkMap::default();
        for k in 0..4u64 {
            map.push(Row { array: 0, coords: vec![k], element: Some(4), range: Some((k, 1)), ..Row::default() }).unwrap();
        }
        for k in 0..4u64 {
            let params = vec![("p.quantize.scale".into(), Scalar::Float64(1.0))];
            map.push(Row { array: 1, coords: vec![k], ranges: Some(vec![(Some("other.bin".into()), k, 1)]), params, ..Row::default() }).unwrap();
        }
        // describe: the second array's 16 entries do not fit in 12 after the first's 8
        let mut ir = file_ir();
        ir.budget.as_mut().unwrap().rows = 12;
        ir.describe(arrays.clone(), vec![vec![], quantize.clone()], map.clone()).unwrap();
        assert!(ir.stored.arrays[1].grid.is_none());
        assert!(ir.stored.map.params.is_empty() && ir.stored.map.objects.is_empty());
        // graft: a file that kept both arrays, into a store with room for the first alone
        let file = with(Stored { arrays, chains: vec![vec![], quantize], map, ..Stored::default() });
        let mut store = Ir::new_store();
        let o = store.listing(&[("f.bin".into(), 100), ("g.bin".into(), 100)]).unwrap();
        let b = store.budget.as_mut().unwrap();
        b.rows = b.spent_rows + 12;
        store.graft(&file, o[0]).unwrap();
        assert!(store.stored.arrays[1].grid.is_none());
        assert!(store.stored.map.params.is_empty() && store.stored.map.objects.is_empty());
        assert_eq!(store.stored.chains, vec![Vec::<Codec>::new()]);
        crate::check::finish(&mut store).unwrap();
        store.budget.as_mut().unwrap().descriptions = store.budget.as_ref().unwrap().spent_descriptions;
        let (arrays, rows) = (store.stored.arrays.len(), store.stored.map.len());
        store.graft(&file, o[1]).unwrap();
        assert_eq!((store.stored.arrays.len(), store.stored.map.len()), (arrays, rows));
        assert_eq!(store.stored.undescribed_keys, ["g.bin"]);

        // the columns of a graft whose array falls back, in a store whose rows already have one of
        // their names: each array of `rows` units (element 4, a byte each), listing `p.quantize.scale`
        // when `scale` gives its value
        let array = |element: u32, n: u64, chain: u32, listed: bool| StoredArray {
            element, index: None, declarations: vec![], shape: Some(vec![n]), axes: None, item_bits: Some(8), pad_from: None,
            grid: Some(Grid { axes: vec![AxisUnits::Each(1)], edge: Edge::Short }), chains: vec![chain], fill: Fill::Undefined,
            unit_parameters: if listed { vec![UnitParameter { chain: 0, codec: 0, name: "scale".into() }] } else { vec![] },
        };
        let rows = |map: &mut ChunkMap, a: u32, n: u64, scale: Option<Scalar>| {
            for k in 0..n {
                let params = scale.map(|x| vec![("p.quantize.scale".to_string(), x)]).unwrap_or_default();
                map.push(Row { array: a, coords: vec![k], element: Some(4), range: Some((k, 1)), params, ..Row::default() }).unwrap();
            }
        };
        let quantize = || vec![codec("quantize", &[])];
        let mut store = Ir::new_store();
        let o = store.listing(&[("a".into(), 100), ("b".into(), 100), ("c".into(), 100)]).unwrap();
        let room = |store: &mut Ir, n: u64| {
            let b = store.budget.as_mut().unwrap();
            b.rows = b.spent_rows + n;
        };
        // a: one kept array giving the column as Float64
        let mut map = ChunkMap::default();
        rows(&mut map, 0, 4, Some(Scalar::Float64(1.0)));
        let a = with(Stored { arrays: vec![array(1, 4, 0, true)], chains: vec![quantize()], map, ..Stored::default() });
        store.graft(&a, o[0]).unwrap();
        // b: a kept array of no column, and one that falls back whose column of that name is UInt64;
        // the graft succeeds, the store's column keeps its type, and b's kept rows are null in it
        let mut map = ChunkMap::default();
        rows(&mut map, 0, 4, None);
        rows(&mut map, 1, 4, Some(Scalar::UInt64(7)));
        let b = with(Stored { arrays: vec![array(1, 4, 0, false), array(2, 4, 1, true)], chains: vec![vec![], quantize()], map, ..Stored::default() });
        room(&mut store, 8);
        store.graft(&b, o[1]).unwrap();
        let col = |store: &Ir| store.stored.map.params["p.quantize.scale"].clone();
        let mut want = vec![Some(1.0); 4];
        want.extend([None; 4]);
        assert_eq!(col(&store), Column::Float64(want.clone()));
        assert!(store.stored.arrays[2].grid.is_none());
        // c: two arrays listing the column, the first (heavier) falling back, the second kept: the
        // column keeps the second's values
        let mut map = ChunkMap::default();
        rows(&mut map, 0, 5, Some(Scalar::Float64(2.0)));
        rows(&mut map, 1, 4, Some(Scalar::Float64(3.0)));
        let c = with(Stored { arrays: vec![array(1, 5, 0, true), array(2, 4, 0, true)], chains: vec![quantize()], map, ..Stored::default() });
        room(&mut store, 12);
        store.graft(&c, o[2]).unwrap();
        let s = &store.stored;
        assert!(s.arrays[3].grid.is_none() && s.arrays[4].grid.is_some());
        want.extend([Some(3.0); 4]);
        assert_eq!(col(&store), Column::Float64(want));
        check_with(&store, TEST_CODECS).unwrap();
    }

    /// A graft appends the chains new to the store's table in the order the file's remaining
    /// descriptions first name them, leaving the table's chains where they are; the map's entries
    /// count each set unit-parameter value.
    #[test]
    fn a_graft_appends_new_chains_in_order_of_first_use() {
        let source = |id: u64| vec![codec("source", &[("id", Param::Value(json!(id)))])];
        let (mut arrays, map) = two_arrays();
        arrays[0].chains = vec![2];
        arrays[1].chains = vec![1];
        let file = with(Stored { arrays, chains: vec![source(0), source(1), source(2)], map, ..Stored::default() });
        let mut store = Ir::new_store();
        let o = store.listing(&[("f.bin".into(), 100), ("g.bin".into(), 100)]).unwrap();
        store.graft(&file, o[0]).unwrap();
        let s = &store.stored;
        assert_eq!(s.chains, vec![source(2), source(1)]);
        assert_eq!((s.arrays[0].chains.clone(), s.arrays[1].chains.clone()), (vec![0], vec![1]));
        assert_eq!(valid().weights().1, 5 + 8 + 2 + 2);
    }

    /// A store's limits grow as it lists its objects, as its records do, and not as files are
    /// grafted: a graft fits its file to the limit as it stands, every listed object counted,
    /// grafted or not.
    #[test]
    fn a_graft_fits_to_the_limit_the_listing_grew() {
        let mut file = file_ir();
        let (mut arrays, map) = two_arrays();
        arrays.truncate(1);
        file.describe(arrays, vec![vec![]], map.take(&(0..4).collect::<Vec<usize>>())).unwrap();
        let mut store = Ir::new_store();
        let o = store.listing(&[("f.bin".into(), 0), ("g.bin".into(), 1000)]).unwrap();
        let b = store.budget.as_ref().unwrap();
        assert_eq!((b.rows, b.descriptions), ((1 << 22) + 4 + (4 + 4000), (1 << 22) + 4 + (4 + 250)));
        let before = b.rows;
        // the file, grafted under the empty object, fits in what the large one's listing grew
        store.budget.as_mut().unwrap().rows -= 1 << 22;
        store.graft(&file, o[0]).unwrap();
        assert!(store.stored.arrays[0].grid.is_some());
        assert_eq!(store.budget.as_ref().unwrap().rows, before - (1 << 22));
    }

    /// A chain no description names after fitting is dropped from the table, the others
    /// renumbered: in a file IR at describe, and in a store at graft.
    #[test]
    fn a_chain_only_a_fallen_back_array_named_is_dropped() {
        let source = vec![codec("source", &[("format", Param::Value(json!("hdf5")))])];
        let (mut arrays, map) = two_arrays();
        arrays[0].chains = vec![1];
        let mut ir = file_ir();
        ir.budget.as_mut().unwrap().rows = 12;
        ir.describe(arrays.clone(), vec![vec![], source.clone()], map.clone()).unwrap();
        assert_eq!(ir.stored.chains, vec![source.clone()]);
        assert_eq!((ir.stored.arrays[0].chains.clone(), ir.stored.arrays[1].chains.clone()), (vec![0], vec![]));
        let mut file = file_ir();
        file.describe(arrays, vec![vec![], source.clone()], map).unwrap();
        assert_eq!(file.stored.chains.len(), 2);
        let mut store = Ir::new_store();
        let o = store.listing(&[("f.bin".into(), 100)]).unwrap();
        let b = store.budget.as_mut().unwrap();
        b.rows = b.spent_rows + 12;
        store.graft(&file, o[0]).unwrap();
        assert_eq!(store.stored.chains, vec![source]);
        assert_eq!((store.stored.arrays[0].chains.clone(), store.stored.arrays[1].chains.clone()), (vec![0], vec![]));
        crate::check::finish(&mut store).unwrap();
    }

    /// A graft past the store's map limit: the file's arrays that fit keep their rows, the next
    /// falls back, and the graft goes on.
    #[test]
    fn a_graft_past_the_maps_limit_falls_back() {
        let mut file = file_ir();
        let (arrays, map) = two_arrays();
        file.describe(arrays, vec![vec![]], map).unwrap();
        let mut store = Ir::new_store();
        let o = store.listing(&[("f.bin".into(), 100)]).unwrap();
        let b = store.budget.as_mut().unwrap();
        b.rows = b.spent_rows + 12;
        store.graft(&file, o[0]).unwrap();
        let s = &store.stored;
        assert_eq!((s.arrays[0].grid.is_some(), s.arrays[1].grid.is_none()), (true, true));
        assert_eq!(s.map.array, [0, 0, 0, 0]);
        crate::check::finish(&mut store).unwrap();
    }

    #[test]
    fn a_refused_description_refunds_the_budget_and_check_finish_leaves_the_ir_unfinished() {
        // one fault: the description names an element the IR has not
        let a = StoredArray { element: 99, index: None, ..valid().arrays[2].clone() };
        let mut ir = file_ir();
        let e = ir.describe(vec![a.clone()], vec![], ChunkMap::default()).unwrap_err();
        assert_eq!(e, "internal: the stored array of element 99: names element 99, which the IR has not");
        let b = ir.budget.as_ref().unwrap();
        assert_eq!((b.spent_descriptions, b.spent_rows), (0, 0));
        let mut store = Ir::new_store();
        let o = store.listing(&[("f.bin".into(), 100)]).unwrap();
        store.graft(&file_ir(), o[0]).unwrap();
        let a = StoredArray { element: 9999, ..a };
        store.stored = std::sync::Arc::new(Stored { arrays: vec![a], ..Stored::default() });
        let e = crate::check::finish(&mut store).unwrap_err();
        assert_eq!(e, "internal: the stored array of element 9999: names element 9999, which the IR has not");
        assert!(!store.finished);
    }

    /// A store's failed graft, refused by its arrays' plan or by the IR's room, leaves it as it was
    /// (the descriptions' limit no longer refuses one: its file falls back).
    #[test]
    fn a_failed_graft_leaves_the_store_unchanged() {
        let chain = |object: Option<&str>| vec![codec("quantize", &[("method", Param::Source(Place::Extent { object: object.map(String::from), offset: 0, length: 4 }))])];
        let a = StoredArray {
            element: 1, index: None, declarations: vec![], shape: Some(vec![2]), axes: None, item_bits: Some(8), pad_from: None,
            grid: Some(Grid { axes: vec![AxisUnits::Each(1)], edge: Edge::Short }), chains: vec![0, 1], fill: Fill::Undefined,
            unit_parameters: vec![UnitParameter { chain: 0, codec: 0, name: "scale".into() }],
        };
        let mut map = ChunkMap::default();
        map.push(Row { array: 0, coords: vec![0], element: Some(1), params: vec![("p.quantize.scale".into(), Scalar::Float64(1.0))], ..Row::default() }).unwrap();
        map.push(Row { array: 0, coords: vec![1], element: Some(4), chain: Some(1), ..Row::default() }).unwrap();
        let refused = with(Stored { arrays: vec![a], chains: vec![chain(None), chain(Some("f.bin"))], map, ..Stored::default() });
        let mut store = Ir::new_store();
        let o = store.listing(&[("f.bin".into(), 100), ("g.bin".into(), 100)]).unwrap();
        store.graft(&with(valid()), o[1]).unwrap();
        let state = |s: &Ir| (s.len(), s.digest(), s.stored.as_ref().clone(), s.budget.as_ref().map(|b| (b.spent_records, b.spent_elements, b.spent_descriptions, b.spent_rows)));
        let before = state(&store);
        assert!(store.graft(&refused, o[0]).unwrap_err().contains("they give other unit parameters"));
        assert!(state(&store) == before);
        let b = store.budget.as_mut().unwrap();
        b.records = b.spent_records;
        let before = state(&store);
        assert_eq!(store.graft(&with(valid()), o[0]).unwrap_err(), format!("budget: more than {} IR records", before.3.unwrap().0));
        assert!(state(&store) == before);
    }

    /// An unfold that moves a chain's place clears the chain index, which the next graft makes
    /// again from the chains as they are.
    #[test]
    fn a_graft_after_an_unfold_indexes_the_chains_as_they_are() {
        let chain = vec![codec("source", &[("parameters", Param::Source(Place::Element { element: 3, member: Some(2), range: None }))])];
        // an array of no rows names the chain, which a graft would otherwise drop
        let a = StoredArray {
            element: 1, index: None, declarations: vec![], shape: Some(vec![1]), axes: None, item_bits: Some(8), pad_from: None,
            grid: Some(Grid { axes: vec![AxisUnits::Each(1)], edge: Edge::Short }), chains: vec![0], fill: Fill::Undefined,
            unit_parameters: vec![],
        };
        let file = with(Stored { arrays: vec![a], chains: vec![chain], ..Stored::default() });
        let mut store = Ir::new_store();
        let o = store.listing(&[("f.bin".into(), 100), ("g.bin".into(), 100)]).unwrap();
        let at = store.graft(&file, o[0]).unwrap();
        let before = chain_json(&store.stored.chains[0]).to_string();
        store.unfold(at[3]).unwrap();
        assert_ne!(chain_json(&store.stored.chains[0]).to_string(), before);
        assert!(store.stored.chain_index.0.is_empty());
        store.graft(&file, o[1]).unwrap();
        let s = &store.stored;
        assert_eq!(s.chains.len(), 2);
        for (k, c) in s.chains.iter().enumerate() {
            assert_eq!(s.chain_index.0.get(&chain_json(c).to_string()), Some(&(k as u32)));
        }
        check_with(&store, TEST_CODECS).unwrap();
    }

    /// A column for every listed unit parameter, though every row skips its codec, or the map has
    /// no rows.
    #[test]
    fn every_listed_unit_parameter_has_its_column() {
        let mut s = valid();
        s.map.params.clear();
        s.map.skips[0] = Some(1);
        s.map.skips[2] = Some(1);
        s.add_columns(TEST_CODECS, 0);
        assert_eq!(s.map.params["p.quantize.scale"], Column::Float64(vec![None; 5]));
        check_with(&with(s), TEST_CODECS).unwrap();
        let mut s = valid();
        s.arrays.truncate(1);
        s.map = ChunkMap::default();
        s.add_columns(TEST_CODECS, 0);
        assert_eq!(s.map.params["p.quantize.scale"], Column::Float64(vec![]));
        check_with(&with(s), TEST_CODECS).unwrap();
    }

    #[test]
    fn refuses_a_listed_unit_parameter_without_its_column() {
        assert!(refusal(|s| {
            s.map.params.clear();
            s.map.skips[0] = Some(1);
            s.map.skips[2] = Some(1);
        }).contains("the chunk map has no column \"p.quantize.scale\""));
    }

    #[test]
    fn a_graft_makes_two_chains_it_renumbers_alike_one() {
        let source = |object: Option<&str>| vec![codec("source", &[("parameters", Param::Source(Place::Extent { object: object.map(String::from), offset: 0, length: 4 }))])];
        let a = StoredArray {
            element: 1, index: None, declarations: vec![], shape: Some(vec![2]), axes: None, item_bits: Some(8), pad_from: None,
            grid: Some(Grid { axes: vec![AxisUnits::Each(1)], edge: Edge::Short }), chains: vec![0, 1], fill: Fill::Undefined, unit_parameters: vec![],
        };
        let mut map = ChunkMap::default();
        map.push(Row { array: 0, coords: vec![0], element: Some(1), ..Row::default() }).unwrap();
        map.push(Row { array: 0, coords: vec![1], element: Some(4), chain: Some(1), ..Row::default() }).unwrap();
        let file = with(Stored { arrays: vec![a], chains: vec![source(None), source(Some("f.bin"))], map, ..Stored::default() });
        check_with(&file, TEST_CODECS).unwrap();
        let mut store = Ir::new_store();
        let o = store.listing(&[("f.bin".into(), 100)]).unwrap();
        store.graft(&file, o[0]).unwrap();
        assert_eq!(store.stored.chains.len(), 1);
        assert_eq!(store.stored.arrays[0].chains, [0]);
        assert_eq!(store.stored.map.chain, [None, None]);
        check_with(&store, TEST_CODECS).unwrap();
    }

    #[test]
    fn refuses_a_graft_that_makes_chains_of_other_unit_parameters_one() {
        let chain = |object: Option<&str>| vec![codec("quantize", &[("method", Param::Source(Place::Extent { object: object.map(String::from), offset: 0, length: 4 }))])];
        let a = StoredArray {
            element: 1, index: None, declarations: vec![], shape: Some(vec![2]), axes: None, item_bits: Some(8), pad_from: None,
            grid: Some(Grid { axes: vec![AxisUnits::Each(1)], edge: Edge::Short }), chains: vec![0, 1], fill: Fill::Undefined,
            unit_parameters: vec![UnitParameter { chain: 0, codec: 0, name: "scale".into() }],
        };
        let mut map = ChunkMap::default();
        map.push(Row { array: 0, coords: vec![0], element: Some(1), params: vec![("p.quantize.scale".into(), Scalar::Float64(1.0))], ..Row::default() }).unwrap();
        map.push(Row { array: 0, coords: vec![1], element: Some(4), chain: Some(1), ..Row::default() }).unwrap();
        let file = with(Stored { arrays: vec![a], chains: vec![chain(None), chain(Some("f.bin"))], map, ..Stored::default() });
        check_with(&file, TEST_CODECS).unwrap();
        let mut store = Ir::new_store();
        let o = store.listing(&[("f.bin".into(), 100)]).unwrap();
        assert!(store.graft(&file, o[0]).unwrap_err().contains("they give other unit parameters"));
    }

    /// Under Full, units of one length but a shorter last one stay a List: Each would say the
    /// last is stored full.
    #[test]
    fn a_full_grid_keeps_a_shorter_last_unit_listed() {
        let mut s = valid();
        s.arrays[0].grid.as_mut().unwrap().axes[0] = AxisUnits::List(vec![Run::Repeat(10, 2), Run::One(5)]);
        check_with(&with(s.clone()), TEST_CODECS).unwrap();
        s.arrays[0].grid.as_mut().unwrap().edge = Edge::Short;
        assert!(check_with(&with(s), TEST_CODECS).unwrap_err().contains("lists units that are Each(10)"));
    }

    #[test]
    fn a_dotted_codec_names_its_parameter_columns() {
        let mut s = valid();
        s.chains.push(vec![codec("acme.zfp", &[])]);
        s.arrays[1].chains = vec![3];
        s.arrays[1].unit_parameters = vec![UnitParameter { chain: 0, codec: 0, name: "rate".into() }];
        s.map.params.insert("p.acme.zfp.rate".into(), Column::Float64(vec![None, None, None, None, Some(0.25)]));
        check_with(&with(s), TEST_CODECS).unwrap();
    }

    #[test]
    fn refuses_a_description_keyed_by_an_element_in_a_run() {
        assert!(refusal(|s| s.arrays[2].element = 3).contains("is keyed by an element in the run"));
    }

    #[test]
    fn refuses_an_element_range_past_its_element() {
        assert!(refusal(|s| s.arrays[0].declarations = vec![Place::Element { element: 1, member: None, range: Some((4, 8)) }]).contains("past its 8 bytes"));
    }

    #[test]
    fn refuses_an_element_range_past_2_to_the_64() {
        assert!(refusal(|s| s.arrays[0].declarations = vec![Place::Element { element: 1, member: None, range: Some((u64::MAX, 2)) }]).contains("names a range at 18446744073709551615 of 2 bytes"));
    }

    #[test]
    fn refuses_an_extent_past_the_parsed_source() {
        assert!(refusal(|s| s.arrays[0].declarations = vec![Place::Extent { object: None, offset: 96, length: 8 }]).contains("names [96, 104) of the parsed source, past its 100 bytes"));
    }

    #[test]
    fn refuses_an_extent_past_2_to_the_64() {
        assert!(refusal(|s| s.arrays[0].declarations = vec![Place::Extent { object: Some("x".into()), offset: u64::MAX, length: 1 }]).contains("past 2^64"));
    }

    #[test]
    fn refuses_an_extent_past_a_store_object() {
        let mut store = Ir::new_store();
        let o = store.listing(&[("f.bin".into(), 100)]).unwrap();
        store.graft(&file_ir(), o[0]).unwrap();
        let a = StoredArray { element: 1, index: None, declarations: vec![Place::Extent { object: Some("f.bin".into()), offset: 99, length: 2 }], ..valid().arrays[2].clone() };
        store.stored = std::sync::Arc::new(Stored { arrays: vec![a], ..Stored::default() });
        assert!(check_with(&store, TEST_CODECS).unwrap_err().contains("names [99, 101) of \"f.bin\", past its 100 bytes"));
    }

    #[test]
    fn refuses_a_row_range_past_the_parsed_source() {
        assert!(refusal(|s| s.map.range_offset[0] = 98).contains("a range names [98, 102) of the parsed source, past its 100 bytes"));
    }

    #[test]
    fn refuses_empty_ranges() {
        assert!(refusal(|s| {
            s.map.range_object.clear();
            s.map.range_offset.clear();
            s.map.range_length.clear();
            s.map.ranges_end = vec![0; 5];
        }).contains("its ranges are empty"));
    }

    #[test]
    fn refuses_a_range_0_bytes_long() {
        assert!(refusal(|s| s.map.range_length[0] = 0).contains("its range at 40 is 0 bytes long"));
    }

    #[test]
    fn refuses_a_chain_table_with_a_chain_twice() {
        assert!(refusal(|s| s.chains.push(vec![])).contains("chains 1 and 3 of the chain table are one"));
    }

    #[test]
    fn refuses_a_description_naming_a_chain_twice() {
        assert!(refusal(|s| s.arrays[3].chains = vec![1, 1]).contains("its chains name chain 1 twice"));
    }

    #[test]
    fn refuses_a_unit_parameter_listed_twice() {
        assert!(refusal(|s| { let u = s.arrays[0].unit_parameters[0].clone(); s.arrays[0].unit_parameters.push(u); }).contains("is listed twice"));
    }

    #[test]
    fn refuses_axes_in_storage_order() {
        assert!(refusal(|s| s.arrays[0].axes = Some(vec![0, 1])).contains("are storage order, which no axes say"));
    }

    #[test]
    fn refuses_a_parameter_value_of_another_type() {
        let mut map = ChunkMap::default();
        map.push(Row { params: vec![("p.quantize.scale".into(), Scalar::Float64(1.0))], ..Row::default() }).unwrap();
        let row = Row { coords: vec![1], params: vec![("p.quantize.scale".into(), Scalar::UInt64(1))], ..Row::default() };
        assert_eq!(map.push(row).unwrap_err(), "internal: a parameter column given a value of another type");
    }

    #[test]
    fn refuses_an_element_the_ir_has_not() {
        assert!(refusal(|s| s.arrays[2].element = 99).contains("names element 99, which the IR has not"));
    }

    #[test]
    fn refuses_a_declaration_of_an_element_the_ir_has_not() {
        assert!(refusal(|s| s.arrays[0].declarations = vec![el(99)]).contains("a declaration names element 99"));
    }

    #[test]
    fn refuses_a_member_of_an_element_in_no_run() {
        assert!(refusal(|s| s.arrays[0].declarations = vec![Place::Element { element: 1, member: Some(0), range: None }]).contains("which is in no run"));
    }

    #[test]
    fn refuses_an_element_in_a_run_without_its_member() {
        assert!(refusal(|s| s.arrays[0].declarations = vec![el(3)]).contains("without its member"));
    }

    #[test]
    fn refuses_a_member_past_its_run() {
        assert!(refusal(|s| s.arrays[0].declarations = vec![Place::Element { element: 3, member: Some(3), range: None }]).contains("whose run has 3"));
    }

    /// The fallback codec `source`, with every parameter the IR's list gives it: the format, the
    /// source's id and own name for the encoding, and its parameters.
    #[test]
    fn the_source_codec_carries_the_sources_name() {
        let c = codec("source", &[("format", Param::Value(json!("hdf5"))), ("id", Param::Value(json!(32001))), ("name", Param::Value(json!("blosc"))), ("parameters", Param::Value(json!([2, 2, 4])))]);
        check(&with(Stored { chains: vec![vec![c]], ..Stored::default() })).unwrap();
    }

    /// A source that declares no extent: its description has no shape (JSON null), no grid, no
    /// axes and no pad_from, and is valid; it counts no axes.
    #[test]
    fn a_description_of_no_extent() {
        let a = StoredArray { element: 1, index: None, shape: None, ..valid().arrays[2].clone() };
        let s = Stored { arrays: vec![a], ..Stored::default() };
        check_with(&with(s.clone()), TEST_CODECS).unwrap();
        assert_eq!(s.arrays[0].to_json()["shape"], J::Null);
        assert_eq!(s.weights().0, 2);
    }

    #[test]
    fn refuses_a_grid_without_an_extent() {
        assert!(refusal(|s| s.arrays[0].shape = None).contains("its source declares no extent, and it has a grid"));
    }

    #[test]
    fn refuses_axes_without_an_extent() {
        assert!(refusal(|s| { s.arrays[2].shape = None; s.arrays[2].axes = Some(vec![]); }).contains("its source declares no extent, and it has axes"));
    }

    #[test]
    fn refuses_pad_from_without_an_extent() {
        assert!(refusal(|s| { s.arrays[2].shape = None; s.arrays[2].pad_from = Some(0); }).contains("its source declares no extent, and it has pad_from"));
    }

    #[test]
    fn refuses_a_grid_of_another_rank() {
        assert!(refusal(|s| s.arrays[0].shape = Some(vec![25, 12, 1])).contains("its grid has 2 axes, its shape 3"));
    }

    #[test]
    fn refuses_units_0_long() {
        assert!(refusal(|s| s.arrays[0].grid.as_mut().unwrap().axes[1] = AxisUnits::Each(0)).contains("axis 1's units are 0 long"));
    }

    #[test]
    fn refuses_short_units_past_the_axis() {
        assert!(refusal(|s| s.arrays[3].grid.as_mut().unwrap().axes[1] = AxisUnits::Each(8)).contains("past the axis's 7"));
    }

    #[test]
    fn refuses_a_listed_unit_0_long() {
        assert!(refusal(|s| s.arrays[3].grid.as_mut().unwrap().axes[1] = AxisUnits::List(vec![Run::One(7), Run::One(0)])).contains("lists a unit 0 long"));
    }

    #[test]
    fn refuses_a_repeat_of_one() {
        assert!(refusal(|s| s.arrays[3].grid.as_mut().unwrap().axes[1] = AxisUnits::List(vec![Run::Repeat(3, 1), Run::One(4)])).contains("a run of 1 units 3 long, not One"));
    }

    #[test]
    fn refuses_runs_of_one_length_side_by_side() {
        let a = AxisUnits::List(vec![Run::Repeat(10, 2), Run::One(10), Run::One(5)]);
        assert!(refusal(|s| { s.arrays[0].shape.as_mut().unwrap()[0] = 35; s.arrays[0].grid.as_mut().unwrap().axes[0] = a }).contains("two runs of units 10 long side by side"));
    }

    #[test]
    fn refuses_a_list_that_does_not_sum_to_the_axis() {
        assert!(refusal(|s| s.arrays[0].shape.as_mut().unwrap()[0] = 26).contains("do not sum to its 26"));
    }

    #[test]
    fn refuses_a_list_that_is_each() {
        assert!(refusal(|s| s.arrays[3].grid.as_mut().unwrap().axes[1] = AxisUnits::List(vec![Run::One(4), Run::One(3)])).contains("lists units that are Each(4)"));
    }

    #[test]
    fn refuses_a_grid_without_chains() {
        assert!(refusal(|s| s.arrays[3].chains = vec![]).contains("it has a grid and no chain"));
    }

    #[test]
    fn refuses_chains_without_a_grid() {
        assert!(refusal(|s| s.arrays[2].chains = vec![1]).contains("it has chains and no grid"));
    }

    #[test]
    fn refuses_a_chain_past_the_table() {
        assert!(refusal(|s| s.arrays[3].chains = vec![3]).contains("its chain 3 is past the chain table's 3"));
    }

    #[test]
    fn refuses_axes_that_are_no_permutation() {
        assert!(refusal(|s| s.arrays[0].axes = Some(vec![1, 1])).contains("are not a permutation of its 2"));
    }

    #[test]
    fn refuses_a_grid_without_item_bits() {
        assert!(refusal(|s| s.arrays[3].item_bits = None).contains("it has a grid and no item_bits"));
    }

    #[test]
    fn refuses_a_grid_without_item_bits_where_one_chain_does_not_begin_with_source() {
        let unsized_ = |s: &mut Stored| (s.arrays[0].item_bits, s.arrays[0].pad_from, s.arrays[0].fill) = (None, None, Fill::Undefined);
        assert!(refusal(unsized_).contains("a chain that does not begin with `source`"));
    }

    #[test]
    fn refuses_a_given_fill_without_item_bits() {
        assert!(refusal(|s| s.arrays[2].fill = Fill::Given(Param::Value(json!("AA==")))).contains("its fill is given and it has no item_bits"));
    }

    #[test]
    fn refuses_item_bits_of_0() {
        assert!(refusal(|s| s.arrays[3].item_bits = Some(0)).contains("its item_bits is 0"));
    }

    #[test]
    fn refuses_pad_from_without_item_bits() {
        assert!(refusal(|s| s.arrays[2].pad_from = Some(0)).contains("it has pad_from and no item_bits"));
    }

    #[test]
    fn refuses_pad_from_of_whole_bytes() {
        assert!(refusal(|s| s.arrays[3].pad_from = Some(0)).contains("with item_bits 8, whole bytes"));
    }

    #[test]
    fn refuses_pad_from_past_the_rank() {
        assert!(refusal(|s| s.arrays[0].pad_from = Some(2)).contains("its pad_from 2 is not below its rank 2"));
    }

    #[test]
    fn refuses_a_fill_value_that_is_not_base64() {
        assert!(refusal(|s| s.arrays[0].fill = Fill::Given(Param::Value(json!("A?A=")))).contains("its fill value is not base64"));
    }

    #[test]
    fn refuses_a_fill_value_that_is_not_a_string() {
        assert!(refusal(|s| s.arrays[0].fill = Fill::Given(Param::Value(json!(0)))).contains("its fill value is not a base64 string"));
    }

    #[test]
    fn refuses_a_fill_value_of_another_length() {
        assert!(refusal(|s| s.arrays[0].fill = Fill::Given(Param::Value(json!("AAAA")))).contains("its fill value is 3 bytes, not 2"));
    }

    #[test]
    fn refuses_a_fill_place_the_ir_has_not() {
        assert!(refusal(|s| s.arrays[1].fill = Fill::Given(Param::Source(el(99)))).contains("its fill names element 99"));
    }

    #[test]
    fn refuses_a_unit_parameter_of_a_chain_it_has_not() {
        assert!(refusal(|s| s.arrays[0].unit_parameters[0].chain = 2).contains("names its chain 2, which it has not"));
    }

    #[test]
    fn refuses_a_unit_parameter_of_a_codec_past_its_chain() {
        assert!(refusal(|s| s.arrays[0].unit_parameters[0].codec = 2).contains("names codec 2 of a chain of 2"));
    }

    #[test]
    fn refuses_a_unit_parameter_of_a_codec_its_chain_has_twice() {
        assert!(refusal(|s| s.chains[0].push(codec("quantize", &[]))).contains("which its chain has twice"));
    }

    #[test]
    fn refuses_a_unit_parameter_its_codec_does_not_allow_as_a_column() {
        assert!(refusal(|s| s.arrays[0].unit_parameters[0].name = "method".into()).contains("is not one the codec quantize allows as a column"));
    }

    #[test]
    fn refuses_a_unit_parameter_its_codec_gives_itself() {
        assert!(refusal(|s| { s.chains[0][0].parameters.insert("scale".into(), Param::Value(json!(1.0))); }).contains("is one the codec quantize gives itself"));
    }

    #[test]
    fn refuses_two_arrays_of_one_key() {
        assert!(refusal(|s| s.arrays[2].index = Some(0)).contains("two have one index"));
    }

    #[test]
    fn refuses_an_element_with_and_without_indexes() {
        assert!(refusal(|s| s.arrays[2].index = None).contains("some have an index and some have none"));
    }

    #[test]
    fn refuses_indexes_that_are_not_0_to_n() {
        assert!(refusal(|s| s.arrays[2].index = Some(2)).contains("their indexes are not 0 to 1"));
    }

    #[test]
    fn refuses_a_codec_the_list_has_not() {
        assert!(refusal(|s| s.chains[1].push(codec("lzma", &[]))).contains("has the codec \"lzma\", which the IR's list has not"));
    }

    #[test]
    fn refuses_a_codec_parameter_its_entry_has_not() {
        assert!(refusal(|s| { s.chains[0][1].parameters.insert("speed".into(), Param::Value(json!(1))); }).contains("has the parameter \"speed\", which its entry has not"));
    }

    #[test]
    fn refuses_a_codec_parameter_place_the_ir_has_not() {
        assert!(refusal(|s| { s.chains[2][0].parameters.insert("parameters".into(), Param::Source(el(99))); }).contains("parameter \"parameters\" names element 99"));
    }

    #[test]
    fn refuses_a_parsed_source_in_a_store() {
        let mut store = Ir::new_store();
        let o = store.listing(&[("f.bin".into(), 100)]).unwrap();
        store.graft(&file_ir(), o[0]).unwrap();
        crate::check::finish(&mut store).unwrap();
        let mut s = Stored { arrays: vec![valid().arrays[2].clone()], ..Stored::default() };
        s.arrays[0].element = 1;
        s.arrays[0].index = None;
        s.arrays[0].declarations = vec![Place::Extent { object: None, offset: 0, length: 1 }];
        store.stored = std::sync::Arc::new(s);
        assert!(check_with(&store, TEST_CODECS).unwrap_err().contains("names the parsed source (a null key) in a store IR"));
    }

    #[test]
    fn refuses_an_object_the_store_has_not() {
        let mut store = Ir::new_store();
        let o = store.listing(&[("f.bin".into(), 100)]).unwrap();
        store.graft(&with(valid()), o[0]).unwrap();
        // the row of grafted ranges names "beside.bin", which this store has not
        assert!(check_with(&store, TEST_CODECS).unwrap_err().contains("a range names the object \"beside.bin\", which the store has not"));
    }

    #[test]
    fn refuses_map_columns_of_two_lengths() {
        assert!(refusal(|s| { s.map.chain.pop(); }).contains("columns are not all of one length"));
    }

    #[test]
    fn refuses_map_offsets_that_do_not_match() {
        assert!(refusal(|s| { s.map.coords.push(0); }).contains("list offsets do not match"));
    }

    #[test]
    fn refuses_ranges_in_a_null_row() {
        assert!(refusal(|s| s.map.has_ranges[3] = false).contains("has ranges in a null row"));
    }

    #[test]
    fn refuses_an_object_past_the_dictionary() {
        assert!(refusal(|s| s.map.range_object[1] = Some(5)).contains("names an object past its dictionary"));
    }

    #[test]
    fn refuses_a_column_of_no_codec() {
        assert!(refusal(|s| { s.map.params.insert("p.squash.speed".into(), Column::UInt64(vec![None; 5])); }).contains("is no codec's parameter column"));
    }

    #[test]
    fn refuses_a_column_of_another_type() {
        assert!(refusal(|s| { s.map.params.insert("p.quantize.scale".into(), Column::UInt64(vec![Some(1), None, Some(1), None, None])); }).contains("is a UInt64, not Float64"));
    }

    #[test]
    fn refuses_a_row_of_an_array_past_the_list() {
        assert!(refusal(|s| s.map.array[4] = 9).contains("is of array 9, past the 4 described"));
    }

    #[test]
    fn refuses_a_row_of_an_array_with_no_grid() {
        assert!(refusal(|s| s.map.array[4] = 2).contains("the array has no grid"));
    }

    #[test]
    fn refuses_coordinates_of_another_rank() {
        assert!(refusal(|s| { s.map.coords.push(0); s.map.coords_end[4] += 1; }).contains("are not of the array's rank 0"));
    }

    #[test]
    fn refuses_coordinates_outside_the_grid() {
        assert!(refusal(|s| s.map.coords[6] = 3).contains("are outside the grid on axis 0"));
    }

    #[test]
    fn refuses_repeated_coordinates() {
        assert!(refusal(|s| s.map.coords[3] = 0).contains("its coordinates [0, 0] repeat"));
    }

    #[test]
    fn refuses_rows_out_of_order() {
        assert!(refusal(|s| s.map.coords[4] = 0).contains("it is out of order"));
    }

    #[test]
    fn refuses_a_row_with_element_and_ranges() {
        assert!(refusal(|s| s.map.element[3] = Some(4)).contains("both its element and its ranges are set"));
    }

    #[test]
    fn refuses_a_row_with_neither_element_nor_ranges() {
        assert!(refusal(|s| s.map.element[4] = None).contains("neither its element nor its ranges is set"));
    }

    #[test]
    fn refuses_a_row_element_the_ir_has_not() {
        assert!(refusal(|s| s.map.element[4] = Some(99)).contains("it names element 99, which the IR has not"));
    }

    #[test]
    fn refuses_a_row_member_of_an_element_in_no_run() {
        assert!(refusal(|s| s.map.member[4] = Some(0)).contains("which is in no run"));
    }

    #[test]
    fn refuses_a_row_element_in_a_run_without_its_member() {
        assert!(refusal(|s| s.map.member[0] = None).contains("without its member"));
    }

    #[test]
    fn refuses_a_row_member_past_its_run() {
        assert!(refusal(|s| s.map.member[0] = Some(3)).contains("whose run has 3"));
    }

    #[test]
    fn refuses_a_row_range_without_an_element() {
        assert!(refusal(|s| s.map.range[3] = Some((0, 1))).contains("it has a range of an element and no element"));
    }

    #[test]
    fn refuses_a_row_range_0_bytes_long() {
        assert!(refusal(|s| s.map.range[1] = Some((2, 0))).contains("its range at 2 of its element is 0 bytes long"));
    }

    #[test]
    fn refuses_a_row_range_past_its_element() {
        assert!(refusal(|s| s.map.range[1] = Some((6, 4))).contains("its range at 6 of 4 bytes is past its element's 8 bytes"));
    }

    #[test]
    fn refuses_a_row_range_of_the_whole_element() {
        // the header (element 1) has no form
        assert!(refusal(|s| s.map.range[1] = Some((0, 8))).contains("its range is its element's whole extent, which the element alone names"));
        // the data element (element 4, 60 bytes) under each recipe that is the identity
        for recipe in [
            r#"[["src",0,null]]"#,
            r#"[["src",0]]"#,
            r#"[["src",0,60]]"#,
            r#"[["src",0,30],["src",30,null]]"#,
            r#"[["rows",0,10,10,6]]"#,
            r#"[["rows",0,99,60,1]]"#,
            r#"null"#,
            r#"[["src",0,0],["src",0,60]]"#,
            r#"[["src",0,null],["src",5,0]]"#,
            r#"[["src",0,null],["rows",60,5,0,3]]"#,
        ] {
            let mut ir = with(valid());
            let f = ir.forms.id(&format!(r#"{{"recipe":{recipe}}}"#));
            let k = ir.form_of.binary_search_by_key(&4, |e| e.0).unwrap();
            ir.form_of[k].1 = f;
            std::sync::Arc::make_mut(&mut ir.stored).map.range[4] = Some((0, 60));
            assert!(check_with(&ir, TEST_CODECS).unwrap_err().contains("its range is its element's whole extent"), "{recipe}");
        }
    }

    #[test]
    fn refuses_a_row_range_past_2_to_the_64() {
        assert!(refusal(|s| s.map.range[1] = Some((u64::MAX, 2))).contains("is past its element's 8 bytes"));
    }

    #[test]
    fn refuses_a_row_member_without_an_element() {
        assert!(refusal(|s| s.map.member[3] = Some(0)).contains("it has a member and no element"));
    }

    #[test]
    fn refuses_chain_0_written_as_a_value() {
        assert!(refusal(|s| s.map.chain[4] = Some(0)).contains("its chain is 0, not null"));
    }

    #[test]
    fn refuses_skips_of_nothing_written_as_a_value() {
        assert!(refusal(|s| s.map.skips[4] = Some(0)).contains("it skips nothing, not null"));
    }

    #[test]
    fn refuses_a_chain_past_the_arrays() {
        assert!(refusal(|s| s.map.chain[4] = Some(1)).contains("its chain 1 is past the array's 1"));
    }

    #[test]
    fn refuses_skips_past_the_chain() {
        assert!(refusal(|s| s.map.skips[3] = Some(0b100)).contains("name codecs past its chain's 2"));
    }

    #[test]
    fn refuses_a_parameter_column_null_where_its_array_gives_it() {
        assert!(refusal(|s| { s.map.params.insert("p.quantize.scale".into(), Column::Float64(vec![None, None, Some(1.0), None, None])); }).contains("is null, but its array gives it"));
    }

    #[test]
    fn refuses_a_parameter_column_set_on_a_skipped_codec() {
        assert!(refusal(|s| { s.map.params.insert("p.quantize.scale".into(), Column::Float64(vec![Some(1.0), None, Some(1.0), Some(1.0), None])); }).contains("is set, but its codec is skipped"));
    }

    #[test]
    fn refuses_a_parameter_column_set_on_another_chain() {
        assert!(refusal(|s| { s.map.params.insert("p.quantize.scale".into(), Column::Float64(vec![Some(1.0), Some(1.0), Some(1.0), None, None])); }).contains("its array does not give it for its chain"));
    }

    /// A store holding `valid()` grafted under `a/f.bin`, and `beside.bin` and `c.bin` listed,
    /// with `f` applied to its stored arrays: the check's refusal.
    fn store_refusal(f: impl FnOnce(&mut Stored)) -> String {
        let mut store = Ir::new_store();
        let o = store.listing(&[("a/f.bin".into(), 100), ("beside.bin".into(), 4), ("c.bin".into(), 10)]).unwrap();
        store.graft(&with(valid()), o[0]).unwrap();
        f(std::sync::Arc::make_mut(&mut store.stored));
        check_with(&store, TEST_CODECS).unwrap_err()
    }

    /// Another object's rows may read the bytes of an object marked `undescribed`: `valid()`'s
    /// rows read `beside.bin`, which is listed.
    #[test]
    fn an_undescribed_objects_bytes_may_be_read_by_another_objects_rows() {
        let mut store = Ir::new_store();
        let o = store.listing(&[("a/f.bin".into(), 100), ("beside.bin".into(), 4)]).unwrap();
        store.graft(&with(valid()), o[0]).unwrap();
        std::sync::Arc::make_mut(&mut store.stored).undescribed_keys.push("beside.bin".into());
        check_with(&store, TEST_CODECS).unwrap();
    }

    #[test]
    fn refuses_a_store_parsers_own_array_in_no_object() {
        let mut own = Ir::new_store();
        own.listing(&[("x.raw".into(), 100)]).unwrap();
        crate::check::finish(&mut own).unwrap();
        let a = StoredArray { element: 0, index: None, ..valid().arrays[2].clone() };
        assert_eq!(own.describe(vec![a], vec![], ChunkMap::default()).unwrap_err(), "internal: a store's own stored array of element 0 (\"\") lies in no object");
    }

    #[test]
    fn refuses_a_file_ir_listing_undescribed_objects() {
        assert_eq!(refusal(|s| s.undescribed_keys.push("x".into())), "internal: a file IR lists objects as undescribed");
    }

    #[test]
    fn refuses_a_source_marked_undescribed_that_keeps_descriptions() {
        assert_eq!(refusal(|s| s.undescribed = true), "internal: a source marked undescribed keeps descriptions");
    }

    #[test]
    fn refuses_a_store_marked_undescribed_itself() {
        assert!(store_refusal(|s| s.undescribed = true).contains("marked undescribed itself"));
    }

    #[test]
    fn refuses_an_undescribed_key_of_no_object() {
        assert!(store_refusal(|s| s.undescribed_keys.push("nope".into())).contains("\"nope\" is no object of the store"));
    }

    #[test]
    fn refuses_an_undescribed_key_listed_twice() {
        assert!(store_refusal(|s| s.undescribed_keys.extend(["c.bin".into(), "c.bin".into()])).contains("\"c.bin\" is listed twice"));
    }

    #[test]
    fn refuses_a_description_in_an_undescribed_object() {
        assert!(store_refusal(|s| s.undescribed_keys.push("a/f.bin".into())).contains("is in an object marked undescribed"));
    }

    #[test]
    fn refuses_a_row_element_in_an_undescribed_object() {
        assert!(store_refusal(|s| {
            s.arrays.clear();
            s.undescribed_keys.push("a/f.bin".into());
        })
        .contains("in an object marked undescribed"));
    }
}
