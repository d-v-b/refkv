//! refkv's plan of a TIFF, QPTIFF or NDPI image (spec/zarrify/tiff.md §4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#4-the-image>,
//! spec/zarrify/qptiff.md §4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/qptiff.md#4-the-image>,
//! spec/zarrify/ndpi.md §3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#3-levels>):
//! the decisions of refkv's convention, made from the parser's description of the file
//! (spec/zarrify/tiff.md §7.6 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#76-the-description>)
//! and nothing else: which IFDs are the levels and the planes, and how OME-XML's TiffData, an
//! ImageJ hyperstack or a Micro-Manager index map maps the planes to them; the image's checks
//! (spec/zarrify/tiff.md §7.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#72-rejection>);
//! how strips are joined, padded and sharded (§4.5
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#45-strips>); and the scales and
//! position, converted to the axes' units. The plan is public data, which the convention's
//! projections (`project/tiff.rs`, `project/qptiff.rs`, `project/ndpi.rs`) read; it belongs to
//! refkv's convention, not to the IR.
//!
//! It lives beside the parser, whose helpers of TIFF's structures it shares, and reads none of the
//! parser's state: a `Src` is made from a description alone, and gives the parser's helpers' names
//! over the description's values, so that the checks run in the profile's order.

use super::*;

/// refkv's plan of the image a description describes, of profile `profile` (`tiff`, `qptiff` or
/// `ndpi`): its levels, each with its planes' tiles or strips and the IFDs that declare them, its
/// sizes and axes, its data type's and codecs' fields, its strips' joins, pads and shards, its
/// scales, units and translation, and its channels' names and colors. An error for an input the
/// convention rejects, with the profile's message.
pub fn plan(profile: &str, description: &J) -> Result<J, String> {
    let s = Src::of(profile, description)?;
    let done = match profile {
        "ndpi" => s.derive_ndpi(),
        "qptiff" => s.derive_qptiff(),
        _ => s.derive(),
    };
    match done {
        Ok(p) => Ok(p),
        Err(Stop::Reject(m)) => Err(m),
        Err(_) => Err("internal: the plan asked for bytes the description does not hold".into()),
    }
}

/// Makes the LZW, Deflate and zstd strips of each level the inner chunks of shards of at most
/// 2^10 strips and 2^30 decoded bytes, as even as whole strips allow (spec/zarrify/tiff.md §4.5
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#45-strips>): each strip stays a
/// stream of its own, which the sharding codec finds through the shard's index. A level stays
/// one strip a chunk when it has one strip, when two do not fit, or when its last strip is short
/// and cannot be padded: the sharding codec refuses the `rectilinear` grid that then needs.
pub(super) fn shard_strips(levels: &mut [Level], bits: u64, spp: u64, planar: u64, compression: u64) {
    if !matches!(compression, 5 | 8 | 32946 | 50000) {
        return;
    }
    let contig = spp > 1 && planar == 1;
    for lv in levels.iter_mut().filter(|lv| lv.strips) {
        let row = row_size(lv.w, bits, if contig { spp } else { 1 });
        let strips = lv.h.div_ceil(lv.th);
        let most = (MAX_SHARD_BYTES / (lv.th as u128 * row)).min(MAX_JOINED_STRIPS) as u64;
        if strips >= 2 && most >= 2 && (compression == 5 || lv.h % lv.th == 0) {
            lv.shard = strips.div_ceil(strips.div_ceil(most));
        }
    }
}

/// The bytes of a base64 text (RFC 4648, with padding), as the description writes them.
pub(crate) fn unbase64(s: &str) -> Option<Vec<u8>> {
    let value = |c: u8| match c {
        b'A'..=b'Z' => Some(c - b'A'),
        b'a'..=b'z' => Some(c - b'a' + 26),
        b'0'..=b'9' => Some(c - b'0' + 52),
        b'+' => Some(62),
        b'/' => Some(63),
        _ => None,
    };
    let b = s.trim_end_matches('=').as_bytes();
    let mut out = Vec::with_capacity(b.len() * 3 / 4);
    let (mut acc, mut bits) = (0u32, 0);
    for &c in b {
        acc = ((acc << 6) | value(c)? as u32) & 0xFFFF;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((acc >> bits) as u8);
        }
    }
    Some(out)
}

/// A float as the description writes it (`project::float_json`).
fn float_of(v: &J) -> Option<f64> {
    match v {
        J::Number(n) => n.as_f64(),
        J::String(s) => match s.as_str() {
            "NaN" => Some(f64::NAN),
            "Infinity" => Some(f64::INFINITY),
            "-Infinity" => Some(f64::NEG_INFINITY),
            _ => None,
        },
        J::Object(o) => {
            let hex = o.get("bits")?.as_str()?;
            let bits = u64::from_str_radix(hex, 16).ok()?;
            Some(if hex.len() == 8 { f32::from_bits(bits as u32) as f64 } else { f64::from_bits(bits) })
        }
        _ => None,
    }
}

fn pairs_of(v: &J) -> Vec<(String, String)> {
    v.as_object().into_iter().flatten().map(|(k, v)| (k.clone(), v.as_str().unwrap_or("").to_string())).collect()
}

fn u(v: &J) -> u64 {
    v.as_u64().unwrap_or(0)
}

/// An entry of an IFD, as the description holds it.
pub(super) struct PEnt<'a> {
    pub typ: u16,
    pub count: u64,
    pub value: &'a J,
}

/// An IFD, as the description holds it.
pub(super) struct PIfd<'a> {
    pub offset: u64,
    pub el: u32,
    pub subs: Vec<usize>,
    /// its tags, in the order of its entries
    tags: Vec<(u16, PEnt<'a>)>,
    pub tiles: Option<u32>,
    pub strips: Option<u32>,
    /// the description's object of it
    pub d: &'a J,
}

impl<'a> PIfd<'a> {
    pub fn field(&self, tag: u16) -> Option<&PEnt<'a>> {
        self.tags.iter().find(|(t, _)| *t == tag).map(|(_, e)| e)
    }
    pub fn has(&self, tag: u16) -> bool {
        self.field(tag).is_some()
    }
}

/// The file a description describes, as the plan reads it.
pub(super) struct Src<'a> {
    pub rules: &'static Rules,
    pub le: bool,
    pub big: bool,
    pub size: u64,
    pub qptiff: bool,
    pub ifds: Vec<PIfd<'a>>,
    pub chain: Vec<usize>,
    pub d: &'a J,
    /// of an image across files (spec/zarrify/tiff.md §7.4
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#74-several-files>): the
    /// named file's key, and each other file's key and description
    pub named: Option<String>,
    pub siblings: Vec<(String, Src<'a>)>,
    /// the description's short last LZW strips, by (IFD, strip)
    tails: HashMap<(usize, usize), &'a J>,
}

