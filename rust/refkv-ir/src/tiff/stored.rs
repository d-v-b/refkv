//! The stored arrays of a TIFF file (spec/stored-array.md §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): a description
//! for every IFD that declares an image, keyed by the IFD's struct, and a chunk map row for every
//! tile or strip the IR holds. Built from what the parser read, before its IR is finished. An
//! NDPI's images are described likewise, but for a JPEG strip McuStarts cuts into restart
//! intervals, each a unit, and a JPEG XR image, whose samples are its stream's.

use super::{type_size, unsigned, Ifd, Tiff, Unit};
use crate::ndpi;
use crate::array::{AxisUnits, Chain, ChunkMap, Codec, Edge, Fill, Grid, Param, Place, Row, StoredArray};
use crate::ir::Res;
use serde_json::{json, Value as J};
use std::collections::{BTreeMap, HashMap};

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

/// What the layout read of an NDPI image's stream, which its description needs and the parser
/// holds no longer once the layout is done.
#[derive(Clone, Copy)]
pub(super) enum ReadStream {
    /// a JPEG strip's header: its length, the rows and columns of a restart interval, and the
    /// frame's components
    Jpeg { header: u64, down: u64, across: u64, components: u64 },
    /// a JPEG XR stream's samples, and their bits
    JpegXr { samples: u64, bits: u64 },
}

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
    /// an NDPI JPEG XR image whose first stream's pixel format gives `samples` and `bits`
    jpegxr: bool,
}

impl Tiff {
    /// The first value of an unsigned field of the IFD, where the parser holds it.
    fn first(&self, f: &Ifd, tag: u16) -> Option<u64> {
        f.field(tag).filter(|e| unsigned(e.typ)).and_then(|e| self.ints(e, true)).and_then(|v| v.first().copied())
    }

    /// The one value every entry of an unsigned field of the IFD has (`default` for an IFD without
    /// the field); None where they differ, or the parser does not hold them.
    fn same(&self, f: &Ifd, tag: u16, default: u64) -> Option<u64> {
        if !f.has(tag) {
            return Some(default);
        }
        let v = self.every(f, tag)?;
        v.first().copied().filter(|x| v.iter().all(|y| y == x))
    }

