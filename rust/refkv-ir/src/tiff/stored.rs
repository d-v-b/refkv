//! The stored arrays of a TIFF file (spec/stored-array.md §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): a description
//! for every IFD that declares an image, keyed by the IFD's struct, and a chunk map row for every
//! tile or strip the IR holds. Built from what the parser read, before its IR is finished; an
//! NDPI has none.

use super::{type_size, unsigned, Ifd, Tiff, Unit};
use crate::array::{AxisUnits, Chain, ChunkMap, Codec, Edge, Fill, Grid, Param, Place, Row, StoredArray};
use crate::ir::Res;
use serde_json::{json, Value as J};
use std::collections::BTreeMap;

/// The tags that declare what an image's elements are, beside the header's byte order:
/// BitsPerSample, PhotometricInterpretation, ColorMap, ExtraSamples, SampleFormat,
/// YCbCrCoefficients, YCbCrSubSampling and ReferenceBlackWhite.
const DECLARING: [u16; 8] = [258, 262, 320, 338, 339, 529, 530, 532];

/// LercParameters: the LERC version, then the compression of the LERC blob (0 none, 1 Deflate,
/// 2 zstd).
const LERC_PARAMETERS: u16 = 50674;

/// The compressions whose decoding restores the stored elements' bytes, and under which libtiff
/// applies a Predictor: LZW, Deflate (both numbers), LZMA and zstd.
fn predicted(compression: u64) -> Option<&'static str> {
    Some(match compression {
        5 => "tiff_lzw",
        8 | 32946 => "zlib",
        34925 => "xz",
        50000 => "zstd",
        _ => return None,
    })
}

fn codec<const N: usize>(name: &str, parameters: [(&str, Option<Param>); N]) -> Codec {
    Codec { name: name.into(), parameters: parameters.into_iter().filter_map(|(k, v)| Some((k.to_string(), v?))).collect() }
}

/// ImageDepth, of the volumes SGI's extension stores as one image.
const IMAGE_DEPTH: u16 = 32997;

/// What an IFD's fields say of how its image is stored.
struct Image {
    w: u64,
    h: u64,
    depth: u64,
    samples: u64,
    planar: u64,
    /// BitsPerSample, where every sample has the same
    bits: Option<u64>,
    /// SampleFormat, where every sample has the same
    format: Option<u64>,
    compression: u64,
    predictor: u64,
}

impl Tiff {
    /// The first value of an unsigned field of the IFD, where the parser holds it.
    fn first(&self, f: &Ifd, tag: u16) -> Option<u64> {
        f.field(tag).filter(|e| unsigned(e.typ)).and_then(|e| self.ints(e, true)).and_then(|v| v.first().copied())
    }

    /// The one value every entry of an unsigned field of the IFD has (`default` for an IFD without
    /// the field); None where they differ, or the parser does not hold them.
    fn same(&self, f: &Ifd, tag: u16, default: u64) -> Option<u64> {
        let Some(e) = f.field(tag) else { return Some(default) };
        let v = self.ints(e, false).filter(|_| unsigned(e.typ))?;
        v.first().copied().filter(|x| v.iter().all(|y| y == x))
    }

    /// The place of a field's value: its `value` element.
    fn value_place(&self, f: &Ifd, tag: u16) -> Option<Place> {
        let e = f.field(tag)?.val?;
        Some(Place::Element { element: e, member: None, range: None })
    }

    /// The place of the first value of an unsigned field, which `first` reads: a parameter a
    /// codec makes from it.
    fn first_param(&self, f: &Ifd, tag: u16) -> Option<Param> {
        let e = f.field(tag).filter(|e| unsigned(e.typ))?;
        let range = (e.count > 1).then_some((0, type_size(e.typ)?));
        Some(Param::Source(Place::Element { element: e.val?, member: None, range }))
    }

