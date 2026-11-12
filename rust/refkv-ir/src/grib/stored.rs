//! The stored arrays of a GRIB file (spec/stored-array.md §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): for every
//! message, descriptions keyed by its `messages/<i>` struct, one for each bitmap its sections 6
//! hold and one for each field, in the message's order. A field's array is the values its
//! section 7 codes, in the file's order: the grid's rows and columns where every point has one,
//! and one axis of the coded values where a bitmap leaves points out. Its values are IEEE floats
//! as section 7 stores them (template 5.4), the reference value alone where the packing codes
//! none (a bit width of 0), or what `grib_scale` makes of the integers the packing's codec
//! decodes; a packing the list has no name for is `source`. Built from the sections the parser
//! read: of a section 7, its length alone.

use super::meta::Grid;
use super::sections::uint;
use crate::array::{AxisUnits, Chain, ChunkMap, Codec, Edge, Fill, Grid as Units, Param, Place, Row, StoredArray};
use crate::ir::Res;
use serde_json::json;
use std::collections::BTreeMap;

/// A section 6 that holds a bitmap (indicator 0), as the IR holds it.
pub(super) struct Bitmap {
    /// the message's struct, and the array's index among the message's
    pub message: u32,
    pub index: u64,
    /// the section 3 in effect, which gives the grid's number of points
    pub s3: u32,
    /// the element of the section's first 6 octets, which hold the indicator
    pub header: u32,
    /// the `bitmap` element, of the octets after them, and its length; None for a section of 6
    pub bits: Option<(u32, u64)>,
    pub points: u64,
}

/// A field, as the IR holds its sections.
pub(super) struct Field {
    pub message: u32,
    pub index: u64,
    /// the elements of its sections 3 and 5, of the first 6 octets of its section 6, and of its
    /// section 7
    pub s3: u32,
    pub s5: u32,
    pub s6: u32,
    pub s7: u32,
    /// the `bitmap` element that says which points its values are: its own section 6's, or, under
    /// indicator 254, that of the message's last earlier field that has one
    pub mask: Option<u32>,
    /// section 6's indicator
    pub indicator: u8,
    /// the grid's rows and the points of each (Nj, Ni), and whether consecutive points run along
    /// a column (scanning mode 0x20); None for a grid of no rows the parser reads
    pub rows: Option<(u64, u64, bool)>,
    pub points: u64,
    /// section 5's bytes, from its length
    pub packing: Vec<u8>,
    /// section 7's length, its 5 octets of length and number included
    pub data: u64,
}

/// What a message's sections declare, in the message's order.
pub(super) enum Reached {
    Bitmap(Bitmap),
    Field(Field),
}

/// The rows of a grid of the templates whose Ni and Nj the parser reads (3.0, 3.1, 3.10, 3.20,
/// 3.30 and 3.40, defined by the template, with no list of the points of each row), where their
/// product is the grid's number of points: (Nj, Ni, whether consecutive points run along a
/// column).
pub(super) fn rows(g: &Grid) -> Option<(u64, u64, bool)> {
    // the key is the section from octet 6: the source of the grid definition, and, at octet 11,
    // the octets of each number of a list of points
    let listed = g.key.first() != Some(&0) || g.key.get(5) != Some(&0);
    let read = matches!(g.template, 0 | 1 | 10 | 20 | 30 | 40) && !listed;
    let whole = g.nx != 0 && g.ny != 0 && g.nx != u32::MAX as u64 && g.ny != u32::MAX as u64 && g.nx.checked_mul(g.ny) == Some(g.points);
    (read && whole).then_some((g.ny, g.nx, g.scan & 0x20 != 0))
}

/// How a field's values are stored, by its section 5 (`s5`, from its length) and the octets of
/// its section 7 after the first five (`data`).
#[derive(Debug, PartialEq)]
pub(super) enum Values {
    /// IEEE floats of `size` octets, as stored (template 5.4)
    Floats { size: u64 },
    /// no value is coded: every one is the reference value (a bit width of 0, or complex packing
    /// of no groups)
    Constant,
    /// integers a codec decodes, which `grib_scale` makes the values of: the codec, the octets of
    /// section 5 that are its parameters as (name, first octet, count), and the octets of section
    /// 7 that hold them
    Packed { codec: &'static str, parameters: Vec<(&'static str, u64, u64)>, length: u64 },
    /// a packing the list has no name for, or one of its packings with parameters the list's
    /// entry does not take
    Source,
    /// section 7 holds fewer octets than the values take
    Short,
}