impl<'a> Src<'a> {
    /// The file `d` describes, of the profile `profile` (`tiff`, `qptiff` or `ndpi`).
    pub fn of(profile: &str, d: &'a J) -> Result<Src<'a>, String> {
        let ndpi = profile == "ndpi";
        let ifds: Vec<PIfd<'a>> = d["ifds"]
            .as_array()
            .ok_or("internal: a description without IFDs")?
            .iter()
            .map(|f| PIfd {
                offset: u(&f["offset"]),
                el: u(&f["element"]) as u32,
                subs: f["subifds"].as_array().into_iter().flatten().map(|x| u(x) as usize).collect(),
                tags: f["tags"]
                    .as_object()
                    .into_iter()
                    .flatten()
                    .filter_map(|(k, e)| {
                        let tag = k.parse::<u16>().ok()?;
                        Some((tag, PEnt { typ: u(&e["type"]) as u16, count: u(&e["count"]), value: &e["value"] }))
                    })
                    .collect(),
                tiles: f["tiles"].as_u64().map(|x| x as u32),
                strips: f["strips"].as_u64().map(|x| x as u32),
                d: f,
            })
            .collect();
        let chain = if ndpi {
            (0..ifds.len()).collect()
        } else {
            d["chain"].as_array().into_iter().flatten().map(|x| u(x) as usize).collect()
        };
        let mut siblings = Vec::new();
        for f in d["files"].as_array().into_iter().flatten() {
            let key = f["key"].as_str().unwrap_or("").to_string();
            siblings.push((key, Src::of(profile, &f["description"])?));
        }
        Ok(Src {
            rules: rules::of(profile),
            le: d["byte_order"] == "little",
            big: d["bigtiff"].as_bool().unwrap_or(false),
            size: u(&d["size"]),
            qptiff: profile == "qptiff",
            ifds,
            chain,
            d,
            named: d["named_key"].as_str().map(str::to_string),
            siblings,
            tails: d["lzw_tails"].as_array().into_iter().flatten().map(|t| ((u(&t["ifd"]) as usize, u(&t["strip"]) as usize), t)).collect(),
        })
    }

    fn within(&self, o: u64, n: u64) -> bool {
        o.checked_add(n).is_some_and(|e| e <= self.size)
    }

    /// A tag of the table whose values the layout reads for every IFD it uses.
    fn used(&self, tag: u16) -> bool {
        if self.qptiff { QPTIFF_TABLE.contains(&tag) && tag != 270 && tag != 330 } else { is_used(tag) }
    }

    /// An entry's integers (the first only, with `first`), as the profile reads them; None for a
    /// value the description does not hold as integers.
    fn ints(&self, e: &PEnt, first: bool) -> Option<Vec<u64>> {
        let v: Vec<u64> = e.value.as_array()?.iter().map(|x| x.as_u64()).collect::<Option<_>>()?;
        Some(if first { v.into_iter().take(1).collect() } else { v })
    }

    /// The profile's `load`: the values of the table's tags an IFD's layout reads (all of them, or
    /// all but its tile tables), each integer tag's checked (the `values` role).
    fn load(&self, ks: &[usize], tiles: bool, _description: bool) -> Got<Vec<Vals>> {
        let mut out = Vec::with_capacity(ks.len());
        for &k in ks {
            let mut m = Vals::new();
            for (tag, e) in &self.ifds[k].tags {
                let tag = *tag;
                if !(self.used(tag) && (tiles || !matches!(tag, 324 | 325 | 273 | 279))) {
                    continue;
                }
                let v = match tag {
                    347 => Loaded::Bytes(e.value.as_str().and_then(unbase64).unwrap_or_default()),
                    282 | 283 | 286 | 287 => {
                        Loaded::Pairs(e.value.as_array().into_iter().flatten().map(|p| (u(&p[0]), u(&p[1]))).collect())
                    }
                    _ => Loaded::Ints(self.ints(e, false).unwrap_or_default()),
                };
                if let Loaded::Ints(v) = &v {
                    let mut env = rules::env([
                        ("tag", int(tag as i128)),
                        ("type", int(e.typ as i128)),
                        ("largest", num(v.iter().copied().max().unwrap_or(0) as f64)),
                    ]);
                    self.rules.check("values", &mut env)?;
                }
                m.insert(tag, v);
            }
            out.push(m);
        }
        Ok(out)
    }

    /// The main chain's length of file `f`, the named file being 0.
    fn chain_of(&self, f: usize) -> usize {
        match f {
            0 => self.chain.len(),
            f => self.siblings.get(f - 1).map_or(0, |s| s.1.chain.len()),
        }
    }

    /// The first IFD's OME-XML, as the description holds it.
    fn ome(&self) -> Option<Ome> {
        let o = self.d.get("ome").filter(|o| o.is_object())?;
        let mut uuid_texts = HashMap::new();
        let tiff_data = o["tiff_data"]
            .as_array()
            .into_iter()
            .flatten()
            .enumerate()
            .map(|(k, t)| {
                if let Some(s) = t["uuid"].as_str() {
                    uuid_texts.insert(k, s.to_string());
                }
                (pairs_of(&t["attributes"]), t["file"].as_str().map(str::to_string))
            })
            .collect();
        Some(Ome {
            name: o["name"].as_str().map(str::to_string),
            pixels: pairs_of(&o["pixels"]),
            tiff_data,
            uuid: o["uuid"].as_str().map(str::to_string),
            uuid_texts,
            plane: o["plane"].is_object().then(|| pairs_of(&o["plane"])),
            channels: o["channels"].as_array().into_iter().flatten().map(pairs_of).collect(),
        })
    }

    /// The Micro-Manager header, as the description holds it.
    fn micromanager(&self) -> Option<Mm> {
        let m = self.d.get("micromanager").filter(|m| m.is_object())?;
        let entries = m["index_map"]
            .as_array()
            .into_iter()
            .flatten()
            .map(|e| MmEntry {
                channel: u(&e[0]) as u32,
                slice: u(&e[1]) as u32,
                frame: u(&e[2]) as u32,
                position: u(&e[3]) as u32,
                ifd: u(&e[4]) as u32,
            })
            .collect();
        Some(Mm { summary: m["summary"].clone(), entries })
    }

    /// The ImageJ hyperstack, as the description holds it.
    fn imagej(&self) -> Option<ImageJ> {
        let ij = self.d.get("imagej").filter(|x| x.is_object())?;
        Some(ImageJ { images: u(&ij["images"]), sizes: (u(&ij["frames"]), u(&ij["channels"]), u(&ij["slices"])), plane_bytes: ij["plane_bytes"].as_u64() })
    }

    /// Each TiffData's file (0: the named one; k: the k-th other file) of a document that names
    /// several, as the parser found them (`Tiff::ome_files`).
    fn ome_files(&self, o: &Ome) -> Got<Vec<usize>> {
        let Some(own) = &self.named else {
            return Err(Stop::Reject("internal: a document that names other files, read as a file".into()));
        };
        let keys: Vec<&str> = self.siblings.iter().map(|s| s.0.as_str()).collect();
        let mut files = Vec::with_capacity(o.tiff_data.len());
        for (k, (_, id)) in o.tiff_data.iter().enumerate() {
            let file = match id {
                None => 0,
                Some(id) if Some(id) == o.uuid_texts.get(&k) => 0,
                Some(id) => {
                    let key = crate::beside::written_key(id.as_bytes(), "", Default::default()).unwrap_or_default();
                    if key == *own { 0 } else { keys.iter().position(|w| *w == key).map_or(0, |i| i + 1) }
                }
            };
            files.push(file);
        }
        Ok(files)
    }

    /// The tail of strip `j` of IFD `k` as the description holds it: Ok((prefix, tail)), or
    /// Err(why its stream does not reach its rows); None where it holds none.
    fn lzw_tail(&self, k: usize, j: usize) -> Option<Result<(u64, Vec<u8>), String>> {
        let t = self.tails.get(&(k, j))?;
        Some(match t["problem"].as_str() {
            Some(p) => Err(p.to_string()),
            None => Ok((u(&t["prefix"]), t["tail"].as_str().and_then(unbase64).unwrap_or_default())),
        })
    }

    /// IFD `k`'s tags of NDPI's table (those of `only`, or all), as the description holds them:
    /// a scalar's first value, an array's every value; with `check`, an integer tag's values are
    /// checked (the `values` role).
    fn nload(&self, k: usize, only: Option<&[u16]>, _need: &mut Vec<(u64, u64)>, check: bool) -> Res<NVals> {
        let mut out = NVals::new();
        for (tag, e) in &self.ifds[k].tags {
            let tag = *tag;
            if !NDPI_TABLE.contains(&tag) || only.is_some_and(|o| !o.contains(&tag)) {
                continue;
            }
            let a = e.value.as_array().cloned().unwrap_or_default();
            let v = match e.typ {
                1 | 3 | 4 | 13 | 16 | 18 => {
                    let v: Vec<u64> = a.iter().map(u).collect();
                    if check {
                        let safe = v.iter().all(|&x| x <= MAX_SAFE);
                        self.rules.check("values", rules::env([("tag", int(tag as i128)), ("safe", flag(safe))]))?;
                    }
                    Nv::Ints(v)
                }
                8 | 9 => Nv::Signed(a.iter().map(|x| x.as_i64().unwrap_or(0)).collect()),
                11 | 12 => Nv::Floats(a.iter().map(|x| float_of(x).unwrap_or(f64::NAN)).collect()),
                5 => Nv::Rats(a.iter().map(|p| (u(&p[0]), u(&p[1]))).collect()),
                _ => return Err("internal: an NDPI tag of an unexpected type".into()),
            };
            out.insert(tag, v);
        }
        Ok(out)
    }

    /// A tag's first value, or the `required` role's rejection naming it.
    fn nreq(&self, m: &NVals, tag: u16, what: &str) -> Res<f64> {
        match m.get(&tag).and_then(|v| v.first()) {
            Some(x) => Ok(x),
            None => {
                self.rules.check("required", rules::env([("what", text(what)), ("present", flag(false))]))?;
                Ok(0.0)
            }
        }
    }

    fn nint(m: &NVals, tag: u16) -> Option<u64> {
        m.get(&tag).and_then(|v| v.ints()).and_then(|v| v.first().copied())
    }

    /// An NDPI image's tiles with their high bytes, as the description holds them.
    fn ndpi_tiles(&self, k: usize) -> Option<NdpiTiles> {
        let t = self.ifds[k].d.get("tile_tables").filter(|t| t.is_object())?;
        let list = |v: &J| v.as_array().into_iter().flatten().map(u).collect::<Vec<u64>>();
        Some(NdpiTiles { offsets: list(&t["offsets"]), counts: list(&t["byte_counts"]), matched: t["high_tables_fit"].as_bool().unwrap_or(false), added: Vec::new() })
    }

    /// A JPEG XR image's first stream, `[offset, length]`.
    fn ndpi_first(&self, k: usize) -> Option<(u64, u64)> {
        let f = self.ifds[k].d.get("jpegxr")?.get("first")?.as_array()?;
        Some((u(&f[0]), u(&f[1])))
    }

    /// A JPEG XR image's first stream's header: its pixel format, width and height.
    fn ndpi_stream(&self, k: usize) -> Option<ndpi::Stream> {
        let h = self.ifds[k].d.get("jpegxr")?.get("header").filter(|h| h.is_object())?;
        let hex = h["pixel_format"].as_str()?;
        let mut format = [0u8; 16];
        for (i, b) in format.iter_mut().enumerate() {
            *b = u8::from_str_radix(hex.get(2 * i..2 * i + 2)?, 16).ok()?;
        }
        Some((format, u(&h["width"]), u(&h["height"])))
    }

    /// A JPEG strip's bytes before its first restart interval.
    fn jpeg_header(&self, k: usize) -> Option<Vec<u8>> {
        self.ifds[k].d.get("jpeg_header")?.as_str().and_then(unbase64)
    }

    /// A JPEG strip's frame header, without McuStarts.
    fn ndpi_frame(&self, k: usize) -> Option<ndpi::Frame> {
        let f = self.ifds[k].d.get("jpeg_frame").filter(|f| f.is_object())?;
        Some(ndpi::Frame { at: u(&f["at"]), height: u(&f["height"]), width: u(&f["width"]), mw: u(&f["mcu_width"]), mh: u(&f["mcu_height"]) })
    }

    /// An image's focal offset in nanometers.
    fn ndpi_focal(&self, k: usize) -> Option<i64> {
        self.ifds[k].d.get("focal_offset")?.as_i64()
    }

    /// An image's acquisition tags as the root's source metadata, and the Fluorescence a single
    /// channel is labeled with: its text, when UTF-8, not empty and at most `MAX_NAME` bytes.
    fn ndpi_acquisition(&self, k: usize) -> (serde_json::Map<String, J>, Option<String>) {
        let own = self.ifds[k].d.get("acquisition").and_then(|a| a.as_object()).cloned().unwrap_or_default();
        let label = own.get("Fluorescence").and_then(|v| v.as_str()).filter(|t| !t.is_empty() && t.len() <= crate::project::MAX_NAME).map(str::to_string);
        (own, label)
    }

    /// An image's McuStarts with McuStartsHighBytes, as the description holds them.
    fn mcu_starts(&self, k: usize) -> Option<McuStarts> {
        let x = self.ifds[k].d.get("mcu_starts").filter(|x| x.is_object())?;
        Some(McuStarts {
            starts: x["starts"].as_array().into_iter().flatten().map(u).collect(),
            same_length: x["same_length"].as_bool().unwrap_or(false),
            high_word: x["high_word"].as_bool().unwrap_or(false),
            added: x["high_added"].as_bool().unwrap_or(false),
        })
    }

    /// An entry's elements for the projection (`Tiff::ent_facts`), as the description holds them.
    fn ent_facts(&self, k: usize, tag: u16) -> J {
        let name = if tag == 65426 { "mcu_starts" } else { "mcu_starts_high" };
        self.ifds[k].d.get("elements").map_or(J::Null, |e| e[name].clone())
    }

    /// The bytes the parser read of the short last LZW strips it describes, each distinct range
    /// once.
    fn described_tail_reads(&self) -> u128 {
        let ranges: HashSet<(u64, u64)> =
            self.d["lzw_tails"].as_array().into_iter().flatten().map(|t| (u(&t["offset"]), u(&t["read"]))).collect();
        ranges.iter().map(|&(_, r)| r as u128).sum()
    }

    /// The element that holds IFD `k`'s chunks, of an image in strips or tiled (`units_element`).
    fn units_of(&self, k: usize, strips: bool) -> Option<u32> {
        units_element((self.ifds[k].tiles, self.ifds[k].strips), strips)
    }
}

impl Src<'_> {
    /// The tiles, or strips, of each plane of a level (spec/zarrify/tiff.md §4
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#4-the-image>).
    fn check_tiles(&self, lv: &Level, vals: &HashMap<usize, Vals>, f: Fmt, prefixes: &mut HashMap<usize, Vec<u8>>) -> Got<()> {
        let (_, spp, _, planar, compression, _, photometric) = f;
        let contig = spp > 1 && planar == 1;
        let samples = if spp > 1 && !contig { spp } else { 1 };
        let jpeg = compression == 7;
        let across = if lv.strips { 1 } else { lv.w.div_ceil(lv.tw) };
        let down = lv.h.div_ceil(lv.th);
        let per = across as u128 * down as u128;
        for &k in &lv.ifds {
            let m = &vals[&k];
            let (ot, ct) = if lv.strips { (273, 279) } else { self.tile_tables(k) };
            let offsets = self.reqs(k, m, ot)?;
            let counts = self.reqs(k, m, ct)?;
            let (prefix, has_tables, tables_ok) = jpeg_prefix(m, jpeg, spp, photometric);
            self.rules.check(
                "plane",
                rules::env([
                    ("offset", num(self.ifds[k].offset as f64)),
                    ("jpeg", flag(jpeg)),
                    ("has_tables", flag(has_tables)),
                    ("tables_ok", flag(tables_ok)),
                    ("tiles", num(offsets.len() as f64)),
                    ("counts", num(counts.len() as f64)),
                    ("expected", num((samples as u128 * per) as f64)),
                ]),
            )?;
            if prefixes.contains_key(&k) {
                continue;
            }
            for (j, (&o, &c)) in offsets.iter().zip(&counts).enumerate() {
                if c == 0 {
                    continue;
                }
                let within = (o as u128 + c as u128) <= self.size as u128;
                self.rules.check(
                    "tile",
                    rules::env([
                        ("k", num(j as f64)),
                        ("offset", num(self.ifds[k].offset as f64)),
                        ("within", flag(within)),
                        ("count", num(c as f64)),
                        ("jpeg", flag(jpeg)),
                    ]),
                )?;
            }
            prefixes.insert(k, if jpeg { prefix } else { Vec::new() });
        }
        Ok(())
    }

    fn req(&self, k: usize, m: &Vals, tag: u16, default: Option<u64>) -> Got<u64> {
        if let Some(v) = first_int(m, tag) {
            return Ok(v);
        }
        if let Some(d) = default {
            return Ok(d);
        }
        self.rules.check(
            "required",
            rules::env([("offset", num(self.ifds[k].offset as f64)), ("tag", int(tag as i128)), ("present", flag(false))]),
        )?;
        Ok(0)
    }

