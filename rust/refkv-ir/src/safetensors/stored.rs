//! The stored arrays of a safetensors file (spec/stored-array.md §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): every tensor
//! of a checked header, presented or not, keyed by the header's element with the tensor's position
//! in the header as its index. A tensor is one unit, its data, stored as is; a packed one (4 or 6
//! bits an element) in a `lsb_first` chain. A shard the store form does not describe in place has
//! no elements of its own: its tensors are keyed by its object, their places ranges of it.

use crate::array::{AxisUnits, Chain, ChunkMap, Codec, Edge, Fill, Grid, Param, Place, Row, StoredArray};
use crate::ir::Res;
use serde_json::json;
use std::collections::BTreeMap;

/// What a tensor's description is built from: the range of the header's bytes that holds its
/// entry's `dtype` value (a JSON string, with its quotes), its entry's shape, the bits of an
/// element of its data type, the (offset, length) of its data from the file's start, and its data's
/// element, where the IR has one.
pub(super) struct Tensor {
    pub dtype: (u64, u64),
    pub shape: Vec<u64>,
    pub bits: u64,
    pub at: (u64, u64),
    pub element: Option<u32>,
}

/// A checked header's tensors, in its order, with the element that keys their descriptions and
/// where the header starts in that element's bytes: the `header` element (0), or, of a shard with
/// no elements, its object (8).
pub(super) struct Tensors {
    pub key: u32,
    pub header_at: u64,
    pub tensors: Vec<Tensor>,
}

/// A scan of JSON text a parser accepted, for the extents of its values.
struct Scan<'a> {
    s: &'a [u8],
    at: usize,
}

impl Scan<'_> {
    fn ws(&mut self) {
        while self.s.get(self.at).is_some_and(|c| matches!(c, b' ' | b'\t' | b'\n' | b'\r')) {
            self.at += 1;
        }
    }

    /// Whether the next byte, after whitespace, is `c`, which is then passed.
    fn eat(&mut self, c: u8) -> bool {
        self.ws();
        let found = self.s.get(self.at) == Some(&c);
        self.at += found as usize;
        found
    }

    /// Passes a string, from its opening quote through its closing one.
    fn string(&mut self) -> Option<()> {
        if self.s.get(self.at) != Some(&b'"') {
            return None;
        }
        self.at += 1;
        loop {
            match *self.s.get(self.at)? {
                b'"' => {
                    self.at += 1;
                    return Some(());
                }
                b'\\' => self.at += 2,
                _ => self.at += 1,
            }
        }
    }

    /// Passes a member's name and its `:`, and gives the name, its escapes read.
    fn name(&mut self) -> Option<String> {
        self.ws();
        let start = self.at;
        self.string()?;
        let name = serde_json::from_slice(self.s.get(start..self.at)?).ok()?;
        self.eat(b':').then_some(name)
    }

    /// Passes a value, and gives its extent.
    fn value(&mut self) -> Option<(usize, usize)> {
        self.ws();
        let start = self.at;
        let mut depth = 0usize;
        loop {
            match *self.s.get(self.at)? {
                b'"' => self.string()?,
                b'{' | b'[' => {
                    depth += 1;
                    self.at += 1;
                }
                b'}' | b']' if depth > 0 => {
                    depth -= 1;
                    self.at += 1;
                }
                // a number or a literal, to the byte that ends it
                _ if depth == 0 => {
                    while self.s.get(self.at).is_some_and(|c| !matches!(c, b',' | b'}' | b']' | b' ' | b'\t' | b'\n' | b'\r')) {
                        self.at += 1;
                    }
                }
                _ => self.at += 1,
            }
            if depth == 0 {
                return (self.at > start).then_some((start, self.at));
            }
        }
    }

    /// Passes an object, calling `member` at each member's value with its name; `member` passes
    /// the value.
    fn object(&mut self, mut member: impl FnMut(&mut Self, String) -> Option<()>) -> Option<()> {
        if !self.eat(b'{') {
            return None;
        }
        if self.eat(b'}') {
            return Some(());
        }
        loop {
            let name = self.name()?;
            member(self, name)?;
            if self.eat(b'}') {
                return Some(());
            }
            if !self.eat(b',') {
                return None;
            }
        }
    }
}