/// The values of a field of `n` values (section 5's octets 6–9).
pub(super) fn values(s5: &[u8], data: u64, n: u64) -> Values {
    let template = uint(s5, 9, 2);
    if template == 4 {
        // octet 12: the precision, 1 for binary32 and 2 for binary64
        let size = match s5.get(11) {
            Some(1) => 4,
            Some(2) => 8,
            _ => return Values::Source,
        };
        return if n.checked_mul(size).is_some_and(|b| b <= data) { Values::Floats { size } } else { Values::Short };
    }
    // octets 12–19, the reference value and the scale factors, and octet 20, the bit width
    if !matches!(template, 0 | 2 | 3 | 40 | 41 | 42) || s5.len() < 20 {
        return Values::Source;
    }
    let bits = s5[19] as u64;
    let (codec, parameters): (&'static str, Vec<(&'static str, u64, u64)>) = match template {
        0 => ("grib_simple", vec![("bits", 20, 1)]),
        2 | 3 => {
            let spatial = template == 3;
            if s5.len() < if spatial { 49 } else { 47 } {
                return Values::Source;
            }
            // missing value management above 2, a width past 64 bits, and spatial differencing
            // of another order than 1 and 2, or of descriptors of more than 8 octets, are not
            // the list's
            let wide = bits > 64 || s5[36] > 64 || s5[46] > 64;
            if s5[22] > 2 || wide || (spatial && (!(1..=2).contains(&s5[47]) || s5[48] > 8)) {
                return Values::Source;
            }
            // no groups (octets 32–35): no value is coded
            if uint(s5, 31, 4) == 0 {
                return Values::Constant;
            }
            let mut p = vec![("bits", 20, 1), ("groups", 22, 26)];
            if spatial {
                p.push(("differencing", 48, 2));
            }
            ("grib_complex", p)
        }
        40 => ("jpeg2k", Vec::new()),
        41 => ("grib_png", Vec::new()),
        _ => {
            // samples of more than 32 bits, and signed ones (libaec's flag 1, at octet 22), are
            // not the list's
            if s5.len() < 25 || bits > 32 || s5[21] & 1 != 0 {
                return Values::Source;
            }
            ("grib_ccsds", vec![("bits", 20, 1), ("flags", 22, 1), ("block_size", 23, 1), ("reference_sample_interval", 24, 2)])
        }
    };
    if !matches!(template, 2 | 3) && bits == 0 {
        return Values::Constant;
    }
    if template == 0 {
        // the integers of `grib_scale` are of 64 bits, two's complement
        if bits > 63 {
            return Values::Source;
        }
        let length = n.checked_mul(bits).map(|b| b.div_ceil(8));
        return match length {
            Some(length) if length <= data => Values::Packed { codec, parameters, length },
            _ => Values::Short,
        };
    }
    // a stream: all of section 7's data
    if data == 0 && n > 0 { Values::Short } else { Values::Packed { codec, parameters, length: data } }
}

/// One unit of the whole of an axis of `n` elements: an axis of none has no unit.
fn whole(n: u64) -> AxisUnits {
    if n == 0 { AxisUnits::List(Vec::new()) } else { AxisUnits::Each(n) }
}

fn place(element: u32) -> Place {
    Place::Element { element, member: None, range: None }
}

