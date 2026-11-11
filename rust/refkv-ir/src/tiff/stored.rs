//! The stored arrays of a TIFF file (spec/stored-array.md §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): a description
//! for every IFD that declares an image, keyed by the IFD's struct, and a chunk map row for every
//! tile or strip the IR holds. Built from what the parser read, before its IR is finished. An
//! NDPI's images are described likewise, but for a JPEG strip McuStarts cuts into restart
//! intervals, each a unit, and a JPEG XR image, whose samples are its stream's.
//!
//! What an IFD's fields give of its image (its shape, its chain, its grid and its rows) is read
//! through [`Fields`], so that a parser that reads IFDs with types of its own (VSI's,
//! `vsi/stored.rs`) describes its IFDs' images by the same rules.

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
pub(crate) struct Image {
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

/// An IFD as its image's description reads it: its fields' unsigned values and the places of
/// their values, as the parser that read the IFD holds them. A field is the first of its tag.
/// The TIFF parser's IFDs are read through it (`Read`), and so are those of a parser that reads
/// IFDs with types of its own (VSI's, `vsi/stored.rs`), whose images are so described by the
/// same rules.
pub(crate) trait Fields {
    fn has(&self, tag: u16) -> bool;
    /// The first value of an unsigned field of the IFD, where the parser holds it.
    fn first(&self, tag: u16) -> Option<u64>;
    /// Every value of an unsigned field of the IFD, where the parser holds them.
    fn every(&self, tag: u16) -> Option<Vec<u64>>;
    /// The place of a field's value: its `value` element.
    fn value_place(&self, tag: u16) -> Option<Place>;
    /// The place of the first value of an unsigned field, which `first` reads: a parameter a
    /// codec makes from it.
    fn first_param(&self, tag: u16) -> Option<Param>;
    /// The place of the file's byte order mark.
    fn byte_order(&self) -> Place;
    /// The `format` of a `source` codec of the file's images.
    fn format(&self) -> &'static str;

    /// The one value every entry of an unsigned field of the IFD has (`default` for an IFD without
    /// the field); None where they differ, or the parser does not hold them.
    fn same(&self, tag: u16, default: u64) -> Option<u64> {
        if !self.has(tag) {
            return Some(default);
        }
        let v = self.every(tag)?;
        v.first().copied().filter(|x| v.iter().all(|y| y == x))
    }
}

/// An IFD of the TIFF parser's.
struct Read<'a>(&'a Tiff, &'a Ifd);

impl Fields for Read<'_> {
    fn has(&self, tag: u16) -> bool {
        self.1.has(tag)
    }
    fn first(&self, tag: u16) -> Option<u64> {
        self.0.first(self.1, tag)
    }
    fn every(&self, tag: u16) -> Option<Vec<u64>> {
        self.0.every(self.1, tag)
    }
    fn value_place(&self, tag: u16) -> Option<Place> {
        self.0.value_place(self.1, tag)
    }
    fn first_param(&self, tag: u16) -> Option<Param> {
        self.0.first_param(self.1, tag)
    }
    fn byte_order(&self) -> Place {
        Place::Element { element: self.0.order_el, member: None, range: None }
    }
    fn format(&self) -> &'static str {
        if self.0.ndpi { "ndpi" } else { "tiff" }
    }
}

/// Whether the IFD's image is in tiles: it has a tile's size, as libtiff and tifffile read it.
pub(crate) fn tiled(f: &dyn Fields) -> bool {
    f.has(322) && f.has(323)
}

impl Image {
    /// What the IFD's fields say of its image; None for an IFD that declares no size, which
    /// declares no image. `stream` is the samples of an NDPI JPEG XR image's streams and their
    /// bits, where the streams hold other samples than the fields say: the stream's are the
    /// image's.
    pub(crate) fn of(f: &dyn Fields, stream: Option<(u64, u64)>) -> Option<Image> {
        let (w, h) = (f.first(256)?, f.first(257)?);
        let (samples, bits) = match stream {
            Some((samples, bits)) => (samples, Some(bits)),
            None => (f.first(277).unwrap_or(1), f.same(258, 1).filter(|&b| b >= 1)),
        };
        Some(Image {
            w,
            h,
            depth: f.first(IMAGE_DEPTH).unwrap_or(1),
            samples,
            planar: if samples > 1 && stream.is_none() { f.first(284).unwrap_or(1) } else { 1 },
            bits,
            format: f.same(339, 1),
            compression: f.first(259).unwrap_or(1),
            predictor: f.first(317).unwrap_or(1),
            jpegxr: stream.is_some(),
        })
    }

    /// Whether each sample is a plane of its own, whose units the tables list apart.
    fn planes(&self) -> bool {
        self.samples > 1 && self.planar == 2
    }