    /// Every value of an unsigned field of the IFD, where the parser holds them (`ints` reads no
    /// other type's).
    fn every(&self, f: &Ifd, tag: u16) -> Option<Vec<u64>> {
        f.field(tag).filter(|e| unsigned(e.typ)).and_then(|e| self.ints(e, false))
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
        let format = if self.ndpi { "ndpi" } else { "tiff" };
        let source = || vec![codec("source", [("format", Some(Param::Value(json!(format)))), ("id", Some(Param::Value(json!(im.compression))))])];
        // the bits a row packs most significant first: another fill order is not modeled
        if self.first(f, 266).unwrap_or(1) != 1 {
            return None;
        }
        let whole = matches!(bits, 8 | 16 | 32 | 64);
        let restoring = im.compression == 1 || im.compression == 32773 || predicted(im.compression).is_some();
        if restoring {
            // stored YCbCr samples that are subsampled are not an array of the image's shape
            let sub = if f.has(530) { self.every(f, 530)? } else { vec![2, 2] };
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
        // an image codec's stream uses no predictor, and libtiff and tifffile pass one over; under
        // the other compressions libtiff applies none and tifffile applies one, so the bytes are
        // not modeled
        let jpeg2000 = matches!(im.compression, 33003 | 33004 | 33005 | 34712);
        let image_codec = jpeg2000 || im.jpegxr || matches!(im.compression, 7 | 50001 | 50002 | 52546 | 65000..=65002);
        if im.predictor != 1 && !image_codec {
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
            ndpi::JPEGXR if im.jpegxr => vec![codec("jpegxr", [])],
            34887 if whole => {
                let lerc = codec("lerc", []);
                let outer = if f.has(LERC_PARAMETERS) { self.every(f, LERC_PARAMETERS).and_then(|v| v.get(1).copied()) } else { Some(0) };
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
    /// outside the file, or has no bytes at an offset.
    fn units_of_image(&self, k: usize, im: &Image) -> Option<(Grid, Vec<Row>)> {
        let f = &self.ifds[k];
        // a volume's tiles and strips are not modeled
        if im.w == 0 || im.h == 0 || im.samples == 0 || !matches!(im.planar, 1 | 2) || im.depth != 1 {
            return None;
        }
        // an image with a tile's size is in tiles, as libtiff and tifffile read it, which take the
        // strips' tables for its tiles where it has no tables of its own
        let (tiles, strips) = self.set.tiles.get(&k).copied().unwrap_or((None, None));
        let (s, down, across, edge) = if f.has(322) && f.has(323) {
            (tiles.or(strips)?, self.first(f, 323)?, self.first(f, 322)?, Edge::Full)
        } else {
            // a strip is the image's width, of RowsPerStrip rows, at most the image's
            (strips?, self.first(f, 278).unwrap_or(u32::MAX as u64).min(im.h), im.w, Edge::Short)
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
                Unit::Absent => continue,
                Unit::Unsized | Unit::Outside => return None,
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

    /// What the headers the layout read say of each NDPI image's stream, by IFD: taken while the
    /// parser holds those bytes.
    pub(super) fn streams_read(&self) -> HashMap<usize, ReadStream> {
        let read = |k: usize| {
            let f = &self.ifds[k];
            match self.first(f, 259) {
                Some(ndpi::JPEGXR) => self.jpegxr_pixels(k, f).map(|(samples, bits)| ReadStream::JpegXr { samples, bits }),
                Some(7) => self.strip_header(k, f),
                _ => None,
            }
        };
        (0..self.ifds.len()).filter_map(|k| Some((k, read(k)?))).collect()
    }

    /// IFD `k`'s McuStarts with its McuStartsHighBytes, where the parser holds them and they fit
    /// one another (spec/zarrify/ndpi.md §7
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#7-ndpi-profile>).
    fn starts_of_strip(&self, k: usize, f: &Ifd) -> Option<Vec<u64>> {
        let high = if f.has(65432) { Some(self.every(f, 65432)?) } else { None };
        let m = self.mcu_starts(k, self.every(f, 65426)?, high.as_deref());
        (m.same_length && m.high_word).then_some(m.starts)
    }

    /// The header of an NDPI JPEG image's strip, its bytes before the first interval McuStarts
    /// locates, where the layout read it and it is one the profile reads.
    fn strip_header(&self, k: usize, f: &Ifd) -> Option<ReadStream> {
        let first = *self.starts_of_strip(k, f)?.first()?;
        let header = self.heads.get(&(self.first(f, 273)?, first))?;
        let hd = ndpi::header(header, self.rules).ok()?;
        // an interval is `restart` MCUs across and one down; the frame's component count follows
        // its marker, length, precision, height and width
        let components = *header.get(hd.sof.0 + 9)? as u64;
        Some(ReadStream::Jpeg { header: first, down: hd.mh, across: hd.restart.checked_mul(hd.mw)?, components })
    }

    /// The samples of an NDPI JPEG XR image and their bits, as the pixel format of its first
    /// stream gives them (spec/zarrify/ndpi.md §3
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#3-levels>): its first tile
    /// that holds bytes, or its strip. None where the parser did not read that stream's header,
    /// its pixel format is not one the profile reads, or its size is not the unit's.
    fn jpegxr_pixels(&self, k: usize, f: &Ifd) -> Option<(u64, u64)> {
        let (w, h) = (self.first(f, 256)?, self.first(f, 257)?);
        let (at, length, unit) = if f.has(322) && f.has(323) {
            let t = self.ndpi_tiles(k)?;
            let (&o, &c) = t.offsets.iter().zip(&t.counts).find(|x| *x.1 > 0)?;
            (o, c, (self.first(f, 322)?, self.first(f, 323)?))
        } else {
            (self.first(f, 273)?, self.first(f, 279)?, (w, self.first(f, 278).unwrap_or(u32::MAX as u64).min(h)))
        };
        let (guid, width, height) = self.ndpi_stream(at, length).ok()??;
        let (data_type, samples) = ndpi::pixel_format(&guid)?;
        ((width, height) == unit).then_some((samples, if data_type == "uint16" { 16 } else { 8 }))
    }

    /// The restart intervals of an NDPI JPEG image's strip (spec/zarrify/ndpi.md §7
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#7-ndpi-profile>), as
    /// McuStarts locates them: their chain, whose codec has the strip's header, their grid, and a
    /// row for each. None where the image is not one strip of a JPEG stream with McuStarts, or
    /// the parser did not read the strip's header, or the header and the starts do not give the
    /// image's intervals: the strip is then one unit, as a TIFF's.
    fn intervals_of_image(&self, k: usize, im: &Image) -> Option<(Chain, (Grid, Vec<Row>))> {
        let f = &self.ifds[k];
        let cut = self.ndpi && im.compression == 7 && im.bits == Some(8) && im.planar == 1 && im.depth == 1 && im.w > 0 && im.h > 0;
        if !cut || f.has(347) || (f.has(322) && f.has(323)) {
            return None;
        }
        let Some(&ReadStream::Jpeg { header, down, across, components }) = self.ndpi_streams.get(&k) else { return None };
        let starts = self.starts_of_strip(k, f)?;
        let (_, strips) = self.set.tiles.get(&k).copied()?;
        let [Unit::Held(e, false)] = self.units.get(&strips?)?[..] else { return None };
        // the frame's components are the image's samples, and the header the strip's bytes
        // before its first interval
        if components != im.samples || starts.first() != Some(&header) {
            return None;
        }
        let (rows, columns) = (im.h.div_ceil(down), im.w.div_ceil(across));
        let ends = ndpi::interval_ends(self.ir.len[e as usize], columns, rows, &starts)?;
        if starts.iter().zip(&ends).any(|(s, e)| e <= s) {
            return None;
        }
        let place = Place::Element { element: e, member: None, range: Some((0, header)) };
        let chain = vec![codec("jpeg_restart_interval", [("header", Some(Param::Source(place)))])];
        let mut axes = vec![AxisUnits::Each(down), AxisUnits::Each(across)];
        if im.samples > 1 {
            axes.push(AxisUnits::Each(im.samples));
        }
        let units = starts.iter().zip(&ends).enumerate().map(|(i, (&s, &end))| {
            let i = i as u64;
            let mut coords = vec![i / columns, i % columns];
            if im.samples > 1 {
                coords.push(0);
            }
            Row { coords, element: Some(e), range: Some((s, end - s)), ..Row::default() }
        });
        Some((chain, (Grid { axes, edge: Edge::Full }, units.collect())))
    }

    /// The descriptions of the images the IFDs declare, their chains and their rows.
    pub(super) fn described(&self) -> Res<(Vec<StoredArray>, Vec<Chain>, ChunkMap)> {
        let mut arrays: Vec<StoredArray> = Vec::new();
        let mut chains: Vec<Chain> = Vec::new();
        let mut chain_index: BTreeMap<String, u32> = BTreeMap::new();
        let mut map = ChunkMap::default();
        let mut ks: Vec<usize> = (0..self.ifds.len()).collect();
        ks.sort_by_key(|&k| self.ifds[k].el);
        for k in ks {
            let f = &self.ifds[k];
            // an IFD that declares no size declares no image
            let (Some(w), Some(h)) = (self.first(f, 256), self.first(f, 257)) else { continue };
            let compression = self.first(f, 259).unwrap_or(1);
            // an NDPI's JPEG XR streams hold other samples than its fields say: the stream's are
            // the image's
            let jpegxr = match self.ndpi_streams.get(&k) {
                Some(&ReadStream::JpegXr { samples, bits }) if compression == ndpi::JPEGXR => Some((samples, bits)),
                _ => None,
            };
            let (samples, bits) = match jpegxr {
                Some((samples, bits)) => (samples, Some(bits)),
                None => (self.first(f, 277).unwrap_or(1), self.same(f, 258, 1).filter(|&b| b >= 1)),
            };
            let im = Image {
                w,
                h,
                depth: self.first(f, IMAGE_DEPTH).unwrap_or(1),
                samples,
                planar: if samples > 1 && jpegxr.is_none() { self.first(f, 284).unwrap_or(1) } else { 1 },
                bits,
                format: self.same(f, 339, 1),
                compression,
                predictor: self.first(f, 317).unwrap_or(1),
                jpegxr: jpegxr.is_some(),
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
            let stored = self.chain_of_image(f, &im).and_then(|c| match self.intervals_of_image(k, &im) {
                Some(cut) => Some(cut),
                None => Some((c, self.units_of_image(k, &im)?)),
            });
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
