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
use std::collections::{BTreeMap, HashMap};

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
    /// the axes in storage order, the slowest-varying first
    pub shape: Vec<u64>,
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
    /// the source defines none
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

/// The IR's codecs. `source` is the general fallback: a source encoding refkv has no name for,
/// by the format, the source's id for it and its parameters as the source gives them.
pub const CODECS: &[CodecEntry] = &[CodecEntry {
    name: "source",
    parameters: &[
        ParamEntry { name: "format", column: None },
        ParamEntry { name: "id", column: None },
        ParamEntry { name: "parameters", column: None },
    ],
}];

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
    fn append(&mut self, from: &ChunkMap, base: u32, at: &dyn Fn(u32) -> u32, key: &str, locals: &[Vec<u32>]) {
        let n = self.len();
        let objects: Vec<u32> = from.objects.iter().map(|o| self.object(o)).collect();
        let before = self.objects.len();
        let parsed = self.object(key);
        let mut used = false;
        for k in 0..from.len() {
            self.array.push(from.array[k] + base);
            self.coords.extend_from_slice(from.coords_of(k));
            self.coords_end.push(self.coords.len() as u64);
            self.element.push(from.element[k].map(at));
            self.member.push(from.member[k]);
            self.range.push(from.range[k]);
            self.has_ranges.push(from.has_ranges[k]);
            if let Some(r) = from.ranges_of(k) {
                for x in r {
                    used |= from.range_object[x].is_none();
                    self.range_object.push(Some(from.range_object[x].map_or(parsed, |o| objects[o as usize])));
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
        for (name, col) in &from.params {
            self.params.entry(name.clone()).or_insert_with(|| Column::of(col.kind(), n));
        }
        for (name, col) in self.params.iter_mut() {
            // a column of one name is of one type (Stored::plan_graft): no push fails
            match from.params.get(name) {
                Some(c) => {
                    for k in 0..from.len() {
                        let _ = col.push(c.get(k));
                    }
                }
                None => {
                    for _ in 0..from.len() {
                        let _ = col.push(None);
                    }
                }
            }
        }
        // the parsed source's key, when no row named it
        if self.objects.len() > before && !used {
            if let Some(o) = self.objects.pop() {
                self.object_index.0.remove(&o);
            }
        }
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
pub struct GraftPlan {
    added: Vec<(Chain, String)>,
    arrays: Vec<StoredArray>,
    locals: Vec<Vec<u32>>,
}

/// An IR's stored arrays: the descriptions, the chain table they name and the chunk map.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Stored {
    pub arrays: Vec<StoredArray>,
    pub chains: Vec<Chain>,
    /// each chain's position in `chains`, which a graft looks the file's chains up in
    pub chain_index: ChainIndex,
    pub map: ChunkMap,
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
    pub fn is_empty(&self) -> bool {
        self.arrays.is_empty() && self.chains.is_empty() && self.map.is_empty()
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
    /// set of unit parameters), and the map's parameter columns of one name of one type.
    pub fn plan_graft(&self, from: &Stored, at: &dyn Fn(u32) -> u32, key: &str) -> Res<GraftPlan> {
        let mut added: Vec<(Chain, String)> = Vec::new();
        let mut chains = Vec::with_capacity(from.chains.len());
        for c in &from.chains {
            let mut c = c.clone();
            for p in c.iter_mut().flat_map(|x| x.parameters.values_mut().filter_map(Param::places)) {
                p.renumber(at, key);
            }
            let text = chain_json(&c).to_string();
            let k = match self.chain_index.0.get(&text) {
                Some(&k) => k,
                None => match added.iter().position(|a| a.1 == text) {
                    Some(j) => (self.chains.len() + j) as u32,
                    None => {
                        added.push((c, text));
                        (self.chains.len() + added.len() - 1) as u32
                    }
                },
            };
            chains.push(k);
        }
        let mut arrays = Vec::with_capacity(from.arrays.len());
        let mut locals = Vec::with_capacity(from.arrays.len());
        for a in &from.arrays {
            let mut a = a.clone();
            a.renumber(at, key);
            let mut mine: Vec<u32> = Vec::with_capacity(a.chains.len());
            let local: Vec<u32> = a
                .chains
                .iter()
                .map(|&c| {
                    let c = chains.get(c as usize).copied().unwrap_or(c);
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
        for (name, col) in &from.map.params {
            if let Some(mine) = self.map.params.get(name) {
                if mine.kind() != col.kind() {
                    return Err(format!("internal: a graft joins the parameter column {name:?} of type {:?} to one of {:?}", col.kind(), mine.kind()));
                }
            }
        }
        Ok(GraftPlan { added, arrays, locals })
    }

    /// The graft [`Stored::plan_graft`] planned, of the file's arrays `from` with the same `at`
    /// and `key`: it cannot fail.
    pub fn graft(&mut self, plan: GraftPlan, from: &Stored, at: &dyn Fn(u32) -> u32, key: &str) {
        self.index_chains();
        for (c, text) in plan.added {
            self.chain_index.0.insert(text, self.chains.len() as u32);
            self.chains.push(c);
        }
        let base = self.arrays.len() as u32;
        self.arrays.extend(plan.arrays);
        self.map.append(&from.map, base, at, key, &plan.locals);
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
    /// names; of the chunk map, each row, each of its coordinates and each of its ranges.
    pub fn weights(&self) -> (u64, u64) {
        let listed = |g: &Grid| -> usize {
            g.axes.iter().map(|x| match x {
                AxisUnits::Each(_) => 0,
                AxisUnits::List(r) => r.len(),
            }).sum()
        };
        let d: usize = self
            .arrays
            .iter()
            .map(|a| {
                let fill = usize::from(matches!(a.fill, Fill::Given(Param::Source(_))));
                1 + a.shape.len() + a.grid.as_ref().map_or(0, listed) + a.declarations.len() + fill + a.chains.len() + a.unit_parameters.len()
            })
            .sum::<usize>()
            + self
                .chains
                .iter()
                .map(|c| 1 + c.iter().map(|x| 1 + x.parameters.values().filter(|p| matches!(p, Param::Source(_))).count()).sum::<usize>())
                .sum::<usize>();
        let r = self.map.len() + self.map.coords.len() + self.map.range_offset.len();
        (d as u64, r as u64)
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
        let d: usize = self.arrays.iter().map(|a| 96 + a.shape.len() * 8 + a.declarations.len() * 48 + a.chains.len() * 4 + a.unit_parameters.len() * 48).sum();
        let c: usize = self.chains.iter().map(|c| 24 + c.len() * 96).sum();
        d + c + self.map.memory()
    }

    /// The descriptions (their JSON forms), the chain table and each row's values.
    pub fn digest(&self, eat: &mut dyn FnMut(&[u8])) {
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
    /// chain table and the list, each description with its element's path in `ir`.
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
        json!({"chains": self.chains.iter().map(chain_json).collect::<Vec<J>>(), "arrays": arrays})
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
        let rank = self.shape.len();
        if self.element as usize >= ctx.ir.len() {
            return Err(format!("names element {}, which the IR has not", self.element));
        }
        if ctx.roots[self.element as usize] != NONE {
            return Err(format!("is keyed by an element in the run {}, which an unfold would leave behind", named(ctx.ir, ctx.roots[self.element as usize])));
        }
        for p in &self.declarations {
            ctx.place(p).map_err(|e| format!("a declaration {e}"))?;
        }
        if let Some(g) = &self.grid {
            if g.axes.len() != rank {
                return Err(format!("its grid has {} axes, its shape {rank}", g.axes.len()));
            }
            for (k, (a, &len)) in g.axes.iter().zip(&self.shape).enumerate() {
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
        match self.item_bits {
            None if self.grid.is_some() => return Err("it has a grid and no item_bits".into()),
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
            if let Some(i) = (0..c.len()).find(|&i| c[i] >= units(&g.axes[i], d.shape[i])) {
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

/// [`check`], with the codec list `codecs`.
pub fn check_with(ir: &Ir, codecs: &[CodecEntry]) -> Res<()> {
    let s = &ir.stored;
    if s.is_empty() {
        return Ok(());
    }
    let keys = ir.store.then(|| (0..ir.len() as u32).filter(|&i| ir.kind[i as usize] == OBJECT).map(|o| (ir.key(o), o)).collect());
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
            parameters: &[ParamEntry { name: "format", column: None }, ParamEntry { name: "id", column: None }, ParamEntry { name: "parameters", column: None }],
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
            shape: vec![25, 12],
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
            shape: vec![],
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
            shape: vec![3],
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
            shape: vec![0, 7, 0],
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
        let one = StoredArray { element: 1, index: None, shape: vec![8], grid: Some(Grid { axes: vec![AxisUnits::Each(8)], edge: Edge::Short }), chains: vec![0], ..valid().arrays[1].clone() };
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

    #[test]
    fn refuses_descriptions_past_the_budget() {
        let mut ir = file_ir();
        ir.budget.as_mut().unwrap().descriptions = 0;
        let a = StoredArray { index: None, ..valid().arrays[2].clone() };
        assert_eq!(ir.describe(vec![a], vec![], ChunkMap::default()).unwrap_err(), "budget: more than 0 entries of stored arrays");
        assert_eq!(ir.budget.as_ref().unwrap().spent_descriptions, 0);
    }

    #[test]
    fn refuses_listed_runs_past_the_budget() {
        let mut ir = file_ir();
        ir.budget.as_mut().unwrap().descriptions = 1000;
        // one description of 2000 listed runs: 1, 2, 1, 2, ...
        let runs: Vec<Run> = (0..2000).map(|k| Run::One(1 + k % 2)).collect();
        let a = StoredArray { element: 1, index: None, shape: vec![3000], grid: Some(Grid { axes: vec![AxisUnits::List(runs)], edge: Edge::Short }), chains: vec![0], fill: Fill::Undefined, ..valid().arrays[1].clone() };
        assert_eq!(ir.describe(vec![a], vec![vec![]], ChunkMap::default()).unwrap_err(), "budget: more than 1000 entries of stored arrays");
    }

    #[test]
    fn refuses_rows_past_the_budget() {
        let mut ir = file_ir();
        ir.budget.as_mut().unwrap().rows = 4;
        let s = valid();
        assert_eq!(ir.describe(s.arrays, s.chains, s.map).unwrap_err(), "budget: more than 4 chunk map entries");
    }

    #[test]
    fn refuses_ranges_past_the_budget() {
        let mut ir = file_ir();
        ir.budget.as_mut().unwrap().rows = 1000;
        // one row of 2000 ranges
        let mut map = ChunkMap::default();
        map.push(Row { array: 0, coords: vec![], ranges: Some(vec![(None, 0, 1); 2000]), ..Row::default() }).unwrap();
        let a = StoredArray { element: 1, index: None, chains: vec![0], fill: Fill::Undefined, ..valid().arrays[1].clone() };
        assert_eq!(ir.describe(vec![a], vec![vec![]], map).unwrap_err(), "budget: more than 1000 chunk map entries");
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

    /// A store's failed graft, refused by its arrays' plan or by the budget, leaves it as it was.
    #[test]
    fn a_failed_graft_leaves_the_store_unchanged() {
        let chain = |object: Option<&str>| vec![codec("quantize", &[("method", Param::Source(Place::Extent { object: object.map(String::from), offset: 0, length: 4 }))])];
        let a = StoredArray {
            element: 1, index: None, declarations: vec![], shape: vec![2], axes: None, item_bits: Some(8), pad_from: None,
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
        store.budget.as_mut().unwrap().descriptions = 0;
        let before = state(&store);
        assert_eq!(store.graft(&with(valid()), o[0]).unwrap_err(), "budget: more than 0 entries of stored arrays");
        assert!(state(&store) == before);
    }

    /// An unfold that moves a chain's place clears the chain index, which the next graft makes
    /// again from the chains as they are.
    #[test]
    fn a_graft_after_an_unfold_indexes_the_chains_as_they_are() {
        let chain = vec![codec("source", &[("parameters", Param::Source(Place::Element { element: 3, member: Some(2), range: None }))])];
        let file = with(Stored { chains: vec![chain], ..Stored::default() });
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
            element: 1, index: None, declarations: vec![], shape: vec![2], axes: None, item_bits: Some(8), pad_from: None,
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
            element: 1, index: None, declarations: vec![], shape: vec![2], axes: None, item_bits: Some(8), pad_from: None,
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

    #[test]
    fn refuses_a_grid_of_another_rank() {
        assert!(refusal(|s| s.arrays[0].shape = vec![25, 12, 1]).contains("its grid has 2 axes, its shape 3"));
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
        assert!(refusal(|s| { s.arrays[0].shape[0] = 35; s.arrays[0].grid.as_mut().unwrap().axes[0] = a }).contains("two runs of units 10 long side by side"));
    }

    #[test]
    fn refuses_a_list_that_does_not_sum_to_the_axis() {
        assert!(refusal(|s| s.arrays[0].shape[0] = 26).contains("do not sum to its 26"));
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
}