    /// The image's description before its grid, keyed by its IFD's element `element`: its
    /// declarations, its shape and its samples' bits.
    pub(crate) fn array(&self, f: &dyn Fields, element: u32) -> StoredArray {
        // a volume's planes come before its rows
        let mut shape = if self.depth == 1 { vec![self.h, self.w] } else { vec![self.depth, self.h, self.w] };
        if self.planes() {
            shape.insert(0, self.samples);
        } else if self.samples != 1 {
            shape.push(self.samples);
        }
        StoredArray {
            element,
            index: None,
            declarations: std::iter::once(f.byte_order()).chain(DECLARING.iter().filter_map(|&t| f.value_place(t))).collect(),
            shape: Some(shape),
            axes: None,
            item_bits: self.bits,
            pad_from: None,
            grid: None,
            chains: Vec::new(),
            fill: Fill::Undefined,
            unit_parameters: Vec::new(),
        }
    }
}

/// The descriptions a parser builds, their chains, each once, and their rows.
#[derive(Default)]
pub(crate) struct Descriptions {
    pub(crate) arrays: Vec<StoredArray>,
    pub(crate) chains: Vec<Chain>,
    chain_index: BTreeMap<String, u32>,
    pub(crate) map: ChunkMap,
}

impl Descriptions {
    /// The position of `chain` among the chains, which it joins if it is not there.
    pub(crate) fn chain(&mut self, chain: Chain) -> u32 {
        let key = J::Array(chain.iter().map(Codec::to_json).collect()).to_string();
        *self.chain_index.entry(key).or_insert_with(|| {
            self.chains.push(chain);
            (self.chains.len() - 1) as u32
        })
    }

    /// Adds the description `a` of an IFD's image `im`, with its chain, its grid and its rows
    /// where the IR models how the image is stored.
    pub(crate) fn image(&mut self, mut a: StoredArray, im: &Image, stored: Option<(Chain, (Grid, Vec<Row>))>) -> Res<()> {
        if let Some((chain, (grid, rows))) = stored {
            let c = self.chain(chain);
            a.grid = Some(grid);
            a.chains = vec![c];
            // samples that are not whole bytes pack unbroken within a row, which ends on a byte
            a.pad_from = im.bits.filter(|b| b % 8 != 0).map(|_| if im.planes() { 2 } else { 1 });
            for row in rows {
                self.map.push(Row { array: self.arrays.len() as u32, ..row })?;
            }
        }
        self.arrays.push(a);
        Ok(())
    }
}