/// The descriptions of the messages' bitmaps and fields, their chains and their rows.
pub(super) fn described(reached: &[Reached]) -> Res<(Vec<StoredArray>, Vec<Chain>, ChunkMap)> {
    let mut arrays: Vec<StoredArray> = Vec::new();
    let mut chains: Vec<Chain> = Vec::new();
    let mut chain_index: BTreeMap<String, u32> = BTreeMap::new();
    let mut map = ChunkMap::default();
    let mut chain_of = |chain: Chain| {
        let key = serde_json::Value::Array(chain.iter().map(Codec::to_json).collect()).to_string();
        *chain_index.entry(key).or_insert_with(|| {
            chains.push(chain);
            (chains.len() - 1) as u32
        })
    };
    for r in reached {
        let array = arrays.len() as u32;
        match r {
            Reached::Bitmap(b) => {
                // a bit for each point of the grid, in the file's order, most significant first
                let octets = b.points.div_ceil(8);
                let held = b.bits.filter(|&(_, length)| octets <= length);
                let stored = b.points == 0 || held.is_some();
                arrays.push(StoredArray {
                    element: b.message,
                    index: Some(b.index),
                    declarations: vec![place(b.s3), place(b.header)],
                    shape: Some(vec![b.points]),
                    axes: None,
                    item_bits: Some(1),
                    pad_from: None,
                    grid: stored.then(|| Units { axes: vec![whole(b.points)], edge: Edge::Short }),
                    chains: if stored { vec![chain_of(Chain::new())] } else { Vec::new() },
                    fill: Fill::Undefined,
                    unit_parameters: Vec::new(),
                });
                if let Some((element, length)) = held.filter(|_| b.points > 0) {
                    // the bits of the points: the section's octets after the first 6, or the
                    // first of them where it holds more
                    let range = (octets < length).then_some((0, octets));
                    map.push(Row { array, coords: vec![0], element: Some(element), range, ..Row::default() })?;
                }
            }
            Reached::Field(f) => {
                let n = uint(&f.packing, 5, 4);
                // every point has a value: the grid's rows, or its columns where consecutive
                // points run along one; else the coded values, in order
                let every = f.indicator == 255 && n == f.points;
                let (shape, axes) = match f.rows.filter(|_| every) {
                    Some((nj, ni, false)) => (vec![nj, ni], Some(vec![1, 0])),
                    Some((nj, ni, true)) => (vec![ni, nj], None),
                    None => (vec![n], None),
                };
                let mut declarations = vec![place(f.s3), place(f.s5), place(f.s6)];
                declarations.extend(f.mask.map(place));
                let within = |first: u64, count: u64| Place::Element { element: f.s5, member: None, range: Some((first - 1, count)) };
                let data = f.data.saturating_sub(5);
                let stored = values(&f.packing, data, n);
                // the elements' bits, the chain, the octets of section 7's data that are the
                // unit, and the fill value
                let (item_bits, chain, unit, fill) = match &stored {
                    Values::Floats { size } => (Some(size * 8), Some(Chain::new()), n * size, Fill::Undefined),
                    // the reference value, an IEEE binary32, is every value: none is stored
                    Values::Constant => (Some(32), Some(Chain::new()), 0, Fill::Given(Param::Source(within(12, 4)))),
                    Values::Packed { codec, parameters, length } => {
                        let scale = [("reference", 12, 4), ("binary_scale", 16, 2), ("decimal_scale", 18, 2)];
                        let codec_of = |name: &str, parameters: &[(&'static str, u64, u64)]| Codec {
                            name: name.to_string(),
                            parameters: parameters.iter().map(|&(k, first, count)| (k.to_string(), Param::Source(within(first, count)))).collect(),
                        };
                        (Some(64), Some(vec![codec_of("grib_scale", &scale), codec_of(codec, parameters)]), *length, Fill::Undefined)
                    }
                    Values::Source => {
                        let mut parameters = BTreeMap::from([
                            ("format".to_string(), Param::Value(json!("grib"))),
                            ("id".to_string(), Param::Value(json!(uint(&f.packing, 9, 2)))),
                        ]);
                        // the template's own octets, after its number
                        if f.packing.len() > 11 {
                            parameters.insert("parameters".to_string(), Param::Source(within(12, f.packing.len() as u64 - 11)));
                        }
                        (None, Some(vec![Codec { name: "source".into(), parameters }]), data, Fill::Undefined)
                    }
                    Values::Short => (None, None, 0, Fill::Undefined),
                };
                let grid = chain.is_some().then(|| Units { axes: shape.iter().map(|&s| whole(s)).collect(), edge: Edge::Short });
                arrays.push(StoredArray {
                    element: f.message,
                    index: Some(f.index),
                    declarations,
                    shape: Some(shape.clone()),
                    axes,
                    item_bits,
                    pad_from: None,
                    grid,
                    chains: chain.map(&mut chain_of).into_iter().collect(),
                    fill,
                    unit_parameters: Vec::new(),
                });
                if unit > 0 && n > 0 {
                    map.push(Row { array, coords: vec![0; shape.len()], element: Some(f.s7), range: Some((5, unit)), ..Row::default() })?;
                }
            }
        }
    }
    Ok((arrays, chains, map))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A section 5 of `template` for `n` values, of `length` octets, with `bits` at octet 20.
    fn s5(template: u16, n: u32, length: usize, bits: u8) -> Vec<u8> {
        let mut b = vec![0u8; length];
        b[..4].copy_from_slice(&(length as u32).to_be_bytes());
        b[4] = 5;
        b[5..9].copy_from_slice(&n.to_be_bytes());
        b[9..11].copy_from_slice(&template.to_be_bytes());
        if length >= 20 {
            b[19] = bits;
        }
        b
    }

    fn packed(v: Values) -> Option<(&'static str, Vec<&'static str>, u64)> {
        match v {
            Values::Packed { codec, parameters, length } => Some((codec, parameters.into_iter().map(|p| p.0).collect(), length)),
            _ => None,
        }
    }

    /// Each packing the list names is its codec, over the octets of section 7 that hold its
    /// values: simple packing's bits, a stream's whole data, and IEEE floats as they are.
    #[test]
    fn each_packing_is_its_codec() {
        assert_eq!(packed(values(&s5(0, 10, 21, 12), 15, 10)), Some(("grib_simple", vec!["bits"], 15)));
        assert_eq!(packed(values(&s5(0, 10, 21, 12), 40, 10)), Some(("grib_simple", vec!["bits"], 15)));
        let mut complex = s5(2, 10, 47, 8);
        complex[34] = 3;
        assert_eq!(packed(values(&complex, 40, 10)), Some(("grib_complex", vec!["bits", "groups"], 40)));
        let mut spatial = s5(3, 10, 49, 8);
        (spatial[34], spatial[47], spatial[48]) = (3, 2, 2);
        assert_eq!(packed(values(&spatial, 40, 10)), Some(("grib_complex", vec!["bits", "groups", "differencing"], 40)));
        assert_eq!(packed(values(&s5(40, 10, 23, 12), 40, 10)), Some(("jpeg2k", vec![], 40)));
        assert_eq!(packed(values(&s5(41, 10, 21, 16), 40, 10)), Some(("grib_png", vec![], 40)));
        assert_eq!(packed(values(&s5(42, 10, 25, 12), 40, 10)), Some(("grib_ccsds", vec!["bits", "flags", "block_size", "reference_sample_interval"], 40)));
        let mut ieee = s5(4, 10, 12, 0);
        ieee[11] = 1;
        assert_eq!(values(&ieee, 40, 10), Values::Floats { size: 4 });
        ieee[11] = 2;
        assert_eq!(values(&ieee, 80, 10), Values::Floats { size: 8 });
    }

    #[test]
    fn a_bit_width_of_0_codes_no_value() {
        for template in [0, 40, 41, 42] {
            assert_eq!(values(&s5(template, 10, 25, 0), 0, 10), Values::Constant, "5.{template}");
        }
    }

    #[test]
    fn complex_packing_of_no_groups_codes_no_value() {
        assert_eq!(values(&s5(2, 10, 47, 8), 0, 10), Values::Constant);
        let mut spatial = s5(3, 10, 49, 8);
        (spatial[47], spatial[48]) = (1, 1);
        assert_eq!(values(&spatial, 0, 10), Values::Constant);
    }

    #[test]
    fn a_packing_the_list_does_not_name_is_source() {
        for template in [1, 50, 51, 61, 200] {
            assert_eq!(values(&s5(template, 10, 30, 8), 40, 10), Values::Source, "5.{template}");
        }
    }

    #[test]
    fn ieee_floats_of_another_precision_are_source() {
        let mut ieee = s5(4, 10, 12, 0);
        ieee[11] = 3;
        assert_eq!(values(&ieee, 160, 10), Values::Source);
    }

    #[test]
    fn simple_packing_of_64_bits_is_source() {
        assert_eq!(values(&s5(0, 10, 21, 64), 80, 10), Values::Source);
    }

    #[test]
    fn a_section_5_shorter_than_its_template_is_source() {
        // before the bit width; before CCSDS's reference sample interval; before complex
        // packing's bits of a scaled length, and before its descriptors' octets
        assert_eq!(values(&s5(0, 10, 19, 0), 80, 10), Values::Source);
        assert_eq!(values(&s5(42, 10, 24, 12), 80, 10), Values::Source);
        assert_eq!(values(&s5(2, 10, 46, 8), 80, 10), Values::Source);
        assert_eq!(values(&s5(3, 10, 48, 8), 80, 10), Values::Source);
    }

    #[test]
    fn ccsds_samples_the_codec_does_not_take_are_source() {
        // of 33 bits, and signed
        assert_eq!(values(&s5(42, 10, 25, 33), 80, 10), Values::Source);
        let mut signed = s5(42, 10, 25, 12);
        signed[21] = 1;
        assert_eq!(values(&signed, 80, 10), Values::Source);
    }

    #[test]
    fn complex_packing_of_other_options_is_source() {
        // missing value management 3; a coded width of 65 bits; spatial differencing of order 0
        // and of order 3, and of descriptors of 9 octets
        let complex = |at: usize, v: u8| {
            let mut b = s5(3, 10, 49, 8);
            (b[34], b[47], b[48]) = (3, 1, 1);
            b[at] = v;
            values(&b, 80, 10)
        };
        assert!(matches!(complex(22, 2), Values::Packed { .. }));
        for (at, v) in [(22, 3), (36, 65), (47, 0), (47, 3), (48, 9)] {
            assert_eq!(complex(at, v), Values::Source, "octet {} of {v}", at + 1);
        }
    }

    #[test]
    fn a_section_7_shorter_than_its_values_is_short() {
        // simple packing's integers, and IEEE floats
        assert_eq!(values(&s5(0, 10, 21, 12), 14, 10), Values::Short);
        let mut ieee = s5(4, 10, 12, 0);
        ieee[11] = 1;
        assert_eq!(values(&ieee, 39, 10), Values::Short);
    }

    #[test]
    fn a_stream_of_no_octets_is_short() {
        assert_eq!(values(&s5(40, 10, 23, 12), 0, 10), Values::Short);
        // a field of no values has none to hold
        assert!(matches!(values(&s5(40, 0, 23, 12), 0, 0), Values::Packed { length: 0, .. }));
    }

    fn grid(template: u64, nx: u64, ny: u64, points: u64, scan: u8) -> Grid {
        Grid { template, points, nx, ny, scan, earth: None, projection: None, reason: None, key: vec![0; 9] }
    }

    #[test]
    fn a_grid_of_rows_is_its_rows_and_columns() {
        assert_eq!(rows(&grid(0, 16, 12, 192, 0)), Some((12, 16, false)));
        assert_eq!(rows(&grid(30, 16, 12, 192, 0x60)), Some((12, 16, true)));
    }

    #[test]
    fn a_grid_of_no_rows_has_none() {
        // another template, a product that is not the points, and a list of the points of each row
        assert_eq!(rows(&grid(101, 0, 0, 192, 0)), None);
        assert_eq!(rows(&grid(0, 16, 12, 191, 0)), None);
        let mut listed = grid(0, 16, 12, 192, 0);
        listed.key[5] = 2;
        assert_eq!(rows(&listed), None);
        let mut source = grid(0, 16, 12, 192, 0);
        source.key[0] = 1;
        assert_eq!(rows(&source), None);
    }
}