/// For each tensor of the header `raw` (each member but `skip`, in the order written), the
/// (offset, length) in `raw` of its entry's `dtype` value; None where `raw` is not a header the
/// parser checked (an object of objects, each with one `dtype`).
pub(super) fn dtype_spans(raw: &[u8], skip: &str) -> Option<Vec<(u64, u64)>> {
    let mut scan = Scan { s: raw, at: 0 };
    let mut spans = Vec::new();
    scan.object(|scan, name| {
        if name == skip {
            return scan.value().map(|_| ());
        }
        let mut dtype = None;
        scan.object(|scan, member| {
            let (start, end) = scan.value()?;
            if member == "dtype" {
                dtype = Some((start as u64, (end - start) as u64));
            }
            Some(())
        })?;
        spans.push(dtype?);
        Some(())
    })?;
    Some(spans)
}

/// The descriptions of the tensors of `files`, their chains and their rows: a tensor is one unit
/// of its whole shape, in the chain of its element's bits, an empty one for whole bytes and
/// `lsb_first` for fewer than 8 bits, which the buffer packs from each byte's least significant
/// bit; a tensor of no bytes has no unit. safetensors defines no fill value.
pub(super) fn described(files: &[Tensors]) -> Res<(Vec<StoredArray>, Vec<Chain>, ChunkMap)> {
    let mut arrays: Vec<StoredArray> = Vec::new();
    let mut chains: Vec<Chain> = Vec::new();
    let mut chain_index: BTreeMap<u64, u32> = BTreeMap::new();
    let mut map = ChunkMap::default();
    for f in files {
        for (index, t) in f.tensors.iter().enumerate() {
            let packed = t.bits < 8;
            let chain = *chain_index.entry(if packed { t.bits } else { 0 }).or_insert_with(|| {
                let codec = |bits: u64| Codec { name: "lsb_first".into(), parameters: BTreeMap::from([("bits".to_string(), Param::Value(json!(bits)))]) };
                chains.push(if packed { vec![codec(t.bits)] } else { Vec::new() });
                (chains.len() - 1) as u32
            });
            let row = Row { array: arrays.len() as u32, coords: vec![0; t.shape.len()], ..Row::default() };
            match (t.at.1, t.element) {
                (0, _) => {}
                (_, Some(e)) => map.push(Row { element: Some(e), ..row })?,
                // no element is the tensor's data alone: its range of the object
                (_, None) => map.push(Row { element: Some(f.key), range: Some(t.at), ..row })?,
            }
            arrays.push(StoredArray {
                element: f.key,
                index: Some(index as u64),
                declarations: vec![Place::Element { element: f.key, member: None, range: Some((f.header_at + t.dtype.0, t.dtype.1)) }],
                // one unit of the whole tensor: an axis of length 0 has none, and declares no length
                grid: Some(Grid {
                    axes: t.shape.iter().map(|&n| if n == 0 { AxisUnits::List(Vec::new()) } else { AxisUnits::Each(n) }).collect(),
                    edge: Edge::Short,
                }),
                shape: Some(t.shape.clone()),
                axes: None,
                item_bits: Some(t.bits),
                pad_from: None,
                chains: vec![chain],
                fill: Fill::Undefined,
                unit_parameters: Vec::new(),
            });
        }
    }
    Ok((arrays, chains, map))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn spans(header: &str) -> Option<Vec<&str>> {
        let spans = dtype_spans(header.as_bytes(), "__metadata__")?;
        Some(spans.into_iter().map(|(o, n)| &header[o as usize..(o + n) as usize]).collect())
    }

    /// Each tensor's `dtype` value is found where the header writes it: after other members, in
    /// an entry whose other members hold a `dtype` of their own, under an escaped name, between
    /// spaces, and beside strings that hold brackets, quotes and a backslash; `__metadata__` is no
    /// tensor, wherever it is and however its name is written.
    #[test]
    fn finds_each_tensors_dtype_value() {
        let header = concat!(
            r#"{"a":{"dtype":"F32","shape":[2,3],"data_offsets":[0,24]},"#,
            r#" "__metadata__" : {"dtype":"no","k":"}\"{"} ,"#,
            r#""b\\":{"shape":[],"note":{"dtype":"inner","x":[{"dtype":1}]},"dtype" :  "F64" ,"data_offsets":[24,32]},"#,
            r#""__metadata__":{},"#,
            r#""c]":{"data_offsets":[32,32],"s":"a\\","dtype":"U8","shape":[0],"n":-1.5e3,"t":true,"z":null}}   "#,
        );
        assert_eq!(spans(header), Some(vec![r#""F32""#, r#""F64""#, r#""U8""#]));
        assert_eq!(spans("{}"), Some(vec![]));
        assert_eq!(spans(r#"{"__metadata__":{"format":"pt"}}"#), Some(vec![]));
    }

    #[test]
    fn an_entry_with_no_dtype_has_no_spans() {
        assert_eq!(spans(r#"{"a":{"shape":[]}}"#), None);
    }

    #[test]
    fn a_header_that_is_no_object_of_objects_has_no_spans() {
        for header in ["[]", r#"{"a":1}"#, r#"{"a":{"dtype":"U8"}"#, r#"{"a":{"dtype":"U8"} "b":{}}"#, r#"{"a":{"dtype":"U8}}"#, ""] {
            assert_eq!(spans(header), None, "{header}");
        }
    }

    /// A tensor of whole bytes is one unit, its data's element, in an empty chain; a packed one a
    /// unit in `lsb_first` of its bits; a scalar one unit of no coordinates; a tensor of no bytes
    /// no unit, its empty axis listing none; and a tensor keyed by an object a range of it, its
    /// `dtype` value 8 bytes further in than in its header.
    #[test]
    fn describes_each_tensor_as_one_unit() {
        let tensor = |shape: &[u64], bits, at, element| Tensor { dtype: (10, 5), shape: shape.to_vec(), bits, at, element };
        let files = [
            Tensors { key: 2, header_at: 0, tensors: vec![
                tensor(&[2, 3], 32, (100, 24), Some(3)),
                tensor(&[4, 2], 4, (124, 4), Some(4)),
                tensor(&[], 64, (128, 8), Some(5)),
                tensor(&[0, 3], 16, (136, 0), None),
                tensor(&[4], 6, (136, 3), Some(6)),
                tensor(&[8], 4, (139, 4), Some(7)),
            ] },
            Tensors { key: 9, header_at: 8, tensors: vec![tensor(&[5], 8, (60, 5), None)] },
        ];
        let (arrays, chains, map) = described(&files).unwrap();
        let lsb = |bits: u64| vec![Codec { name: "lsb_first".into(), parameters: BTreeMap::from([("bits".to_string(), Param::Value(json!(bits)))]) }];
        assert_eq!(chains, vec![Vec::new(), lsb(4), lsb(6)]);
        let got: Vec<_> = arrays.iter().map(|a| (a.element, a.index, a.shape.clone().unwrap(), a.item_bits, a.chains.clone())).collect();
        assert_eq!(got, vec![
            (2, Some(0), vec![2, 3], Some(32), vec![0]),
            (2, Some(1), vec![4, 2], Some(4), vec![1]),
            (2, Some(2), vec![], Some(64), vec![0]),
            (2, Some(3), vec![0, 3], Some(16), vec![0]),
            (2, Some(4), vec![4], Some(6), vec![2]),
            (2, Some(5), vec![8], Some(4), vec![1]),
            (9, Some(0), vec![5], Some(8), vec![0]),
        ]);
        for a in &arrays {
            assert_eq!((&a.axes, a.pad_from, &a.fill, a.unit_parameters.len()), (&None, None, &Fill::Undefined, 0));
            let at = if a.element == 9 { 18 } else { 10 };
            assert_eq!(a.declarations, vec![Place::Element { element: a.element, member: None, range: Some((at, 5)) }]);
        }
        let grids: Vec<_> = arrays.iter().map(|a| a.grid.clone().unwrap()).collect();
        assert!(grids.iter().all(|g| g.edge == Edge::Short));
        assert_eq!(grids[0].axes, vec![AxisUnits::Each(2), AxisUnits::Each(3)]);
        assert_eq!(grids[2].axes, vec![]);
        assert_eq!(grids[3].axes, vec![AxisUnits::List(Vec::new()), AxisUnits::Each(3)]);
        let rows: Vec<_> = (0..map.len()).map(|k| (map.array[k], map.coords_of(k).to_vec(), map.element[k], map.range[k])).collect();
        assert_eq!(rows, vec![
            (0, vec![0, 0], Some(3), None),
            (1, vec![0, 0], Some(4), None),
            (2, vec![], Some(5), None),
            (4, vec![0], Some(6), None),
            (5, vec![0], Some(7), None),
            (6, vec![0], Some(9), Some((60, 5))),
        ]);
    }
}