    fn reqs(&self, k: usize, m: &Vals, tag: u16) -> Got<Vec<u64>> {
        match m.get(&tag) {
            Some(Loaded::Ints(v)) => Ok(v.clone()),
            _ => {
                self.rules.check(
                    "required",
                    rules::env([("offset", num(self.ifds[k].offset as f64)), ("tag", int(tag as i128)), ("present", flag(false))]),
                )?;
                Ok(vec![])
            }
        }
    }

    fn fmt(&self, k: usize, m: &Vals) -> Got<Fmt> {
        // TIFF 6.0's default, which a bilevel image's writer may rely on
        let bits = match m.get(&258) {
            Some(Loaded::Ints(v)) => v.clone(),
            _ => vec![1],
        };
        let formats = match m.get(&339) {
            Some(Loaded::Ints(v)) => v.clone(),
            _ => vec![1],
        };
        let spp = self.req(k, m, 277, Some(1))?;
        let planar = if spp > 1 { self.req(k, m, 284, Some(1))? } else { 1 };
        let num_list = |v: &[u64]| V::List(v.iter().map(|&x| V::Num(x as f64)).collect());
        self.rules.check(
            "format",
            rules::env([
                ("bits", num_list(&bits)),
                ("formats", num_list(&formats)),
                ("spp", num(spp as f64)),
                ("planar", num(planar as f64)),
            ]),
        )?;
        let compression = self.req(k, m, 259, Some(1))?;
        let photometric = if compression == 7 { first_int(m, 262) } else { None };
        Ok((bits[0], spp, formats[0], planar, compression, self.req(k, m, 317, Some(1))?, photometric))
    }

    /// The main-chain indexes of an EER file's frames: the IFDs of IFD 0's size and format, as
    /// `pages` tests them, in chain order. Each frame's tags are read, in one batch.
    fn eer_frames(&self, i0: usize, f0: &Fmt) -> Got<Vec<usize>> {
        let loaded = self.load(&self.chain, false, false)?;
        let size = |k: usize, m: &Vals| (first_int(m, 256), first_int(m, 257), self.tiled(k));
        let at0 = self.chain.iter().position(|&k| k == i0).unwrap_or(0);
        let want = size(i0, &loaded[at0]);
        let mut frames = Vec::new();
        for (i, (&k, m)) in self.chain.iter().zip(&loaded).enumerate() {
            let format = match self.fmt(k, m) {
                Ok(f) => Some(f),
                Err(Stop::Reject(_)) => None,
                Err(e) => return Err(e),
            };
            if size(k, m) == want && format.as_ref() == Some(f0) {
                frames.push(i);
            }
        }
        Ok(frames)
    }

    /// An EER image's skip, horizontal and vertical bits (spec/zarrify/tiff.md §4.3
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#43-data-types-and-codecs>): from
    /// its compression, but for 65002's, from tags 65007 to 65009, each 7, 2 and 2 without its tag.
    /// A tag of another field type counts as absent: these are outside the table, and read only here.
    fn eer_bits(&self, k: usize, compression: u64) -> (u64, u64, u64) {
        eer_bits(compression, |t, default| {
            let value = self.ifds[k].field(t).filter(|e| unsigned(e.typ)).and_then(|e| self.ints(e, true));
            value.and_then(|v| v.first().copied()).unwrap_or(default)
        })
    }

    /// An IFD with a tile's size, TileWidth and TileLength, is tiled, as libtiff and tifffile read
    /// it and as its description takes it (spec/zarrify/tiff.md §2
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#2-the-source>,
    /// `tiff/stored.rs`).
    fn tiled(&self, k: usize) -> bool {
        self.ifds[k].has(322) && self.ifds[k].has(323)
    }

    /// The tags of the tables that list a tiled IFD's tiles: TileOffsets and TileByteCounts, its
    /// own tables, or, in an IFD with neither, StripOffsets and StripByteCounts, under which some
    /// writers list a tiled image's tiles. An IFD with one of its own tables and not the other has
    /// no tile tables (`check_size`): libtiff and tifffile would pair the one with a strips'
    /// table, and its description takes the strips' two.
    fn tile_tables(&self, k: usize) -> (u16, u16) {
        if self.ifds[k].has(324) || self.ifds[k].has(325) { (324, 325) } else { (273, 279) }
    }

    fn check_size(&self, k: usize, m: &Vals) -> Got<()> {
        let w = self.req(k, m, 256, None)?;
        let h = self.req(k, m, 257, None)?;
        let tiled = self.tiled(k);
        let (ot, ct) = self.tile_tables(k);
        let (has_offsets, has_counts) = (self.ifds[k].has(ot), self.ifds[k].has(ct));
        let (mut tw, mut th) = (1, 1);
        if tiled && has_offsets && has_counts && w >= 1 && h >= 1 {
            tw = self.req(k, m, 322, None)?;
            th = self.req(k, m, 323, None)?;
        }
        self.rules.check(
            "size",
            rules::env([
                ("offset", num(self.ifds[k].offset as f64)),
                ("width", num(w as f64)),
                ("height", num(h as f64)),
                ("tiled", flag(tiled)),
                ("has_offsets", flag(has_offsets)),
                ("has_counts", flag(has_counts)),
                ("missing", int(if has_offsets { ct } else { ot } as i128)),
                ("tile_width", num(tw as f64)),
                ("tile_height", num(th as f64)),
            ]),
        )?;
        Ok(())
    }

    fn check_samples(&self, k: usize, m: &Vals) -> Got<()> {
        let compression = self.req(k, m, 259, Some(1))?;
        let fill = self.req(k, m, 266, Some(1))?;
        let photometric = self.req(k, m, 262, Some(0))?;
        let sub = match m.get(&530) {
            Some(Loaded::Ints(v)) => v.clone(),
            _ => vec![2, 2],
        };
        self.rules.check(
            "samples",
            rules::env([
                ("offset", num(self.ifds[k].offset as f64)),
                ("compression", num(compression as f64)),
                ("fill_order", num(fill as f64)),
                ("photometric", num(photometric as f64)),
                ("subsampling", V::List(sub.iter().map(|&x| V::Num(x as f64)).collect())),
            ]),
        )?;
        Ok(())
    }

    /// A level of the IFDs `ks` (one per plane): each loaded and checked.
    fn level(&self, ks: &[usize], vals: &mut HashMap<usize, Vals>) -> Got<Level> {
        let mut uniq: Vec<usize> = ks.to_vec();
        uniq.sort_unstable();
        uniq.dedup();
        let loaded = self.load(&uniq, true, false)?;
        for (k, m) in uniq.iter().zip(loaded) {
            let mut m = m;
            if let Some(old) = vals.get(k) {
                if let Some(d) = old.get(&270) {
                    m.insert(270, d.clone());
                }
            }
            vals.insert(*k, m);
        }
        for &k in ks {
            let m = &vals[&k];
            self.check_size(k, m)?;
            self.fmt(k, m)?;
            self.check_samples(k, m)?;
        }
        // a plane in strips (spec/zarrify/tiff.md §4.5
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#45-strips>): its strips and RowsPerStrip
        for &k in ks {
            if !self.tiled(k) {
                self.check_strips(k, &vals[&k])?;
            }
        }
        let k0 = ks[0];
        let m0 = &vals[&k0];
        // the size, and the chunk's: the tile's, or the image's width and the rows per strip
        let dims = |k: usize, m: &Vals| -> Got<(u64, u64, bool, u64, u64)> {
            let (w, h) = (self.req(k, m, 256, None)?, self.req(k, m, 257, None)?);
            Ok(if self.tiled(k) {
                (w, h, true, self.req(k, m, 322, None)?, self.req(k, m, 323, None)?)
            } else {
                (w, h, false, w, self.req(k, m, 278, Some(u32::MAX as u64))?.min(h))
            })
        };
        let d0 = dims(k0, m0)?;
        let f0 = self.fmt(k0, m0)?;
        for &k in ks {
            let same = dims(k, &vals[&k])? == d0 && self.fmt(k, &vals[&k])? == f0;
            self.rules.check("level_ifd", rules::env([("offset", num(self.ifds[k].offset as f64)), ("same", flag(same))]))?;
        }
        Ok(Level { w: d0.0, h: d0.1, tw: d0.3, th: d0.4, ifds: ks.to_vec(), strips: !d0.2, join: 1, shard: 1, rectilinear: false, pad: J::Null })
    }

    /// The `strips` role of an IFD that is not tiled: StripOffsets, StripByteCounts and a
    /// RowsPerStrip of at least 1.
    fn check_strips(&self, k: usize, m: &Vals) -> Got<()> {
        let rows = self.req(k, m, 278, Some(u32::MAX as u64))?;
        self.rules.check(
            "strips",
            rules::env([
                ("offset", num(self.ifds[k].offset as f64)),
                ("has_offsets", flag(self.ifds[k].has(273))),
                ("has_counts", flag(self.ifds[k].has(279))),
                ("rows", num(rows as f64)),
            ]),
        )?;
        Ok(())
    }

