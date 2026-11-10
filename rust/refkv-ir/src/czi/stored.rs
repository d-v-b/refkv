//! The stored arrays of a CZI file (spec/stored-array.md §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): a description
//! for every subblock the parser reads, keyed by the subblock's struct. A subblock is one
//! rectangle of pixels of its own size, pixel type and compression, in one unit; where it lies
//! among the others (its scene, its plane, its place in a mosaic or a pyramid) is in its entry,
//! which its declarations name, and assembling the subblocks is the profile's. Built from what
//! the parser read, on its finished IR.

use crate::array::{AxisUnits, Chain, ChunkMap, Codec, Edge, Fill, Grid, Param, Place, Row, StoredArray};
use crate::coding::{JpegFrame, JPEG};
use crate::ir::Res;
use serde_json::{json, Value as J};
use std::collections::BTreeMap;

/// What a subblock's description needs.
pub(super) struct Reached {
    /// the subblock's struct, which declares the array
    pub element: u32,
    /// where its own copy of its entry is: the `entry` value, then the `dimensions` value where
    /// the copy has any
    pub declarations: Vec<Place>,
    /// its pixels, where its copy of its entry agrees with the entry that names it
    pub pixels: Option<Pixels>,
    /// how its data is encoded, and where it is, where the parser found the data to be what the
    /// entry says (a coded size)
    pub data: Option<(Encoding, Unit)>,
}

/// A subblock's pixels, as its entry declares them: its stored size, the samples of its pixel
/// type, and the bits of one.
#[derive(Clone, Copy)]
pub(super) struct Pixels {
    pub height: u64,
    pub width: u64,
    pub samples: u64,
    pub bits: u64,
}

/// How a subblock's data is encoded, by its entry's Compression and what the parser read of its
/// stream.
#[derive(Clone, Copy)]
pub(super) enum Encoding {
    /// uncompressed: the pixels as they are
    Stored,
    /// a JPEG stream of 8-bit samples
    Jpeg,
    /// TIFF's LZW
    Lzw,
    /// a JPEG XR file
    JpegXr,
    /// Zstd0: a Zstandard frame
    Zstd,
    /// Zstd1: its header, then a Zstandard frame
    Zstd1,
    /// an encoding the list has no name for: the entry's Compression
    Source(i32),
}

/// Where a subblock's unit is.
#[derive(Clone, Copy)]
pub(super) enum Unit {
    /// an element, whole
    Element(u32),
    /// (offset, length) of the file, where no one element holds the unit
    Range(u64, u64),
}

/// How the stream of a JpgFile or lossless JPEG subblock is encoded, by its frame header: None
/// where the stream is not what the entry says. Its components are the pixel type's samples, and
/// its sample precision is one the samples' bits hold: a lossless stream's (SOF3) from 2 to 8 for
/// samples of 8 bits and from 9 to 16 for samples of 16; under JpgFile a DCT stream's (SOF0 to
/// SOF2) too, of 8 for samples of 8 bits and of 12 for samples of 16. A stream of 8-bit samples
/// is the list's `jpeg`; any other is the source's own.
pub(super) fn jpeg_encoding(compression: i32, pixels: &Pixels, frame: &JpegFrame) -> Option<Encoding> {
    if frame.components as u64 != pixels.samples || frame.height < 1 || frame.width < 1 {
        return None;
    }
    let fits = match (frame.marker, pixels.bits) {
        (0xC3, 8) => (2..=8).contains(&frame.precision),
        (0xC3, 16) => (9..=16).contains(&frame.precision),
        (0xC0..=0xC2, 8) if compression == JPEG => frame.precision == 8,
        (0xC0..=0xC2, 16) if compression == JPEG => frame.precision == 12,
        _ => false,
    };
    fits.then_some(if frame.precision == 8 { Encoding::Jpeg } else { Encoding::Source(compression) })
}

fn codec(name: &str) -> Codec {
    Codec { name: name.into(), parameters: BTreeMap::new() }
}

/// The chain of an encoding.
fn chain(encoding: Encoding) -> Chain {
    match encoding {
        Encoding::Stored => Vec::new(),
        Encoding::Jpeg => vec![codec("jpeg")],
        Encoding::Lzw => vec![codec("tiff_lzw")],
        Encoding::JpegXr => vec![codec("jpegxr")],
        Encoding::Zstd => vec![codec("zstd")],
        Encoding::Zstd1 => vec![codec("czi_zstd1")],
        Encoding::Source(id) => {
            let parameters = [("format".to_string(), Param::Value(json!("czi"))), ("id".to_string(), Param::Value(json!(id)))];
            vec![Codec { name: "source".into(), parameters: parameters.into_iter().collect() }]
        }
    }
}