impl Tiff {
    /// The first value of an unsigned field of the IFD, where the parser holds it.
    fn first(&self, f: &Ifd, tag: u16) -> Option<u64> {
        f.field(tag).filter(|e| unsigned(e.typ)).and_then(|e| self.ints(e, true)).and_then(|v| v.first().copied())
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
    pub(crate) fn chain_of_image(f: &dyn Fields, im: &Image) -> Option<Chain> {
        let bits = im.bits?;
        let format = f.format();
        let source = || vec![codec("source", [("format", Some(Param::Value(json!(format)))), ("id", Some(Param::Value(json!(im.compression))))])];
        // the bits a row packs most significant first: another fill order is not modeled
        if f.first(266).unwrap_or(1) != 1 {
            return None;
        }
        let whole = matches!(bits, 8 | 16 | 32 | 64);
        let restoring = im.compression == 1 || im.compression == 32773 || predicted(im.compression).is_some();
        if restoring {
            // stored YCbCr samples that are subsampled are not an array of the image's shape
            let sub = if f.has(530) { f.every(530)? } else { vec![2, 2] };
            if f.first(262) == Some(6) && sub != [1, 1] {
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
                            ("predictor", f.first_param(317)),
                            ("planar_configuration", f.first_param(284).filter(|_| im.samples > 1)),
                            ("byte_order", Some(Param::Source(f.byte_order()))),
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
            7 if bits == 8 => vec![codec("jpeg", [("tables", f.value_place(347).map(Param::Source))])],
            _ if jpeg2000 && matches!(bits, 8 | 16) => vec![codec("jpeg2k", [])],
            50002 | 52546 if matches!(bits, 8 | 16 | 32) => vec![codec("jpegxl", [])],
            50001 if bits == 8 => vec![codec("webp", [])],
            ndpi::JPEGXR if im.jpegxr => vec![codec("jpegxr", [])],
            34887 if whole => {
                let lerc = codec("lerc", []);
                let outer = if f.has(LERC_PARAMETERS) { f.every(LERC_PARAMETERS).and_then(|v| v.get(1).copied()) } else { Some(0) };
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
                vec![codec("eer", [("skip_bits", f.first_param(65007)), ("horizontal_bits", f.first_param(65008)), ("vertical_bits", f.first_param(65009))])]
            }
            _ => source(),
        })
    }

    /// The entries of the table of IFD `k`'s image, where the parser holds one: its tiles' for an
    /// image in tiles that has them, else its strips'.
    fn table_of_image(&self, k: usize, tiled: bool) -> Option<&[Unit]> {
        let (tiles, strips) = self.set.tiles.get(&k).copied().unwrap_or((None, None));
        let s = if tiled { tiles.or(strips)? } else { strips? };
        self.units.get(&s).map(Vec::as_slice)
    }

    /// The grid of an image, and a row for each unit its table holds (with no array yet); None
    /// where the table does not cover the image as its fields say, or a unit lies outside the
    /// file, or has no bytes at an offset. `table` is the entries of the image's table where the
    /// parser holds one, its offsets and byte counts of one length: the tiles' for an image in
    /// tiles ([`tiled`]) that has them, else the strips'. `length` gives the bytes of a unit's
    /// element.
    pub(crate) fn units_of_image(f: &dyn Fields, im: &Image, table: Option<&[Unit]>, length: &dyn Fn(u32) -> u64) -> Option<(Grid, Vec<Row>)> {
        // a volume's tiles and strips are not modeled
        if im.w == 0 || im.h == 0 || im.samples == 0 || !matches!(im.planar, 1 | 2) || im.depth != 1 {
            return None;
        }
        // an image with a tile's size is in tiles, as libtiff and tifffile read it, which take the
        // strips' tables for its tiles where it has no tables of its own
        let (down, across, edge) = if tiled(f) {
            (f.first(323)?, f.first(322)?, Edge::Full)
        } else {
            // a strip is the image's width, of RowsPerStrip rows, at most the image's
            (f.first(278).unwrap_or(u32::MAX as u64).min(im.h), im.w, Edge::Short)
        };
        if down == 0 || across == 0 {
            return None;
        }
        let (rows, columns) = (im.h.div_ceil(down), im.w.div_ceil(across));
        let planes = if im.samples > 1 && im.planar == 2 { im.samples } else { 1 };
        let units = table?;
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
            let range = framed.then(|| (0, length(e)));
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
        // the frame's components are the image's samples
        if components != im.samples {
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
        let mut out = Descriptions::default();
        let mut ks: Vec<usize> = (0..self.ifds.len()).collect();
        ks.sort_by_key(|&k| self.ifds[k].el);
        for k in ks {
            let f = &self.ifds[k];
            let read = Read(self, f);
            // an NDPI's JPEG XR streams hold other samples than its fields say: the stream's are
            // the image's
            let jpegxr = match self.ndpi_streams.get(&k) {
                Some(&ReadStream::JpegXr { samples, bits }) if self.first(f, 259).unwrap_or(1) == ndpi::JPEGXR => Some((samples, bits)),
                _ => None,
            };
            // an IFD that declares no size declares no image
            let Some(im) = Image::of(&read, jpegxr) else { continue };
            let a = im.array(&read, f.el);
            let stored = Self::chain_of_image(&read, &im).and_then(|c| match self.intervals_of_image(k, &im) {
                Some(cut) => Some(cut),
                None => Some((c, Self::units_of_image(&read, &im, self.table_of_image(k, tiled(&read)), &|e| self.ir.len[e as usize])?)),
            });
            // the planes after the first of an ImageJ stack in one IFD (spec/stored-array.md §7
            // <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>):
            // each IFD 0's image again, keyed by its `imagej/planes/<p>`, with the ImageDescription
            // as one more declaration, its rows its own strips
            let planes = if self.chain.first() == Some(&k) { &self.imagej_planes[..] } else { &[] };
            let mut later = Vec::new();
            for (plane, strips) in planes {
                let declarations = a.declarations.iter().cloned().chain(self.value_place(f, 270)).collect();
                let shifted = match &stored {
                    // each of IFD 0's rows is one whole strip's element, as its uncompressed strips
                    // give them: the plane's row is its own strip's
                    Some((_, (_, rows))) if rows.len() != strips.len() || rows.iter().any(|r| r.member.is_some() || r.range.is_some() || r.ranges.is_some()) => {
                        return Err("internal: an ImageJ plane whose strips are not IFD 0's".into())
                    }
                    Some((chain, (grid, rows))) => {
                        let rows = rows.iter().zip(strips).map(|(row, &e)| Row { element: Some(e), ..row.clone() }).collect();
                        Some((chain.clone(), (grid.clone(), rows)))
                    }
                    None => None,
                };
                later.push((StoredArray { element: *plane, declarations, ..a.clone() }, shifted));
            }
            out.image(a, &im, stored)?;
            for (a, stored) in later {
                out.image(a, &im, stored)?;
            }
        }
        Ok((out.arrays, out.chains, out.map))
    }
}