    fn derive(&self) -> Got<J> {
        self.rules.check("images", rules::env([("ifds", num(self.chain.len() as f64))]))?;
        let i0 = self.chain[0];
        let mut vals: HashMap<usize, Vals> = HashMap::new();
        let m0 = self.load(&[i0], false, true)?.pop().unwrap();
        vals.insert(i0, m0.clone());
        // IFD 0's OME-XML (spec/zarrify/tiff.md §3
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#3-ome-xml>), as the parser read it
        let ome: Option<Ome> = self.ome();
        let f0 = self.fmt(i0, &m0)?;
        let (bits, spp, sample_format, planar, compression, predictor, photometric) = f0;
        self.rules.check(
            "predictor",
            rules::env([
                ("compression", num(compression as f64)),
                ("predictor", num(predictor as f64)),
                ("sample_format", num(sample_format as f64)),
                ("bits", num(bits as f64)),
            ]),
        )?;
        // the planes (spec/zarrify/tiff.md §3
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#3-ome-xml>, §4.1
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#41-planes>), in (t, c,
        // z) order, as main-chain IFD indices
        let empty = vec![];
        let px = ome.as_ref().map(|o| &o.pixels).unwrap_or(&empty);
        // a Micro-Manager stack (spec/zarrify/tiff.md §7.5
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#75-micro-manager-stacks>):
        // its index maps give the sizes and the planes
        let mm = if ome.is_none() { self.micromanager() } else { None };
        let mm_stack = match &mm {
            Some(m) => Some(self.mm_stack(m)?),
            None => None,
        };
        // an ImageJ hyperstack, without OME-XML (spec/zarrify/tiff.md §4.1
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#41-planes>): its description
        // gives the sizes, and its planes are the main-chain IFDs or, in one IFD, IFD 0's strips shifted
        let imagej = if mm_stack.is_none() { self.imagej() } else { None };
        let size_z = match (&mm_stack, &imagej) {
            (Some(s), _) => s.sizes.2,
            (None, Some(ij)) => ij.sizes.2,
            _ => int_attr(self.rules, px, "SizeZ", 1, 1)?,
        };
        // an EER file's frames, without OME-XML: the main-chain IFDs of IFD 0's size and format
        // (spec/zarrify/tiff.md §4.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#41-planes>)
        let frames = if is_eer(compression) && ome.is_none() && mm_stack.is_none() && imagej.is_none() { Some(self.eer_frames(i0, &f0)?) } else { None };
        let size_t = match (&mm_stack, &frames, &imagej) {
            (Some(s), _, _) => s.sizes.0,
            (None, Some(f), _) => f.len() as u64,
            (None, None, Some(ij)) => ij.sizes.0,
            _ => int_attr(self.rules, px, "SizeT", 1, 1)?,
        };
        let mut size_c = match (&mm_stack, &imagej) {
            (Some(s), _) => s.sizes.1.max(if spp > 1 { spp } else { 1 }),
            (None, Some(ij)) => ij.sizes.1.max(if spp > 1 { spp } else { 1 }),
            _ => int_attr(self.rules, px, "SizeC", spp, 1)?,
        };
        let order = get(px, "DimensionOrder").unwrap_or("XYZCT").to_string();
        if ome.is_some() || (spp > 1 && size_c != spp) {
            self.rules.check(
                "ome_pixels",
                rules::env([("order", text(if ome.is_some() { &order } else { "XYZCT" })), ("spp", num(spp as f64)), ("size_c", num(size_c as f64))]),
            )?;
        }
        if spp > 1 && size_c != spp {
            size_c = spp;
        }
        let plane_c = if spp > 1 { 1 } else { size_c };
        let count = (size_t as u128) * (plane_c as u128) * (size_z as u128);
        let files = ome.as_ref().map(|o| {
            let mut u: Vec<&String> = o.tiff_data.iter().filter_map(|t| t.1.as_ref()).collect();
            u.sort();
            u.dedup();
            u.len()
        });
        self.rules.check("ome_planes", rules::env([("planes", num(count as f64))]))?;
        let count = count as usize;
        let mut plane_ifd: Vec<i128> = vec![-1; count];
        let plane = |t: u64, c: u64, z: u64| ((t * plane_c + c) * size_z + z) as usize;
        // the files the OME-XML names, by each TiffData (spec/zarrify/tiff.md §7.4
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#74-several-files>)
        let tiff_data_file = match &ome {
            Some(o) if files.unwrap() > 1 => Some(self.ome_files(o)?),
            _ => None,
        };
        let mut plane_file: Vec<usize> = vec![0; if tiff_data_file.is_some() { count } else { 0 }];
        match &ome {
            None => match (&frames, &imagej) {
                (Some(f), _) => plane_ifd.iter_mut().zip(f).for_each(|(p, &i)| *p = i as i128),
                // ImageJ's planes: channels fastest, then slices, then frames; in one IFD, every
                // plane is IFD 0's strips (`emit_data` shifts those of the planes after the first)
                (None, Some(ij)) => {
                    let (_, channels, slices) = ij.sizes;
                    for t in 0..size_t {
                        for c in 0..plane_c {
                            for z in 0..size_z {
                                let p = (t * slices + z) * channels + c;
                                plane_ifd[plane(t, c, z)] = if ij.plane_bytes.is_some() { 0 } else { p as i128 };
                            }
                        }
                    }
                }
                (None, None) => plane_ifd[0] = 0,
            },
            Some(o) => {
                let sizes = |d: u8| match d {
                    b'Z' => size_z,
                    b'C' => plane_c,
                    _ => size_t,
                };
                let default = vec![(vec![], None)];
                let entries = if o.tiff_data.is_empty() { &default } else { &o.tiff_data };
                let bound = 4 * count as u128 + 1000;
                let mut total: u128 = 0;
                let ord = order.as_bytes();
                for (entry, (a, _)) in entries.iter().enumerate() {
                    let file = tiff_data_file.as_ref().map_or(0, |f| f.get(entry).copied().unwrap_or(0));
                    let mut pos = [0u64; 3]; // Z, C, T
                    pos[0] = int_attr(self.rules, a, "FirstZ", 0, 0)?;
                    pos[1] = int_attr(self.rules, a, "FirstC", 0, 0)?;
                    pos[2] = int_attr(self.rules, a, "FirstT", 0, 0)?;
                    let inside = pos[0] < size_z && pos[1] < plane_c && pos[2] < size_t;
                    self.rules.check(
                        "tiff_data",
                        rules::env([("inside", flag(inside)), ("total", num(0.0)), ("bound", num(bound as f64))]),
                    )?;
                    let mut ifd = int_attr(self.rules, a, "IFD", 0, 0)? as i128;
                    let dflt = if entries.len() == 1 && get(a, "IFD").is_none() { count as u64 } else { 1 };
                    let n = int_attr(self.rules, a, "PlaneCount", dflt, 1)? as u128;
                    let idx = |d: u8| match d {
                        b'Z' => 0,
                        b'C' => 1,
                        _ => 2,
                    };
                    let (d0, d1, d2) = (ord[2], ord[3], ord[4]);
                    let first = pos[idx(d0)] as u128
                        + sizes(d0) as u128 * (pos[idx(d1)] as u128 + sizes(d1) as u128 * pos[idx(d2)] as u128);
                    let mut n = n.min(count as u128 - first);
                    total += n;
                    self.rules.check(
                        "tiff_data",
                        rules::env([("inside", flag(true)), ("total", num(total as f64)), ("bound", num(bound as f64))]),
                    )?;
                    'fill: while n > 0 {
                        n -= 1;
                        let at = plane(pos[2], pos[1], pos[0]);
                        plane_ifd[at] = ifd;
                        if let Some(f) = plane_file.get_mut(at) {
                            *f = file;
                        }
                        ifd += 1;
                        for &d in &ord[2..] {
                            let i = idx(d);
                            pos[i] += 1;
                            if pos[i] < sizes(d) {
                                continue 'fill;
                            }
                            pos[i] = 0;
                        }
                        break;
                    }
                }
            }
        }
        // each plane's (file, main-chain index), when a plane may lie in another file or be absent
        let plan: Option<Vec<Option<(usize, usize)>>> = match (&mm_stack, &tiff_data_file) {
            (Some(s), _) => Some(s.plan(plane_c, size_z, count)),
            (None, Some(_)) => Some(plane_ifd.iter().zip(&plane_file).map(|(&i, &f)| (i >= 0).then_some((f, i as usize))).collect()),
            _ => None,
        };
        let mapped = match &plan {
            Some(p) => mm_stack.is_some() || p.iter().all(|x| x.is_some_and(|(f, i)| i < self.chain_of(f))),
            None => plane_ifd.iter().all(|&i| i >= 0 && (i as usize) < self.chain.len()),
        };
        self.rules.check("plane_map", rules::env([("mapped", flag(mapped))]))?;
        let planes: Vec<usize> = match &plan {
            // the planes this file holds: the other files check theirs (`derive_sibling`)
            Some(p) => {
                let own: Vec<usize> = p.iter().flatten().filter(|x| x.0 == 0).map(|x| self.chain[x.1]).collect();
                if own.is_empty() { vec![i0] } else { own }
            }
            None => plane_ifd.iter().map(|&i| self.chain[i as usize]).collect(),
        };
        // the levels (spec/zarrify/tiff.md §4.2
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#42-pyramid-levels>)
        let mut levels: Vec<Level> = Vec::new();
        let subs0 = self.ifds[i0].subs.clone();
        self.rules.check("set_layout", rules::env([("set", flag(plan.is_some())), ("flat", flag(subs0.is_empty())), ("whole", flag(true))]))?;
        if !subs0.is_empty() {
            let s = subs0.len();
            for &p in &planes {
                self.rules.check(
                    "subifd_levels",
                    rules::env([
                        ("offset", num(self.ifds[p].offset as f64)),
                        ("subs", num(self.ifds[p].subs.len() as f64)),
                        ("levels", num(s as f64)),
                    ]),
                )?;
            }
            levels.push(self.level(&planes, &mut vals)?);
            for k in 0..s {
                let ks: Vec<usize> = planes.iter().map(|&p| self.ifds[p].subs[k]).collect();
                levels.push(self.level(&ks, &mut vals)?);
            }
        } else {
            levels.push(self.level(&planes, &mut vals)?);
            if ome.is_none() && plan.is_none() {
                for &k in &self.chain[1..] {
                    let (pw, ph) = (levels.last().unwrap().w, levels.last().unwrap().h);
                    if !self.tiled(k) {
                        continue;
                    }
                    let m = self.load(&[k], false, false)?.pop().unwrap();
                    self.check_size(k, &m)?;
                    let (w, h) = (self.req(k, &m, 256, None)?, self.req(k, &m, 257, None)?);
                    let f = self.fmt(k, &m)?;
                    vals.entry(k).or_insert(m);
                    if f == f0 && w < pw && h < ph {
                        levels.push(self.level(&[k], &mut vals)?);
                    }
                }
                // a stack of pages in strips, as ImageJ and tifffile write them without OME-XML
                // (spec/zarrify/tiff.md §4.2
                // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#42-pyramid-levels>):
                // a later page of IFD 0's size and format is a plane the layout cannot map
                if levels[0].strips {
                    let used: HashSet<usize> = levels.iter().flat_map(|l| l.ifds.iter().copied()).collect();
                    let (w0, h0) = (levels[0].w, levels[0].h);
                    // a page's width and length are in line: only a page of IFD 0's size has its values
                    // loaded, one at a time, so that a stack is rejected at its first such page; one
                    // whose values do not load (a malformed tag) is not a page of IFD 0's format
                    let one = |k: usize, t: u16| {
                        self.ifds[k].field(t).filter(|e| unsigned(e.typ)).and_then(|e| self.ints(e, true)).and_then(|v| v.first().copied())
                    };
                    for &k in &self.chain[1..] {
                        if used.contains(&k) || (one(k, 256), one(k, 257)) != (Some(w0), Some(h0)) {
                            continue;
                        }
                        let m = match self.load(&[k], false, false) {
                            Ok(mut m) => m.pop().unwrap(),
                            Err(Stop::Reject(_)) => continue,
                            Err(e) => return Err(e),
                        };
                        let format = match self.fmt(k, &m) {
                            Ok(f) => Some(f),
                            Err(Stop::Reject(_)) => None, // not of IFD 0's format, whatever it is
                            Err(e) => return Err(e),
                        };
                        let page = format == Some(f0);
                        self.rules.check("pages", rules::env([("offset", num(self.ifds[k].offset as f64)), ("page", flag(page))]))?;
                    }
                }
            }
        }
        for lv in &levels {
            for &k in &lv.ifds {
                let same = self.fmt(k, &vals[&k])? == f0;
                self.rules.check("pyramid", rules::env([("same", flag(same))]))?;
            }
        }
        // data type and codecs (spec/zarrify/tiff.md §4.3
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#43-data-types-and-codecs>)
        let mut env = rules::env([
            ("bits", num(bits as f64)),
            ("sample_format", num(sample_format as f64)),
            ("spp", num(spp as f64)),
            ("planar", num(planar as f64)),
            ("compression", num(compression as f64)),
            ("photometric", photometric.map(|p| num(p as f64)).unwrap_or(V::Absent)),
        ]);
        self.rules.check("image", &mut env)?;
        // an EER image's codec reads the tag's bits, and decodes one shape for a level's chunks
        let eer = if is_eer(compression) { Some(self.eer_bits(i0, compression)) } else { None };
        if let Some(bits) = eer {
            for lv in &levels {
                let short = lv.strips && lv.h % lv.th != 0;
                for &k in &lv.ifds {
                    let same = self.eer_bits(k, compression) == bits;
                    self.rules.check("eer", rules::env([("offset", num(self.ifds[k].offset as f64)), ("same", flag(same)), ("short", flag(short))]))?;
                }
            }
        }
        // the tiles, or strips, of every plane of every level
        let mut prefixes = HashMap::new();
        for lv in &levels {
            self.check_tiles(lv, &vals, f0, &mut prefixes)?;
        }
        // uncompressed strips joined into larger chunks, then each level's short last chunk padded,
        // but for an image whose planes may lie in several files, of one strip a chunk
        // (spec/zarrify/tiff.md §7.4
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#74-several-files>)
        if plan.is_some() {
            let lv = &levels[0];
            let whole = !lv.strips || lv.h.is_multiple_of(lv.th);
            self.rules.check("set_layout", rules::env([("set", flag(true)), ("flat", flag(true)), ("whole", flag(whole))]))?;
        } else {
            self.join_strips(&mut levels, &vals, bits, spp, planar, compression)?;
            shard_strips(&mut levels, bits, spp, planar, compression);
        }
        // a level in strips whose last strip is short, padded to the chunk (spec/zarrify/tiff.md §4.5
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#45-strips>): one that
        // cannot be padded has a `rectilinear` grid
        if plan.is_none() {
            let padded = self.pad_strips(&mut levels, &vals, bits, spp, planar, compression)?;
            for (lv, padded) in levels.iter_mut().zip(padded) {
                lv.rectilinear = !padded;
            }
        }
        // pixel size and position (spec/zarrify/tiff.md §4.4
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#44-arrays>)
        let unit_of = |u: &str| self.rules.table("units").get(u).and_then(|x| x.as_str()).map(|x| x.to_string());
        let length = |u: &str| self.rules.table("lengths").get(u).and_then(|x| x.as_f64());
        let mut units: BTreeMap<char, String> = BTreeMap::new();
        let mut sizes: HashMap<char, f64> = HashMap::new();
        let mut centre: Option<(f64, f64)> = None;
        let mut corner: Option<(f64, f64)> = None;
        if let Some(o) = &ome {
            for (d, a) in [("Z", 'z'), ("Y", 'y'), ("X", 'x')] {
                if let Some(v) = get(&o.pixels, &format!("PhysicalSize{d}")).and_then(|v| xml::decimal_value(v, true)) {
                    sizes.insert(a, v);
                    if let Some(u) = unit_of(get(&o.pixels, &format!("PhysicalSize{d}Unit")).unwrap_or("µm")) {
                        units.insert(a, u);
                    }
                }
            }
            let stage = o.plane.clone().unwrap_or_default();
            let pos = |d: &str| get(&stage, &format!("Position{d}")).and_then(|v| xml::decimal_value(v, false));
            let pos_unit = |d: &str| unit_of(get(&stage, &format!("Position{d}Unit")).unwrap_or(""));
            let mut c = [0.0; 2];
            let mut all = true;
            for (i, (d, a)) in [("X", 'x'), ("Y", 'y')].into_iter().enumerate() {
                match (pos(d), pos_unit(d).and_then(|u| length(&u)), units.get(&a).and_then(|u| length(u))) {
                    (Some(p), Some(lp), Some(lu)) => c[i] = p * (lp / lu),
                    _ => all = false,
                }
            }
            if all {
                centre = Some((c[0], c[1]));
            }
        } else {
            let fields = self.d.get("aperio").filter(|a| a.is_object()).map(pairs_of);
            let mpp = fields.as_ref().and_then(|f| get(f, "MPP")).and_then(|v| xml::decimal_value(v, true));
            if let Some(mpp) = mpp {
                sizes.insert('x', mpp);
                sizes.insert('y', mpp);
                units.insert('x', "micrometer".into());
                units.insert('y', "micrometer".into());
                let f = fields.unwrap();
                if let (Some(l), Some(t)) = (get(&f, "Left").and_then(|v| xml::decimal_value(v, false)), get(&f, "Top").and_then(|v| xml::decimal_value(v, false))) {
                    corner = Some((l * 1000.0, t * 1000.0));
                }
            } else {
                let per_unit = self.rules.table("resolution_units").get(first_int(&vals[&i0], 296).unwrap_or(0).to_string()).and_then(|x| x.as_f64());
                for (tag, a) in [(282u16, 'x'), (283, 'y')] {
                    if let (Some(pu), Some(Loaded::Pairs(r))) = (per_unit, vals[&i0].get(&tag)) {
                        let (n, d) = r[0];
                        if n > 0 && d > 0 {
                            let pixel = pu / (n as f64 / d as f64);
                            if pixel < 25.4 {
                                sizes.insert(a, pixel);
                                units.insert(a, "micrometer".into());
                            }
                        }
                    }
                }
            }
        }
        // a Micro-Manager summary's sizes, in place of the resolution tags'
        if let Some(m) = &mm {
            let positive = |k: &str| m.summary[k].as_f64().map(f64::abs).filter(|v| *v > 0.0);
            if let Some(v) = positive("PixelSize_um") {
                for a in ['x', 'y'] {
                    sizes.insert(a, v);
                    units.insert(a, "micrometer".into());
                }
            }
            if let Some(v) = positive("z-step_um") {
                sizes.insert('z', v);
                units.insert('z', "micrometer".into());
            }
        }
        let axes: Vec<char> = [('t', size_t > 1), ('c', size_c > 1), ('z', size_z > 1), ('y', true), ('x', true)]
            .into_iter()
            .filter(|x| x.1)
            .map(|x| x.0)
            .collect();
        let size_of = |a: char| sizes.get(&a).copied();
        let (bw, bh) = (levels[0].w as f64, levels[0].h as f64);
        let scales: Vec<Vec<J>> = levels
            .iter()
            .map(|lv| {
                axes.iter()
                    .map(|&a| match a {
                        't' | 'c' => json!(1),
                        'z' => size_of('z').map(|v| json!(v)).unwrap_or(json!(1)),
                        'y' => json!(size_of('y').unwrap_or(1.0) * (bh / lv.h as f64)),
                        _ => json!(size_of('x').unwrap_or(1.0) * (bw / lv.w as f64)),
                    })
                    .collect()
            })
            .collect();
        let mut translation = J::Null;
        if units.contains_key(&'x') && units.contains_key(&'y') {
            if let Some((cx, cy)) = centre {
                let (sx, sy) = (size_of('x').unwrap_or(1.0), size_of('y').unwrap_or(1.0));
                corner = Some((cx - bw * sx / 2.0, cy - bh * sy / 2.0));
            }
            if let Some((x, y)) = corner {
                self.rules.check("translation", rules::env([("x", num(x)), ("y", num(y))]))?;
                translation = J::Array(axes.iter().map(|&a| match a {
                    'x' => json!(x),
                    'y' => json!(y),
                    _ => json!(0),
                }).collect());
            }
        }
        let name = ome.as_ref().and_then(|o| o.name.clone()).filter(|n| !n.is_empty());
        let pair = |m: &Vals, t: u16| match m.get(&t) {
            Some(Loaded::Pairs(p)) => json!([p[0].0, p[0].1]),
            _ => J::Null,
        };
        let m0 = &vals[&i0];
        let facts = json!({
            "little": self.le,
            "bigtiff": self.big,
            "ifds": self.chain.len(),
            "bits": bits,
            "spp": spp,
            "sample_format": sample_format,
            "planar": planar,
            "compression": compression,
            "predictor": predictor,
            "photometric": photometric,
            "eer": eer.map(|(skip, horz, vert)| json!({"skipbits": skip, "horzbits": horz, "vertbits": vert})),
            "sizes": {"t": size_t, "c": size_c, "z": size_z},
            "plane_c": plane_c,
            "ome": ome.as_ref().map(|o| json!({
                "name": o.name,
                "pixels": o.pixels.iter().map(|(k, v)| (k.clone(), J::String(v.clone()))).collect::<serde_json::Map<_, _>>(),
                "plane": o.plane.as_ref().map(|p| p.iter().map(|(k, v)| (k.clone(), J::String(v.clone()))).collect::<serde_json::Map<_, _>>()),
                "channels": o.channels.iter().map(|c| json!({"name": get(c, "Name"), "color": get(c, "Color")})).collect::<Vec<_>>(),
            })).or(mm.as_ref().map(|m| json!({
                "channels": m.summary["ChNames"].as_array().into_iter().flatten().take(MAX_OMERO as usize).map(|n| json!({"name": n})).collect::<Vec<_>>(),
            }))),
            "micromanager": mm.as_ref().map(|m| &m.summary),
            "imagej": imagej.as_ref().map(|ij| json!({"images": ij.images, "frames": ij.sizes.0, "channels": ij.sizes.1, "slices": ij.sizes.2, "one_ifd": ij.plane_bytes.is_some()})),
            "interpretation": first_int(m0, 262),
            "axes": axes.iter().map(|a| a.to_string()).collect::<Vec<_>>(),
            "units": units.iter().map(|(a, u)| (a.to_string(), J::String(u.clone()))).collect::<serde_json::Map<_, _>>(),
            "scales": scales,
            "translation": translation,
            "name": name,
            "resolution_unit": first_int(m0, 296).unwrap_or(0),
            "x_resolution": pair(m0, 282),
            "y_resolution": pair(m0, 283),
        });
        // the other files' planes, each of the image's size and format, their tiles or strips in
        // them (spec/zarrify/tiff.md §7.4
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#74-several-files>)
        if let Some(p) = &plan {
            let dims = (levels[0].w, levels[0].h, !levels[0].strips, levels[0].tw, levels[0].th);
            for (f, (_, sib)) in self.siblings.iter().enumerate() {
                let ks: Vec<usize> = p.iter().flatten().filter(|x| x.0 == f + 1).map(|x| sib.chain[x.1]).collect();
                if ks.is_empty() {
                    continue;
                }
                let mut v = HashMap::new();
                let lv = sib.level(&ks, &mut v)?;
                let fm = sib.fmt(ks[0], &v[&ks[0]])?;
                let same = (lv.w, lv.h, !lv.strips, lv.tw, lv.th) == dims && fm == f0;
                sib.rules.check("set_format", rules::env([("same", flag(same))]))?;
                sib.check_tiles(&lv, &v, fm, &mut HashMap::new())?;
            }
        }
        // each level's planes: the tiles or strips of each plane's IFD (in the file that holds it),
        // and the IFD (or `imagej/planes/<p>`) that declares its image, the key of its stored
        // array's description
        let imagej_planes = self.imagej().filter(|ij| ij.plane_bytes.is_some()).map(|ij| {
            let i0 = self.chain[0];
            let mut structs: Vec<(Option<u32>, Option<u32>)> = vec![(self.ifds[i0].strips, Some(self.ifds[i0].el))];
            for p in self.d["imagej"]["planes"].as_array().into_iter().flatten() {
                structs.push((p["strips"].as_u64().map(|x| x as u32), p["element"].as_u64().map(|x| x as u32)));
            }
            let (frames, channels, slices) = ij.sizes;
            let mut planes = Vec::new();
            for t in 0..frames {
                for c in 0..channels {
                    for z in 0..slices {
                        planes.push(structs.get(((t * slices + z) * channels + c) as usize).copied().unwrap_or((None, None)));
                    }
                }
            }
            planes.into_iter().unzip::<_, _, Vec<_>, Vec<_>>()
        });
        let file = |f: usize| if f == 0 { self } else { &self.siblings[f - 1].1 };
        let levels_json: Vec<J> = levels
            .iter()
            .map(|lv| {
                let (planes, declared_by): (Vec<Option<u32>>, Vec<Option<u32>>) = match (&plan, &imagej_planes) {
                    (Some(p), _) => p
                        .iter()
                        .map(|x| match x {
                            Some((f, i)) => {
                                let s = file(*f);
                                let k = s.chain[*i];
                                (s.units_of(k, lv.strips), Some(s.ifds[k].el))
                            }
                            None => (None, None),
                        })
                        .unzip(),
                    (None, Some((planes, keys))) => (planes.clone(), keys.clone()),
                    (None, None) => lv.ifds.iter().map(|&k| (self.units_of(k, lv.strips), Some(self.ifds[k].el))).unzip(),
                };
                json!({"w": lv.w, "h": lv.h, "tw": lv.tw, "th": lv.th, "strips": lv.strips, "join": lv.join, "shard": lv.shard,
                       "rectilinear": lv.rectilinear, "pad": lv.pad, "planes": planes, "declared_by": declared_by})
            })
            .collect();
        let mut facts = facts;
        facts["levels"] = J::Array(levels_json);
        if let Some(named) = &self.named {
            facts["named_key"] = json!(named);
            facts["files"] = json!(self.siblings.iter().map(|s| &s.0).collect::<Vec<_>>());
        }
        let _ = (&planes, prefixes);
        Ok(facts)
    }

    /// A Micro-Manager stack's planes across its files (spec/zarrify/tiff.md §7.5
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#75-micro-manager-stacks>):
    /// each index map's entries, of this file and of each other file that has one. The parser
    /// found the files.
    fn mm_stack(&self, m: &Mm) -> Got<MmStack> {
        let mut files: Vec<(usize, Mm, Vec<u64>)> = vec![(0, m.clone(), self.chain.iter().map(|&k| self.ifds[k].offset).collect())];
        for (k, (_, s)) in self.siblings.iter().enumerate() {
            if let Some(o) = s.micromanager() {
                files.push((k + 1, o, s.chain.iter().map(|&i| s.ifds[i].offset).collect()));
            }
        }
        let position = m.entries[0].position;
        let mut sizes = (1, 1, 1);
        let mut entries = Vec::new();
        for (f, file, offsets) in files {
            let at: HashMap<u64, usize> = offsets.iter().enumerate().map(|(i, &o)| (o, i)).collect();
            for e in &file.entries {
                self.rules.check("micromanager", rules::env([("one_position", flag(e.position == position)), ("found", flag(at.contains_key(&(e.ifd as u64))))]))?;
                let (t, c, z) = (e.frame as u64, e.channel as u64, e.slice as u64);
                sizes = (sizes.0.max(t + 1), sizes.1.max(c + 1), sizes.2.max(z + 1));
                entries.push((t, c, z, f, at[&(e.ifd as u64)]));
            }
        }
        Ok(MmStack { sizes, entries })
    }

    /// The NDPI layout: levels, chunk grids, scales and translation, as facts for
    /// `project/ndpi.rs`. Asks for the values and strip headers not read yet.
    fn derive_ndpi(&self) -> Got<J> {
        let mut need = Vec::new();
        let mut mags = Vec::new();
        for &k in &self.chain {
            mags.push(self.nload(k, Some(&[65421]), &mut need, false)?);
        }
        let positive = |j: usize| mags[j].get(&65421).and_then(|v| v.first()).is_some_and(|m| m > 0.0);
        let starts_of = |k: usize, _: &NVals| self.mcu_starts(k);
        let tiled_of = |k: usize, m: &NVals| {
            let f = &self.ifds[k];
            [322u16, 323].iter().all(|&t| Self::nint(m, t).is_some_and(|v| v >= 1)) && f.has(324) && f.has(325)
        };
        // focal offsets are read only when a Magnification has more than one image
        // (spec/zarrify/ndpi.md §3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#3-levels>)
        let mut seen = HashSet::new();
        let repeated = (0..self.chain.len())
            .filter(|&j| positive(j))
            .filter_map(|j| mags[j].get(&65421).and_then(|v| v.first()))
            .any(|m| !seen.insert(m.to_bits()));
        // the levels (spec/zarrify/ndpi.md §3
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#3-levels>)
        let mut tags: Vec<(usize, NVals)> = Vec::new();
        let mut levels: Vec<(usize, f64, u64, u64, u64, bool)> = Vec::new(); // the images: (IFD, magnification, w, h, compression, tiled)
        let mut last_size: Option<(u64, u64)> = None; // of the last new Magnification's first image
        let mut magnifications = HashSet::new();
        for (j, &k) in self.chain.iter().enumerate() {
            self.nload(k, Some(&[65421]), &mut need, true)?; // the profile reads Magnification first
            let mag = self.nreq(&mags[j], 65421, "Magnification")?;
            if !(mag > 0.0) {
                continue;
            }
            let m = self.nload(k, None, &mut need, true)?;
            let w = self.nreq(&m, 256, "ImageWidth")? as u64;
            let h = self.nreq(&m, 257, "ImageLength")? as u64;
            let w = Self::nint(&m, 256).unwrap_or(w);
            let h = Self::nint(&m, 257).unwrap_or(h);
            let compression = Self::nint(&m, 259).unwrap_or(1);
            let photometric = if compression == 7 { Some(self.nreq(&m, 262, "PhotometricInterpretation")? as u64) } else { Self::nint(&m, 262) };
            let bits = m.get(&258).and_then(|v| v.ints()).map(|v| v.to_vec()).unwrap_or_default();
            let f = &self.ifds[k];
            let one_strip = [273u16, 279].iter().all(|&t| f.field(t).is_some_and(|e| e.count == 1));
            let tiled = tiled_of(k, &m);
            // an image of an earlier Magnification is a plane of that level, not a new level
            let new_magnification = magnifications.insert(mag.to_bits());
            let smaller = !new_magnification || last_size.is_none_or(|l| w < l.0 && h < l.1);
            self.rules.check(
                "level",
                rules::env([
                    ("compression", num(compression as f64)),
                    ("first_compression", num(levels.first().map_or(compression, |l| l.4) as f64)),
                    ("photometric", photometric.map(|p| num(p as f64)).unwrap_or(V::Absent)),
                    ("spp", num(Self::nint(&m, 277).unwrap_or(1) as f64)),
                    ("bits", V::List(bits.iter().map(|&b| V::Num(b as f64)).collect())),
                    ("one_strip", flag(one_strip)),
                    ("tiled", flag(tiled)),
                    ("smaller", flag(smaller)),
                    ("width", num(w as f64)),
                    ("height", num(h as f64)),
                ]),
            )?;
            if new_magnification {
                last_size = Some((w, h));
            }
            levels.push((k, mag, w, h, compression, tiled));
            tags.push((k, m));
        }
        self.rules.check("images", rules::env([("levels", num(levels.len() as f64))]))?;
        let vals = |k: usize| &tags.iter().find(|t| t.0 == k).unwrap().1;
        // scale and position (spec/zarrify/ndpi.md §4
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#4-the-image>)
        let (k0, _, bw, bh, _, _) = levels[0];
        let m0 = vals(k0);
        let per_unit = self
            .rules
            .table("resolution_units")
            .get(Self::nint(m0, 296).unwrap_or(0).to_string())
            .and_then(|x| x.as_f64());
        let physical = |tag: u16| -> Option<f64> {
            let pu = per_unit?;
            let Some(Nv::Rats(r)) = m0.get(&tag) else { return None };
            let (n, d) = *r.first()?;
            if n == 0 || d == 0 {
                return None;
            }
            let pixel = pu / (n as f64 / d as f64);
            (pixel < 25.4).then_some(pixel)
        };
        let (px, py) = (physical(282), physical(283));
        let mut units = serde_json::Map::new();
        for (a, p) in [("x", px), ("y", py)] {
            if p.is_some() {
                units.insert(a.into(), json!("micrometer"));
            }
        }
        // the chunks of each level; the header's pieces are data sources, numbered by first use
        let mut pieces: Vec<Vec<u8>> = Vec::new();
        // by image: its facts, its grid, and what the focal plane rule reads of it
        let mut out: Vec<J> = Vec::new();
        let mut grids: Vec<Option<ndpi::Grid>> = Vec::new();
        let mut images: Vec<ndpi::Image> = Vec::new();
        // the pixel format of level 0's first JPEG XR stream (spec/zarrify/ndpi.md §3
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#3-levels>)
        let mut format0: Option<[u8; 16]> = None;
        for (li, &(k, mag, w, h, compression, tiled)) in levels.iter().enumerate() {
            let m = vals(k);
            let offset = if repeated { self.ndpi_focal(k) } else { None };
            let image = |cut: ndpi::Cut, framed: bool| ndpi::Image { magnification: mag, width: w, height: h, cut, framed, offset };
            let (s0, n) = (Self::nint(m, 273).unwrap_or(0), Self::nint(m, 279).unwrap_or(0));
            if compression == ndpi::JPEGXR {
                // a JPEG XR level: its tiles, or its strip, each a chunk
                let (cw, ch) = if tiled {
                    let (tw, th) = (Self::nint(m, 322).unwrap_or(1), Self::nint(m, 323).unwrap_or(1));
                    let t = self.ndpi_tiles(k).ok_or_else(|| Stop::Reject("internal: an NDPI level's tile tables were not read".into()))?;
                    let within = t.offsets.iter().zip(&t.counts).all(|(&o, &c)| c == 0 || self.within(o, c));
                    self.rules.check(
                        "tiles",
                        rules::env([
                            ("tiles", num(h.div_ceil(th).saturating_mul(w.div_ceil(tw)) as f64)),
                            ("offsets", num(t.offsets.len() as f64)),
                            ("counts", num(t.counts.len() as f64)),
                            ("high", flag(t.matched)),
                            ("within", flag(within)),
                        ]),
                    )?;
                    (tw, th)
                } else {
                    let within = (s0 as u128 + n as u128) <= self.size as u128;
                    self.rules.check("strip", rules::env([("within", flag(within)), ("length", num(n as f64))]))?;
                    (w, h)
                };
                let first = self.ndpi_first(k).filter(|x| x.1 > 0);
                let header = if first.is_some() { self.ndpi_stream(k) } else { None };
                let format = header.map(|x| x.0);
                if li == 0 {
                    format0 = format;
                }
                let (fw, fh) = header.map_or((0, 0), |x| (x.1, x.2));
                self.rules.check(
                    "jpegxr",
                    rules::env([
                        ("stream", flag(first.is_some())),
                        ("header", flag(header.is_some())),
                        ("format", text(&format.as_ref().map(ndpi::hex).unwrap_or_default())),
                        ("supported", flag(format.as_ref().and_then(ndpi::pixel_format).is_some())),
                        ("same", flag(format.is_some() && format == format0)),
                        ("width", num(fw as f64)),
                        ("height", num(fh as f64)),
                        ("chunk_width", num(cw as f64)),
                        ("chunk_height", num(ch as f64)),
                    ]),
                )?;
                out.push(json!({
                    "tw": cw, "th": ch, "strip": s0, "length": n,
                    "tiles_ifd": if tiled { json!(k) } else { J::Null },
                }));
                grids.push(None);
                images.push(image(ndpi::Cut::Fixed(ch, cw), true));
                continue;
            }
            let within = (s0 as u128 + n as u128) <= self.size as u128;
            self.rules.check("strip", rules::env([("within", flag(within)), ("length", num(n as f64))]))?;
            let head = starts_of(k, m);
            // the projection adds McuStartsHighBytes as the layout does
            let added = head.as_ref().is_some_and(|x| x.added);
            if let Some(x) = &head {
                self.rules.check(
                    "mcu_starts",
                    rules::env([
                        ("same_length", flag(x.same_length)),
                        ("high_word", flag(x.high_word)),
                        ("first", x.starts.first().map(|&f| num(f as f64)).unwrap_or(V::Absent)),
                        ("length", num(n as f64)),
                    ]),
                )?;
            }
            // a strip without McuStarts whose frame is not the level's size, but has its MCU rows
            // and columns, is spliced the level's (spec/zarrify/ndpi.md §7
            // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#7-ndpi-profile>); with
            // others, the level is not a level
            let mut frame = J::Null;
            if head.is_none() {
                let f = self.ndpi_frame(k);
                if let Some(f) = f.filter(|f| (f.height, f.width) != (h, w)) {
                    let covers = f.covers(h, w);
                    self.rules.check(
                        "frame",
                        rules::env([
                            ("level", num(li as f64)),
                            ("covers", flag(covers)),
                            ("frame_height", num(f.height as f64)),
                            ("frame_width", num(f.width as f64)),
                            ("height", num(h as f64)),
                            ("width", num(w as f64)),
                        ]),
                    )?;
                    if !covers {
                        out.push(J::Null);
                        grids.push(None);
                        images.push(image(ndpi::Cut::Fixed(h, w), false));
                        continue;
                    }
                    frame = json!(f.at);
                }
            }
            let grid = match head {
                None => None,
                Some(McuStarts { starts, .. }) => {
                    let Some(hb) = self.jpeg_header(k) else {
                        return Err(Stop::Reject("internal: a strip header the description does not hold passed the checks".into()));
                    };
                    let hd = ndpi::header(&hb, self.rules)?;
                    let (before, after) = (ndpi::piece(&mut pieces, &hb[..hd.sof.0]), ndpi::piece(&mut pieces, &hb[hd.sof.1..]));
                    Some(ndpi::Grid::new(s0, n, w, h, hb, hd, starts, &before, &after, self.rules)?)
                }
            };
            let high = if added { self.ent_facts(k, 65432) } else { J::Null };
            let cut = grid.as_ref().map_or(ndpi::Cut::Fixed(h, w), |g| ndpi::Cut::Intervals { restart: g.h.restart, mw: g.h.mw, mh: g.h.mh });
            out.push(json!({"strip": s0, "length": n, "starts": self.ent_facts(k, 65426), "high": high, "grid": J::Null, "frame": frame}));
            grids.push(grid);
            images.push(image(cut, true));
        }
        // the levels and their planes (spec/zarrify/ndpi.md §3
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#3-levels>); with images to
        // choose from, the data sources are numbered again by the first use of those kept, and a
        // level's planes get one chunk height (§7
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#7-ndpi-profile>)
        let chosen = ndpi::levels(&images);
        if repeated {
            let mut pieces: Vec<Vec<u8>> = Vec::new();
            for level in &chosen.levels {
                let (mut at, mut planes) = (Vec::new(), Vec::new());
                for &i in level {
                    if let Some(g) = grids[i].take() {
                        let (a, b) = g.pieces();
                        let (before, after) = (ndpi::piece(&mut pieces, a), ndpi::piece(&mut pieces, b));
                        at.push(i);
                        planes.push((g, before, after));
                    }
                }
                ndpi::Grid::one_height(&mut planes, self.rules)?;
                for (i, (g, _, _)) in at.into_iter().zip(planes) {
                    grids[i] = Some(g);
                }
            }
        }
        let offsets = chosen.offsets.as_deref().unwrap_or(&[]);
        let stacked = offsets.len() > 1;
        // `z`'s scale and position, when its planes are evenly spaced (§4
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#4-the-image>)
        let step = if stacked { ndpi::even_step(offsets) } else { None };
        if step.is_some() {
            units.insert("z".into(), json!("micrometer"));
        }
        let (scale_z, start_z) = step.map_or((1.0, 0.0), |s| (s as f64 / 1000.0, offsets[0] as f64 / 1000.0));
        let mut facts_of_levels = Vec::with_capacity(chosen.levels.len());
        let mut scales = Vec::with_capacity(chosen.levels.len());
        for level in &chosen.levels {
            let (w, h) = (images[level[0]].width, images[level[0]].height);
            let planes: Vec<J> = level
                .iter()
                .map(|&i| {
                    let mut p = out[i].take();
                    if let Some(g) = &grids[i] {
                        p["grid"] = g.facts();
                    }
                    // a tiled JPEG XR level's chunks are its IFD's tiles
                    if let Some(k) = p["tiles_ifd"].as_u64() {
                        p["tiles"] = json!(self.ifds[k as usize].tiles);
                    }
                    p
                })
                .collect();
            // `declared_by`: each plane's IFD, the key of its stored array's description
            let declared_by: Vec<u32> = level.iter().map(|&i| self.ifds[levels[i].0].el).collect();
            facts_of_levels.push(json!({"w": w, "h": h, "planes": planes, "declared_by": declared_by}));
            let (sy, sx) = (py.unwrap_or(1.0) * (bh as f64 / h as f64), px.unwrap_or(1.0) * (bw as f64 / w as f64));
            scales.push(if stacked { json!([1, scale_z, sy, sx]) } else { json!([1, sy, sx]) });
        }
        let mut position = None;
        if let (Some(sx), Some(sy), Some(ox), Some(oy)) =
            (px, py, m0.get(&65422).and_then(|v| v.first()), m0.get(&65423).and_then(|v| v.first()))
        {
            let (x, y) = (ox / 1000.0 - bw as f64 * sx / 2.0, oy / 1000.0 - bh as f64 * sy / 2.0);
            self.rules.check("translation", rules::env([("x", num(x)), ("y", num(y))]))?;
            position = Some((y, x));
        }
        let translation = match (stacked, position) {
            (false, Some((y, x))) => json!([0, y, x]),
            (true, Some((y, x))) => json!([0, start_z, y, x]),
            (true, None) if step.is_some() => json!([0, start_z, 0, 0]),
            _ => J::Null,
        };
        // the pixels (spec/zarrify/ndpi.md §3
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#3-levels>), and level 0's
        // acquisition tags (§5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#5-source-metadata>)
        let (data_type, samples, codec) = match format0.as_ref().and_then(ndpi::pixel_format) {
            Some((t, s)) => (t, s, "imagecodecs_jpegxr"),
            None => ("uint8", 3, "imagecodecs_jpeg"),
        };
        let (mut own, label) = self.ndpi_acquisition(k0);
        if !need.is_empty() {
            return Err(Stop::Need(need));
        }
        // what the source metadata keeps of the focal planes (§5
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#5-source-metadata>)
        if let Some(o) = &chosen.offsets {
            own.insert("FocalOffsets".into(), json!(o));
        }
        if !chosen.left_out.is_empty() {
            let entries: Vec<J> = chosen
                .left_out
                .iter()
                .map(|&(i, reason)| {
                    let im = &images[i];
                    let mut e = json!({
                        "ifd": self.chain.iter().position(|&k| k == levels[i].0),
                        "magnification": crate::project::float_json(im.magnification.to_bits(), 8, im.magnification),
                        "width": im.width,
                        "height": im.height,
                        "reason": reason,
                    });
                    if let Some(o) = im.offset {
                        e["offset"] = json!(o);
                    }
                    e
                })
                .collect();
            own.insert("LeftOut".into(), J::Array(entries));
        }
        let axes: &[&str] = if stacked { &["c", "z", "y", "x"] } else { &["c", "y", "x"] };
        let facts = json!({
            "profile": "ndpi",
            "ifds": self.chain.len(),
            "axes": axes,
            "data_type": data_type,
            "samples": samples,
            "codec": codec,
            "label": label,
            "own": own,
            "units": units,
            "scales": scales,
            "translation": translation,
            "levels": facts_of_levels,
        });
        Ok(facts)
    }

    /// A QPTIFF level of the IFDs `ks` (one per channel), each checked (the `size`, `samples`,
    /// `strips` and `level` roles): all the same size, tiling and format, IFD 0's format, and
    /// smaller than the level before. A level in strips has the chunk width `w` and height
    /// `min(RowsPerStrip, h)`.
    fn qlevel(&self, index: usize, ks: &[usize], vals: &HashMap<usize, Vals>, f0: &Fmt, prev: Option<&Level>) -> Got<Level> {
        let mut first: Option<(u64, u64, bool, u64, u64)> = None;
        for &k in ks {
            let m = &vals[&k];
            self.check_size(k, m)?;
            let f = self.fmt(k, m)?;
            self.check_samples(k, m)?;
            let w = self.req(k, m, 256, None)?;
            let h = self.req(k, m, 257, None)?;
            let tiled = self.tiled(k);
            let (tw, th) = if tiled {
                (self.req(k, m, 322, None)?, self.req(k, m, 323, None)?)
            } else {
                self.check_strips(k, m)?;
                (w, self.req(k, m, 278, Some(u32::MAX as u64))?.min(h))
            };
            let this = (w, h, tiled, tw, th);
            let same = first.is_none_or(|x| x == this);
            let smaller = prev.is_none_or(|p| w < p.w && h < p.h);
            self.rules.check(
                "level",
                rules::env([
                    ("offset", num(self.ifds[k].offset as f64)),
                    ("level", num(index as f64)),
                    ("previous", num(index.saturating_sub(1) as f64)),
                    ("same", flag(same)),
                    ("format", flag(f == *f0)),
                    ("smaller", flag(smaller)),
                ]),
            )?;
            first.get_or_insert(this);
        }
        let (w, h, tiled, tw, th) = first.ok_or_else(|| Stop::Reject("internal: an empty QPTIFF level".into()))?;
        Ok(Level { w, h, tw, th, ifds: ks.to_vec(), strips: !tiled, join: 1, shard: 1, rectilinear: false, pad: J::Null })
    }

    /// Joins the strips of each uncompressed level into chunks of at most 2^20 bytes and 2^10
    /// strips, as even as whole strips allow (spec/zarrify/tiff.md §4.5
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#45-strips>). A level stays one strip a chunk
    /// when two strips do not fit, or when a strip does not hold exactly its rows' bytes, which
    /// a chunk could not then find by position.
    fn join_strips(&self, levels: &mut [Level], vals: &HashMap<usize, Vals>, bits: u64, spp: u64, planar: u64, compression: u64) -> Got<()> {
        if compression != 1 {
            return Ok(());
        }
        let contig = spp > 1 && planar == 1;
        for lv in levels.iter_mut().filter(|lv| lv.strips) {
            let row = row_size(lv.w, bits, if contig { spp } else { 1 });
            let strips = lv.h.div_ceil(lv.th);
            let most = (MAX_JOINED_BYTES / (lv.th as u128 * row)).min(MAX_JOINED_STRIPS) as u64;
            if strips < 2 || most < 2 {
                continue;
            }
            let mut whole = true;
            for &k in &lv.ifds {
                whole &= strips_hold_their_rows(&self.reqs(k, &vals[&k], 279)?, lv.h, lv.th, row);
            }
            if whole {
                lv.join = strips.div_ceil(strips.div_ceil(most));
            }
        }
        Ok(())
    }

    /// Pads each level in strips whose last strip is short to the chunk (spec/zarrify/tiff.md §4.5
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#45-strips>,
    /// spec/zarrify/qptiff.md §4.2
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/qptiff.md#42-tiles-and-strips>): with
    /// zeros when uncompressed, or with a literal tail spliced into the strip's LZW stream, which is
    /// read for that (asked for, when it was not yet); a level of another compression cannot be
    /// padded, which the profile's `padding` role decides. Returns whether each level is kept: padded,
    /// or without a short last strip. Each strip's pad is keyed by its IFD's index in the level and
    /// the strip's.
    fn pad_strips(&self, levels: &mut [Level], vals: &HashMap<usize, Vals>, bits: u64, spp: u64, planar: u64, compression: u64) -> Got<Vec<bool>> {
        let contig = spp > 1 && planar == 1;
        let samples = if spp > 1 && !contig { spp } else { 1 };
        // the sizes in bytes, from the file's integers, in u128 (a product that does not fit in u64
        // saturates, and is then more padding than the `padding` role allows)
        let row_bytes = |lv: &Level| row_size(lv.w, bits, if contig { spp } else { 1 });
        let bytes = |x: u128| u64::try_from(x).unwrap_or(u64::MAX);
        // the bytes of an LZW strip of `n` bytes the scan reads to find where its output reaches
        // `target` bytes (spec/zarrify/tiff.md §7.1
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#71-reading>): at most
        // 3 × target + 64, as a stream that puts a Clear code (12 bits at most) before each
        // literal (9 bits) spends 21 bits on a byte it decodes
        let read = |n: u64, target: u64| n.min(bytes(3 * target as u128 + 64));
        let last_strips = |lv: &Level, k: usize, m: &Vals| -> Got<Vec<(usize, u64, u64)>> {
            let down = lv.h.div_ceil(lv.th);
            let (offsets, counts) = (self.reqs(k, m, 273)?, self.reqs(k, m, 279)?);
            Ok((0..samples)
                .map(|s| {
                    let j = (s * down + down - 1) as usize;
                    (j, offsets[j], counts[j])
                })
                .collect())
        };
        let short_of = |lv: &Level| -> Option<u64> {
            let rows = lv.chunk_rows();
            let down = lv.h.div_ceil(rows);
            let short = lv.h - rows * (down - 1);
            (lv.strips && down > 1 && short != rows).then_some(short)
        };
        // the distinct ranges of the strips read for their tails, whose bytes together are at most
        // the file's (as strips that do not overlap hold)
        let mut tails: HashSet<(u64, u64)> = HashSet::new();
        if compression == 5 {
            for (li, lv) in levels.iter().enumerate() {
                let Some(short) = short_of(lv) else { continue };
                // the zeros an LZW tail would hold, and the bytes of a strip read for it, within the
                // limits before its strips are read
                let missing = bytes((lv.chunk_rows() - short) as u128 * row_bytes(lv));
                let target = bytes(short as u128 * row_bytes(lv));
                let mut most_read = 0;
                for &k in &lv.ifds {
                    for (_, _, c) in last_strips(lv, k, &vals[&k])? {
                        most_read = most_read.max(read(c, target));
                    }
                }
                self.rules.check(
                    "padding",
                    rules::env([
                        ("level", num(li as f64)),
                        ("compression", num(compression as f64)),
                        ("kept", flag(true)),
                        ("zeros", num(missing as f64)),
                        ("read", num(most_read as f64)),
                    ]),
                )?;
                for &k in &lv.ifds {
                    for (_, o, c) in last_strips(lv, k, &vals[&k])? {
                        let r = read(c, target);
                        if c > 0 {
                            tails.insert((o, r));
                        }
                    }
                }
            }
        }
        let total: u128 = tails.iter().map(|&(_, r)| r as u128).sum();
        self.rules.check("tails", rules::env([("read", num(total as f64)), ("size", num(self.size as f64))]))?;
        let mut keep = vec![true; levels.len()];
        for (li, lv) in levels.iter_mut().enumerate() {
            let Some(short) = short_of(lv) else { continue };
            let rows = row_bytes(lv);
            let missing = bytes((lv.chunk_rows() - short) as u128 * rows);
            // the most zero bytes a strip of the level is padded with
            let mut most = missing;
            match compression {
                1 => {
                    // each strip with the zeros its byte count lacks of the chunk's size (none
                    // when it holds the chunk, as a strip written whole does)
                    let mut pads = Vec::new();
                    most = 0;
                    for (c, &k) in lv.ifds.iter().enumerate() {
                        for (j, _, n) in last_strips(lv, k, &vals[&k])? {
                            let z = if lv.join > 1 { missing } else { bytes(lv.th as u128 * rows).saturating_sub(n) };
                            if n > 0 && z > 0 {
                                most = most.max(z);
                                pads.push(json!({"c": c, "k": j, "zeros": z}));
                            }
                        }
                    }
                    lv.pad = json!({"zeros": pads});
                }
                5 => {
                    let mut pads = Vec::new();
                    for (c, &k) in lv.ifds.iter().enumerate() {
                        for (j, _, n) in last_strips(lv, k, &vals[&k])? {
                            if n == 0 {
                                continue; // an absent strip: an absent chunk
                            }
                            let Some(tail) = self.lzw_tail(k, j) else {
                                // a strip the parser left out of its reads past the file's size
                                // (`lzw_tails`): what its reads would have been, with this one
                                let read = self.described_tail_reads() + read(n, bytes(short as u128 * rows)) as u128;
                                self.rules.check("tails", rules::env([("read", num(read as f64)), ("size", num(self.size as f64))]))?;
                                return Err(Stop::Reject("internal: a short last LZW strip the description does not hold".into()));
                            };
                            let (ok, problem, pad) = match tail {
                                Ok((prefix, tail)) => (true, String::new(), json!({"c": c, "k": j, "prefix": prefix, "tail": tail})),
                                Err(e) => (false, e, J::Null),
                            };
                            self.rules.check(
                                "strip_tail",
                                rules::env([
                                    ("offset", num(self.ifds[k].offset as f64)),
                                    ("strip", num(j as f64)),
                                    ("decodes", flag(ok)),
                                    ("problem", text(&problem)),
                                ]),
                            )?;
                            pads.push(pad);
                        }
                    }
                    lv.pad = json!({"lzw": pads});
                }
                _ => {
                    keep[li] = false; // not padded: no zeros
                    most = 0;
                }
            }
            self.rules.check(
                "padding",
                rules::env([
                    ("level", num(li as f64)),
                    ("compression", num(compression as f64)),
                    ("kept", flag(keep[li])),
                    ("zeros", num(most as f64)),
                ]),
            )?;
        }
        Ok(keep)
    }

    /// The QPTIFF layout: the channels (the FullResolution images at the start of the main
    /// chain), the levels (the runs of ReducedResolution images), the data type and codecs,
    /// each IFD's tiles or strips, the pixel size, the position and the channels' names and
    /// colors, as facts for `project/qptiff.rs`. Asks for the values not read yet.
    fn derive_qptiff(&self) -> Got<J> {
        self.rules.check("images", rules::env([("ifds", num(self.chain.len() as f64))]))?;
        // every main-chain IFD's values but its tile and strip tables, and its description, in one batch
        let loaded = self.load(&self.chain, false, true)?;
        let mut vals: HashMap<usize, Vals> = self.chain.iter().copied().zip(loaded).collect();
        // each IFD's QPI description, as the parser read it
        let desc = |k: usize| -> Option<Qpi> {
            let q = self.ifds[k].d.get("qpi").filter(|q| q.is_object())?;
            let text = |n: &str| q[n].as_str().map(str::to_string);
            Some(Qpi { image_type: text("image_type"), name: text("name"), color: text("color"), slide_id: text("slide_id") })
        };
        let kinds: Vec<Option<Qpi>> = self.chain.iter().map(|&k| desc(k)).collect();
        let is = |q: &Option<Qpi>, t: &str| q.as_ref().is_some_and(|q| q.is(t));
        let n = kinds.iter().take_while(|q| is(q, "FullResolution")).count();
        let later = kinds[n..].iter().any(|q| is(q, "FullResolution"));
        let i0 = self.chain[0];
        let m0 = vals[&i0].clone(); // kept while the tables are loaded below
        let m0 = &m0;
        let f0 = self.fmt(i0, m0)?;
        let (bits, spp, sample_format, planar, compression, predictor, photometric) = f0;
        self.rules.check(
            "baseline",
            rules::env([
                ("described", flag(kinds[0].is_some())),
                ("baseline", flag(n >= 1)),
                ("channels", num(n as f64)),
                ("later", flag(later)),
                ("spp", num(spp as f64)),
            ]),
        )?;
        self.rules.check(
            "predictor",
            rules::env([
                ("compression", num(compression as f64)),
                ("predictor", num(predictor as f64)),
                ("sample_format", num(sample_format as f64)),
            ]),
        )?;
        let reduced: Vec<usize> =
            self.chain.iter().copied().zip(&kinds).filter(|(_, q)| is(q, "ReducedResolution")).map(|(k, _)| k).collect();
        self.rules.check(
            "reduced",
            rules::env([
                ("channels", num(n as f64)),
                ("reduced", num(reduced.len() as f64)),
                ("remainder", num((reduced.len() % n) as f64)),
            ]),
        )?;
        let mut levels = vec![self.qlevel(0, &self.chain[..n], &vals, &f0, None)?];
        for (i, g) in reduced.chunks(n).enumerate() {
            let lv = self.qlevel(i + 1, g, &vals, &f0, levels.last())?;
            levels.push(lv);
        }
        // data type and codecs (spec/zarrify/qptiff.md §4.1
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/qptiff.md#41-data-types-and-codecs>)
        let mut env = rules::env([
            ("bits", num(bits as f64)),
            ("sample_format", num(sample_format as f64)),
            ("spp", num(spp as f64)),
            ("planar", num(planar as f64)),
            ("compression", num(compression as f64)),
            ("photometric", photometric.map(|p| num(p as f64)).unwrap_or(V::Absent)),
        ]);
        self.rules.check("image", &mut env)?;
        // the tiles or strips of every IFD of every level: their tables, in one batch
        let used: Vec<usize> = levels.iter().flat_map(|l| l.ifds.iter().copied()).collect();
        for (k, m) in used.iter().copied().zip(self.load(&used, true, false)?) {
            let d = vals.get(&k).and_then(|old| old.get(&270).cloned());
            let mut m = m;
            if let Some(d) = d {
                m.insert(270, d);
            }
            vals.insert(k, m);
        }
        let contig = spp > 1 && planar == 1;
        let samples = if spp > 1 && !contig { spp } else { 1 };
        let jpeg = compression == 7;
        let mut prefixes = HashMap::new();
        for lv in &levels {
            let per: u128 = if lv.strips {
                lv.h.div_ceil(lv.th) as u128
            } else {
                lv.w.div_ceil(lv.tw) as u128 * lv.h.div_ceil(lv.th) as u128
            };
            for &k in &lv.ifds {
                let m = &vals[&k];
                let (ot, ct) = if lv.strips { (273, 279) } else { self.tile_tables(k) };
                let offsets = self.reqs(k, m, ot)?;
                let counts = self.reqs(k, m, ct)?;
                let (prefix, has_tables, tables_ok) = jpeg_prefix(m, jpeg, spp, photometric);
                self.rules.check(
                    "plane",
                    rules::env([
                        ("offset", num(self.ifds[k].offset as f64)),
                        ("jpeg", flag(jpeg)),
                        ("has_tables", flag(has_tables)),
                        ("tables_ok", flag(tables_ok)),
                        ("tiles", num(offsets.len() as f64)),
                        ("counts", num(counts.len() as f64)),
                        ("expected", num((samples as u128 * per) as f64)),
                    ]),
                )?;
                if prefixes.contains_key(&k) {
                    continue;
                }
                for (j, (&o, &c)) in offsets.iter().zip(&counts).enumerate() {
                    if c == 0 {
                        continue;
                    }
                    let within = (o as u128 + c as u128) <= self.size as u128;
                    self.rules.check(
                        "tile",
                        rules::env([
                            ("k", num(j as f64)),
                            ("offset", num(self.ifds[k].offset as f64)),
                            ("within", flag(within)),
                            ("count", num(c as f64)),
                            ("jpeg", flag(jpeg)),
                        ]),
                    )?;
                }
                prefixes.insert(k, prefix);
            }
        }
        let keep = self.pad_strips(&mut levels, &vals, bits, spp, planar, compression)?;
        let levels: Vec<Level> = levels.into_iter().zip(keep).filter(|(_, k)| *k).map(|(l, _)| l).collect();
        // pixel size (the TIFF convention's resolution tags) and position (spec/zarrify/qptiff.md §4.3
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/qptiff.md#43-arrays>)
        let per_unit = self
            .rules
            .table("resolution_units")
            .get(first_int(m0, 296).unwrap_or(0).to_string())
            .and_then(|x| x.as_f64());
        let rational = |tag: u16| -> Option<(u64, u64)> {
            let Some(Loaded::Pairs(p)) = m0.get(&tag) else { return None };
            p.first().copied()
        };
        let physical = |tag: u16| -> Option<f64> {
            let pu = per_unit?;
            let (a, b) = rational(tag)?;
            if a == 0 || b == 0 {
                return None;
            }
            let pixel = pu / (a as f64 / b as f64);
            (pixel < 25.4).then_some(pixel)
        };
        let (px, py) = (physical(282), physical(283));
        let has_c = n > 1 || spp > 1;
        let axes: Vec<&str> = if has_c { vec!["c", "y", "x"] } else { vec!["y", "x"] };
        let mut units = serde_json::Map::new();
        for (a, p) in [("x", px), ("y", py)] {
            if p.is_some() {
                units.insert(a.into(), json!("micrometer"));
            }
        }
        let (w0, h0) = (levels[0].w, levels[0].h);
        let scales: Vec<J> = levels
            .iter()
            .map(|lv| {
                let sy = py.unwrap_or(1.0) * (h0 as f64 / lv.h as f64);
                let sx = px.unwrap_or(1.0) * (w0 as f64 / lv.w as f64);
                if has_c { json!([1, sy, sx]) } else { json!([sy, sx]) }
            })
            .collect();
        let mut translation = J::Null;
        if let (Some(_), Some(_), Some(pu)) = (px, py, per_unit) {
            let position = |tag: u16| -> Option<f64> {
                let (a, b) = rational(tag)?;
                (b != 0).then(|| a as f64 / b as f64 * pu)
            };
            if let (Some(x), Some(y)) = (position(286), position(287)) {
                self.rules.check("translation", rules::env([("x", num(x)), ("y", num(y))]))?;
                translation = if has_c { json!([0, y, x]) } else { json!([y, x]) };
            }
        }
        let channels: Vec<J> = kinds[..n]
            .iter()
            .map(|q| {
                let q = q.as_ref().expect("a channel is a described FullResolution image");
                json!({"name": q.name, "color": q.color})
            })
            .collect();
        let name = kinds[0].as_ref().and_then(|q| q.slide_id.clone()).filter(|s| !s.is_empty());
        let facts = json!({
            "profile": "qptiff",
            "little": self.le,
            "bigtiff": self.big,
            "ifds": self.chain.len(),
            "bits": bits,
            "spp": spp,
            "sample_format": sample_format,
            "planar": planar,
            "compression": compression,
            "predictor": predictor,
            "photometric": photometric,
            "channels": channels,
            "axes": axes,
            "units": units,
            "scales": scales,
            "translation": translation,
            "name": name,
        });
        let _ = prefixes;
        let mut facts = facts;
        facts["levels"] = J::Array(
            levels
                .iter()
                .map(|lv| {
                    let planes: Vec<Option<u32>> = lv.ifds.iter().map(|&k| self.units_of(k, lv.strips)).collect();
                    let declared_by: Vec<u32> = lv.ifds.iter().map(|&k| self.ifds[k].el).collect();
                    json!({"w": lv.w, "h": lv.h, "tw": lv.tw, "th": lv.th, "strips": lv.strips, "join": lv.join, "shard": lv.shard,
                           "rectilinear": lv.rectilinear, "pad": lv.pad, "planes": planes, "declared_by": declared_by})
                })
                .collect(),
        );
        Ok(facts)
    }}