/// The descriptions of the subblocks, their chains and their rows.
pub(super) fn described(subblocks: &[Reached]) -> Res<(Vec<StoredArray>, Vec<Chain>, ChunkMap)> {
    let mut arrays: Vec<StoredArray> = Vec::new();
    let mut chains: Vec<Chain> = Vec::new();
    let mut chain_index: BTreeMap<String, u32> = BTreeMap::new();
    let mut map = ChunkMap::default();
    for s in subblocks {
        let array = arrays.len() as u32;
        // rows of pixels, a pixel's samples together: an axis of them where the pixel type has
        // more than one
        let shape = s.pixels.map(|p| {
            let mut shape = vec![p.height, p.width];
            if p.samples > 1 {
                shape.push(p.samples);
            }
            shape
        });
        let mut a = StoredArray {
            element: s.element,
            index: None,
            declarations: s.declarations.clone(),
            shape: shape.clone(),
            axes: None,
            item_bits: s.pixels.map(|p| p.bits),
            pad_from: None,
            grid: None,
            chains: Vec::new(),
            fill: Fill::Undefined,
            unit_parameters: Vec::new(),
        };
        if let (Some(shape), Some((encoding, unit))) = (shape, s.data) {
            // one unit, the whole subblock
            let chain = chain(encoding);
            let key = J::Array(chain.iter().map(Codec::to_json).collect()).to_string();
            let c = *chain_index.entry(key).or_insert_with(|| {
                chains.push(chain);
                (chains.len() - 1) as u32
            });
            a.grid = Some(Grid { axes: shape.iter().map(|&n| AxisUnits::Each(n)).collect(), edge: Edge::Short });
            a.chains = vec![c];
            let coords = vec![0; shape.len()];
            let row = match unit {
                Unit::Element(e) => Row { array, coords, element: Some(e), ..Row::default() },
                Unit::Range(offset, length) => Row { array, coords, ranges: Some(vec![(None, offset, length)]), ..Row::default() },
            };
            map.push(row)?;
        }
        arrays.push(a);
    }
    Ok((arrays, chains, map))
}

#[cfg(test)]
mod tests {
    use super::*;

    const GRAY16: Pixels = Pixels { height: 5, width: 7, samples: 1, bits: 16 };
    const BGR24: Pixels = Pixels { height: 5, width: 7, samples: 3, bits: 8 };

    fn reached(pixels: Option<Pixels>, data: Option<(Encoding, Unit)>) -> Reached {
        Reached { element: 4, declarations: vec![place(6), place(7)], pixels, data }
    }

    fn place(element: u32) -> Place {
        Place::Element { element, member: None, range: None }
    }

    fn names(c: &Chain) -> Vec<&str> {
        c.iter().map(|x| x.name.as_str()).collect()
    }

    /// A subblock with a coded size is one unit of its stored size, with an axis of samples where
    /// its pixel type has several, in its encoding's chain: its element, or the range of the file
    /// no one element holds.
    #[test]
    fn a_subblock_is_one_unit_of_its_encodings_chain() {
        let cases: [(Pixels, Encoding, Vec<&str>); 7] = [
            (GRAY16, Encoding::Stored, vec![]),
            (BGR24, Encoding::Jpeg, vec!["jpeg"]),
            (GRAY16, Encoding::Lzw, vec!["tiff_lzw"]),
            (BGR24, Encoding::JpegXr, vec!["jpegxr"]),
            (GRAY16, Encoding::Zstd, vec!["zstd"]),
            (GRAY16, Encoding::Zstd1, vec!["czi_zstd1"]),
            (GRAY16, Encoding::Source(1), vec!["source"]),
        ];
        for (pixels, encoding, want) in cases {
            for unit in [Unit::Element(9), Unit::Range(400, 33)] {
                let (arrays, chains, map) = described(&[reached(Some(pixels), Some((encoding, unit)))]).unwrap();
                let [a] = &arrays[..] else { panic!("{} arrays", arrays.len()) };
                let shape: Vec<u64> = if pixels.samples > 1 { vec![5, 7, pixels.samples] } else { vec![5, 7] };
                assert_eq!((a.element, a.index, a.axes.clone(), a.pad_from), (4, None, None, None));
                assert_eq!(a.declarations, vec![place(6), place(7)]);
                assert_eq!((a.shape.clone(), a.item_bits), (Some(shape.clone()), Some(pixels.bits)));
                assert_eq!(a.grid, Some(Grid { axes: shape.iter().map(|&n| AxisUnits::Each(n)).collect(), edge: Edge::Short }));
                assert_eq!((a.chains.clone(), a.fill.clone(), a.unit_parameters.len()), (vec![0], Fill::Undefined, 0));
                assert_eq!(chains.len(), 1);
                assert_eq!(names(&chains[0]), want);
                if let Encoding::Source(id) = encoding {
                    let parameters = [("format".to_string(), Param::Value(json!("czi"))), ("id".to_string(), Param::Value(json!(id)))];
                    assert_eq!(chains[0][0].parameters, parameters.into_iter().collect());
                }
                assert_eq!((map.len(), map.array[0], map.coords_of(0)), (1, 0, &vec![0; shape.len()][..]));
                match unit {
                    Unit::Element(e) => assert_eq!((map.element[0], map.has_ranges[0]), (Some(e), false)),
                    Unit::Range(o, n) => {
                        assert_eq!((map.element[0], map.ranges_of(0)), (None, Some(0..1)));
                        assert_eq!((map.range_object[0], map.range_offset[0], map.range_length[0]), (None, o, n));
                    }
                }
            }
        }
    }