    /// The chain of the image's units; None where the IR does not model how they decode (the
    /// array then has no grid).
    fn chain_of_image(&self, f: &Ifd, im: &Image) -> Option<Chain> {
        let bits = im.bits?;
        let source = || vec![codec("source", [("format", Some(Param::Value(json!("tiff")))), ("id", Some(Param::Value(json!(im.compression))))])];
        // the bits a row packs most significant first: another fill order is not modeled
        if self.first(f, 266).unwrap_or(1) != 1 {
            return None;
        }
        let whole = matches!(bits, 8 | 16 | 32 | 64);
        let restoring = im.compression == 1 || im.compression == 32773 || predicted(im.compression).is_some();
        if restoring {
            // stored YCbCr samples that are subsampled are not an array of the image's shape
            let sub = match f.field(530) {
                Some(e) => self.ints(e, false).filter(|_| unsigned(e.typ))?,
                None => vec![2, 2],
            };
            if self.first(f, 262) == Some(6) && sub != [1, 1] {
                return None;
            }
        }
        if let Some(name) = predicted(im.compression) {
            let c = codec(name, []);
            return match im.predictor {
                1 => Some(vec![c]),
                // horizontal differencing of integers, and the floating-point predictor, as
                // libtiff applies them; anything else is one readers disagree on, or refuse
                2 | 3 => {
                    let fits = if im.predictor == 2 { whole && matches!(im.format?, 1 | 2) } else { matches!(bits, 32 | 64) && im.format? == 3 };
                    let predictor = codec(
                        "tiff_predictor",
                        [
                            ("predictor", self.first_param(f, 317)),
                            ("planar_configuration", self.first_param(f, 284).filter(|_| im.samples > 1)),
                            ("byte_order", Some(Param::Source(Place::Element { element: self.order_el, member: None, range: None }))),
                        ],
                    );
                    fits.then(|| vec![predictor, c])
                }
                _ => None,
            };
        }
        // JPEG 2000's streams do not use a predictor, and readers pass over one; under the other
        // compressions libtiff applies none and tifffile applies one, so the bytes are not modeled
        let jpeg2000 = matches!(im.compression, 33003 | 33004 | 33005 | 34712);
        if im.predictor != 1 && !jpeg2000 {
            return None;
        }
        Some(match im.compression {
            1 => Vec::new(),
            32773 => vec![codec("packbits", [])],
            4 if bits == 1 && im.samples == 1 => vec![codec("ccitt_t6", [])],
            7 if bits == 8 => vec![codec("jpeg", [("tables", self.value_place(f, 347).map(Param::Source))])],
            _ if jpeg2000 && matches!(bits, 8 | 16) => vec![codec("jpeg2k", [])],
            50002 | 52546 if matches!(bits, 8 | 16 | 32) => vec![codec("jpegxl", [])],
            50001 if bits == 8 => vec![codec("webp", [])],
            34887 if whole => {
                let lerc = codec("lerc", []);
                let outer = match f.field(LERC_PARAMETERS) {
                    Some(e) => self.ints(e, false).filter(|_| unsigned(e.typ)).and_then(|v| v.get(1).copied()),
                    None => Some(0),
                };
                match outer {
                    Some(0) => vec![lerc],
                    Some(1) => vec![lerc, codec("zlib", [])],
                    Some(2) => vec![lerc, codec("zstd", [])],
                    _ => source(),
                }
            }
            65000 | 65001 if bits == 1 && im.samples == 1 => {
                let (skip, horizontal, vertical) = super::eer_bits(im.compression, |_, d| d);
                let value = |n: u64| Some(Param::Value(json!(n)));
                vec![codec("eer", [("skip_bits", value(skip)), ("horizontal_bits", value(horizontal)), ("vertical_bits", value(vertical))])]
            }
            65002 if bits == 1 && im.samples == 1 => {
                vec![codec("eer", [("skip_bits", self.first_param(f, 65007)), ("horizontal_bits", self.first_param(f, 65008)), ("vertical_bits", self.first_param(f, 65009))])]
            }
            _ => source(),
        })
    }