    fn frame(marker: u8, precision: u8, components: u8) -> JpegFrame {
        JpegFrame { marker, precision, height: 5, width: 7, components }
    }

    fn named(e: Option<Encoding>) -> Option<Vec<String>> {
        e.map(|e| chain(e).iter().map(|c| c.name.clone()).collect())
    }

    /// A JPEG stream whose frame header is of its entry's samples is the list's `jpeg` at 8 bits,
    /// and the source's own at any other precision: a DCT stream under JpgFile, and a lossless
    /// one under either compression, of three components too.
    #[test]
    fn a_jpeg_stream_of_its_entrys_samples_has_a_chain() {
        const LJPEG: i32 = crate::coding::LJPEG;
        let cases = [
            (JPEG, BGR24, frame(0xC0, 8, 3), "jpeg"),
            (JPEG, BGR24, frame(0xC2, 8, 3), "jpeg"),
            (JPEG, BGR24, frame(0xC3, 8, 3), "jpeg"),
            (JPEG, BGR24, frame(0xC3, 5, 3), "source"),
            (JPEG, GRAY16, frame(0xC1, 12, 1), "source"),
            (JPEG, GRAY16, frame(0xC3, 16, 1), "source"),
            (LJPEG, GRAY16, frame(0xC3, 9, 1), "source"),
            (LJPEG, Pixels { samples: 1, ..BGR24 }, frame(0xC3, 8, 1), "jpeg"),
            (LJPEG, Pixels { samples: 1, ..BGR24 }, frame(0xC3, 2, 1), "source"),
        ];
        for (compression, pixels, f, want) in cases {
            assert_eq!(named(jpeg_encoding(compression, &pixels, &f)), Some(vec![want.to_string()]), "{compression} {f:?}");
        }
    }

    /// A JPEG stream that is not what its entry says has no chain: other components than the
    /// pixel type's samples, a precision the samples' bits do not hold, a DCT stream under
    /// lossless JPEG, a frame of no size, and a frame of another process.
    #[test]
    fn a_jpeg_stream_that_is_not_its_entrys_has_no_chain() {
        const LJPEG: i32 = crate::coding::LJPEG;
        let cases = [
            (JPEG, BGR24, frame(0xC0, 8, 1)),
            (JPEG, GRAY16, frame(0xC0, 8, 1)),
            (JPEG, GRAY16, frame(0xC3, 8, 1)),
            (JPEG, BGR24, frame(0xC0, 12, 3)),
            (JPEG, BGR24, frame(0xC3, 12, 3)),
            (LJPEG, Pixels { samples: 1, ..BGR24 }, frame(0xC0, 8, 1)),
            (LJPEG, GRAY16, frame(0xC1, 12, 1)),
            (JPEG, BGR24, JpegFrame { height: 0, ..frame(0xC0, 8, 3) }),
            (JPEG, BGR24, JpegFrame { width: 0, ..frame(0xC0, 8, 3) }),
            (JPEG, BGR24, frame(0xC9, 8, 3)),
        ];
        for (compression, pixels, f) in cases {
            assert_eq!(named(jpeg_encoding(compression, &pixels, &f)), None, "{compression} {f:?}");
        }
    }

    /// Subblocks of one encoding share its chain.
    #[test]
    fn subblocks_of_one_encoding_share_a_chain() {
        let all = [
            reached(Some(GRAY16), Some((Encoding::Zstd, Unit::Element(9)))),
            reached(Some(GRAY16), Some((Encoding::Source(3), Unit::Element(12)))),
            reached(Some(BGR24), Some((Encoding::Zstd, Unit::Element(15)))),
        ];
        let (arrays, chains, map) = described(&all).unwrap();
        assert_eq!(arrays.iter().map(|a| a.chains.clone()).collect::<Vec<_>>(), vec![vec![0], vec![1], vec![0]]);
        assert_eq!((chains.len(), map.len()), (2, 3));
    }

    /// A subblock whose data the parser did not find to be what its entry says has its entry's
    /// shape and sample size, and no grid.
    #[test]
    fn a_subblock_with_no_coded_size_has_no_grid() {
        let (arrays, chains, map) = described(&[reached(Some(BGR24), None)]).unwrap();
        assert_eq!((arrays[0].shape.clone(), arrays[0].item_bits), (Some(vec![5, 7, 3]), Some(8)));
        assert_eq!((arrays[0].grid.clone(), arrays[0].chains.len(), chains.len(), map.len()), (None, 0, 0, 0));
    }

    /// A subblock whose copy of its entry does not agree with the entry that names it has neither
    /// shape nor sample size, and no grid.
    #[test]
    fn a_subblock_declared_two_ways_has_no_shape() {
        let (arrays, chains, map) = described(&[reached(None, None)]).unwrap();
        assert_eq!((arrays[0].shape.clone(), arrays[0].item_bits, arrays[0].declarations.len()), (None, None, 2));
        assert_eq!((arrays[0].grid.clone(), arrays[0].chains.len(), chains.len(), map.len()), (None, 0, 0, 0));
    }
}