    /// The grid of the image of IFD `k`, and a row for each unit its table holds (with no array
    /// yet); None where the tables do not cover the image as its fields say, or a unit lies
    /// outside the file.
    fn units_of_image(&self, k: usize, im: &Image) -> Option<(Grid, Vec<Row>)> {
        let f = &self.ifds[k];
        // a volume's tiles and strips are not modeled
        if im.w == 0 || im.h == 0 || im.samples == 0 || !matches!(im.planar, 1 | 2) || im.depth != 1 {
            return None;
        }
        let (tiles, strips) = self.set.tiles.get(&k).copied().unwrap_or((None, None));
        let (s, down, across, edge) = match tiles {
            Some(s) if f.has(322) && f.has(323) => (s, self.first(f, 323)?, self.first(f, 322)?, Edge::Full),
            // a strip is the image's width, of RowsPerStrip rows, at most the image's
            _ => (strips?, self.first(f, 278).unwrap_or(u32::MAX as u64).min(im.h), im.w, Edge::Short),
        };
        if down == 0 || across == 0 {
            return None;
        }
        let (rows, columns) = (im.h.div_ceil(down), im.w.div_ceil(across));
        let planes = if im.samples > 1 && im.planar == 2 { im.samples } else { 1 };
        let units = self.units.get(&s)?;
        if rows.checked_mul(columns)?.checked_mul(planes)? != units.len() as u64 {
            return None;
        }
        let mut axes = vec![AxisUnits::Each(down), AxisUnits::Each(across)];
        if planes > 1 {
            axes.insert(0, AxisUnits::Each(1));
        } else if im.samples > 1 {
            axes.push(AxisUnits::Each(im.samples));
        }
        let mut out = Vec::new();
        for (j, unit) in units.iter().enumerate() {
            let (e, framed) = match *unit {
                Unit::Held(e, framed) => (e, framed),
                Unit::Empty => continue,
                Unit::Outside => return None,
            };
            let j = j as u64;
            let (plane, at) = (j / (rows * columns), j % (rows * columns));
            let mut coords = vec![at / columns, at % columns];
            if planes > 1 {
                coords.insert(0, plane);
            } else if im.samples > 1 {
                coords.push(0);
            }
            // a framed tile's element gives its recipe's bytes: the range of all its extent names
            // the stored ones
            let range = framed.then(|| (0, self.ir.len[e as usize]));
            out.push(Row { coords, element: Some(e), range, ..Row::default() });
        }
        Some((Grid { axes, edge }, out))
    }

    /// The descriptions of the images the IFDs declare, their chains and their rows.
    pub(super) fn described(&self) -> Res<(Vec<StoredArray>, Vec<Chain>, ChunkMap)> {
        let mut arrays: Vec<StoredArray> = Vec::new();
        let mut chains: Vec<Chain> = Vec::new();
        let mut chain_index: BTreeMap<String, u32> = BTreeMap::new();
        let mut map = ChunkMap::default();
        if self.ndpi {
            return Ok((arrays, chains, map));
        }
        let mut ks: Vec<usize> = (0..self.ifds.len()).collect();
        ks.sort_by_key(|&k| self.ifds[k].el);
        for k in ks {
            let f = &self.ifds[k];
            // an IFD that declares no size declares no image
            let (Some(w), Some(h)) = (self.first(f, 256), self.first(f, 257)) else { continue };
            let samples = self.first(f, 277).unwrap_or(1);
            let im = Image {
                w,
                h,
                depth: self.first(f, IMAGE_DEPTH).unwrap_or(1),
                samples,
                planar: if samples > 1 { self.first(f, 284).unwrap_or(1) } else { 1 },
                bits: self.same(f, 258, 1).filter(|&b| b >= 1),
                format: self.same(f, 339, 1),
                compression: self.first(f, 259).unwrap_or(1),
                predictor: self.first(f, 317).unwrap_or(1),
            };
            let planar = im.samples > 1 && im.planar == 2;
            // a volume's planes come before its rows
            let mut shape = if im.depth == 1 { vec![h, w] } else { vec![im.depth, h, w] };
            if planar {
                shape.insert(0, samples);
            } else if samples != 1 {
                shape.push(samples);
            }
            let order = Place::Element { element: self.order_el, member: None, range: None };
            let mut a = StoredArray {
                element: f.el,
                index: None,
                declarations: std::iter::once(order).chain(DECLARING.iter().filter_map(|&t| self.value_place(f, t))).collect(),
                shape: Some(shape),
                axes: None,
                item_bits: im.bits,
                pad_from: None,
                grid: None,
                chains: Vec::new(),
                fill: Fill::Undefined,
                unit_parameters: Vec::new(),
            };
            let stored = self.chain_of_image(f, &im).and_then(|c| Some((c, self.units_of_image(k, &im)?)));
            if let Some((chain, (grid, rows))) = stored {
                let key = J::Array(chain.iter().map(Codec::to_json).collect()).to_string();
                let c = *chain_index.entry(key).or_insert_with(|| {
                    chains.push(chain);
                    (chains.len() - 1) as u32
                });
                a.grid = Some(grid);
                a.chains = vec![c];
                // samples that are not whole bytes pack unbroken within a row, which ends on a byte
                a.pad_from = im.bits.filter(|b| b % 8 != 0).map(|_| if planar { 2 } else { 1 });
                for row in rows {
                    map.push(Row { array: arrays.len() as u32, ..row })?;
                }
            }
            arrays.push(a);
        }
        Ok((arrays, chains, map))
    }
}
