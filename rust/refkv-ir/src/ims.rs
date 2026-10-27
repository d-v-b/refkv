//! The sans-IO IMS parser (spec/zarrify/ims.md
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ims.md>): an Imaris file is an HDF5
//! file, read by `hdf5.rs`. Two phases, each run in rounds (`hdf5.rs`'s replay):
//!
//! 1. **The layout** (spec/zarrify/ims.md §2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ims.md#2-the-source>–§4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ims.md#4-output>): the groups, attributes and datasets the
//!    convention reads, in its order, every rejection of the profile checked here (the HDF5
//!    subset by the reader, the Imaris layout by `schema/ims.json`'s roles). Its outcome is the
//!    facts the projection writes: the root's metadata, the level arrays, and each chunk's key.
//! 2. **The walk**, which never rejects: every object reachable from the root group by hard
//!    links (by level, at most `walk_objects` + `walk_per_image` × the Data datasets, to
//!    `walk_depth` levels), its header, the structures that hold its links and attributes, its
//!    chunk index and chunks, and the global heap collections its attributes name. A structure
//!    outside the reader's subset is not described: its bytes are gaps.
//!
//! `finish` emits the IR from what the two phases read: the superblock, then the objects,
//! numbered depth first from the root group (a group's hard links in ascending byte order of
//! their names), then the global heap collections.

use crate::out::{omero_channel, omero_json, omero_window};
use crate::check;
use crate::hdf5::walk::{lenient, visit, Emit, Visit};
use crate::hdf5::{self, le, Acc, Fail, Links, R, Target, H5};
use crate::ir::{Ir, NO_INDEX};
use crate::nd2::Step;
use crate::rules::{self, flag, num, text, Rules, Vars};
use crate::expr::V;
use crate::text::{is_decimal, utf8_or_latin1};
use serde_json::{json, Map, Value as J};
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::sync::Arc as Rc;

const WS: &[char] = &[' ', '\t', '\r', '\n'];

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Phase {
    Layout,
    Walk,
    Done,
}

/// A Data dataset of the image: its level, time point and channel, its header, and its
/// chunk grid (z, y, x).
struct Image {
    r: usize,
    t: usize,
    c: usize,
    at: u64,
    grid: [u64; 3],
    /// it has allocated chunks
    allocated: bool,
}


pub struct Ims {
    h: H5,
    rules: &'static Rules,
    phase: Phase,
    rounds: u64,
    requests: u64,
    requested: u64,
    /// the layout's outcome
    facts: Option<J>,
    image_sets: Vec<Image>,
    /// the objects the layout reads (they are walked first, whatever the budget)
    core: BTreeSet<u64>,
    images: u64,
    // the walk
    visited: HashMap<u64, Visit>,
    level: Vec<u64>,
    depth: u64,
    extension: Option<Visit>,
    /// the walk's room in elements, when a test sets it (else the schema's)
    pub room: Option<u64>,
}


// ---- text (spec/zarrify/ims.md §2.1
// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ims.md#21-attributes-as-text>)

/// The text of a string's data: up to the first NUL, UTF-8 or else Latin-1.
pub fn decode_text(data: &[u8]) -> String {
    utf8_or_latin1(data.split(|&c| c == 0).next().unwrap_or(&[]))
}

/// A decimal number, or None if `s` is absent or not one.
pub fn decimal(s: Option<&str>) -> Option<f64> {
    let t = s?.trim_matches(WS);
    if !is_decimal(t) {
        return None;
    }
    let v: f64 = t.parse().ok()?;
    v.is_finite().then_some(v)
}

/// `n` decimal numbers separated by whitespace, or None.
pub fn decimals(s: Option<&str>, n: u64) -> Option<Vec<f64>> {
    let t = s?.trim_matches(WS);
    let parts: Vec<&str> = t.split(WS).filter(|p| !p.is_empty()).collect();
    let parts = if t.is_empty() { vec![""] } else { parts };
    if parts.len() as u64 != n {
        return None;
    }
    parts.iter().map(|p| decimal(Some(p))).collect()
}

/// (all digits, the value from 1 to 2^53 − 1) of an integer's text.
fn integer_text(s: &str) -> (bool, Option<u64>) {
    let t = s.trim_matches(WS);
    if t.is_empty() || !t.bytes().all(|c| c.is_ascii_digit()) {
        return (false, None);
    }
    let d = t.trim_start_matches('0');
    if d.is_empty() || d.len() > 16 {
        return (true, None);
    }
    let v: u64 = d.parse().unwrap();
    (true, (v <= hdf5::MAX_SAFE).then_some(v))
}

/// An integer from 1 to 2^53 − 1, or None.
pub fn integer(s: Option<&str>) -> Option<u64> {
    integer_text(s?).1
}

fn unit_rgb(values: Option<&[f64]>) -> Option<String> {
    let v = values?;
    if !v.iter().all(|&x| (0.0..=1.0).contains(&x)) {
        return None;
    }
    Some(v.iter().map(|&x| format!("{:02X}", (x * 255.0 + 0.5).floor() as i64)).collect())
}

fn days(y: i64, m: i64, d: i64) -> i64 {
    let y = y - (m <= 2) as i64;
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let doy = (153 * (m + if m > 2 { -3 } else { 9 }) + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146097 + doe - 719468
}

/// (whole seconds from 1970-01-01, fraction of a second) of a valid time point.
pub fn timestamp(s: Option<&str>) -> Option<(i64, f64)> {
    let b = s?.as_bytes();
    if b.len() < 19 {
        return None;
    }
    let digits = |r: std::ops::Range<usize>| -> Option<i64> {
        let p = &b[r];
        p.iter().all(|c| c.is_ascii_digit()).then(|| std::str::from_utf8(p).unwrap().parse().unwrap())
    };
    if b[4] != b'-' || b[7] != b'-' || b[10] != b' ' || b[13] != b':' || b[16] != b':' {
        return None;
    }
    let (y, mo, d, h, mi, sec) = (digits(0..4)?, digits(5..7)?, digits(8..10)?, digits(11..13)?, digits(14..16)?, digits(17..19)?);
    let frac = if b.len() > 19 {
        if b[19] != b'.' || b.len() == 20 || !b[20..].iter().all(|c| c.is_ascii_digit()) {
            return None;
        }
        &s.unwrap()[20..]
    } else {
        "0"
    };
    let leap = y % 4 == 0 && (y % 100 != 0 || y % 400 == 0);
    let month = [31, if leap { 29 } else { 28 }, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
    if !((1..=12).contains(&mo) && d >= 1 && d <= month[(mo - 1) as usize] && h <= 23 && mi <= 59 && sec <= 59) {
        return None;
    }
    Some((days(y, mo, d) * 86400 + h * 3600 + mi * 60 + sec, format!("0.{frac}").parse().unwrap()))
}

/// The length unit of a `Unit` text (conventions §5
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#5-units>).
fn length_unit(s: &str) -> Option<&'static str> {
    Some(match s {
        "\u{b5}m" | "\u{3bc}m" | "um" => "micrometer",
        "nm" => "nanometer",
        "mm" => "millimeter",
        "cm" => "centimeter",
        "m" => "meter",
        "\u{c5}" | "\u{212b}" => "angstrom",
        "pm" => "picometer",
        "in" => "inch",
        "ft" => "foot",
        _ => return None,
    })
}

/// A JSON number: an integer when it is one (as Python writes an int), else a float.
fn jf(x: f64) -> J {
    json!(x)
}

impl Ims {
    pub fn new(size: u64) -> Self {
        Ims {
            h: H5::new(size),
            rules: rules::of("ims"),
            phase: Phase::Layout,
            rounds: 0,
            requests: 0,
            requested: 0,
            facts: None,
            image_sets: Vec::new(),
            core: BTreeSet::new(),
            images: 0,
            visited: HashMap::new(),
            level: Vec::new(),
            depth: 0,
            extension: None,
            room: None,
        }
    }

    pub fn feed(&mut self, offset: u64, data: Vec<u8>) {
        self.h.src.store.feed(offset, data);
    }

    fn check(&self, role: &str, mut env: Vars<'static>) -> R<()> {
        self.rules.check(role, &mut env).map_err(Fail::Bad)
    }

    pub fn step(&mut self) -> Result<Step, String> {
        loop {
            self.h.src.want.clear();
            let r = match self.phase {
                Phase::Layout => self.layout(),
                Phase::Walk => self.walk(),
                Phase::Done => return Ok(Step::Done),
            };
            match r {
                Ok(()) => {
                    self.phase = if self.phase == Phase::Layout { Phase::Walk } else { Phase::Done };
                }
                Err(Fail::Bad(m)) => {
                    if self.phase == Phase::Layout {
                        return Err(m);
                    }
                    return Err(format!("internal: the walk rejected: {m}"));
                }
                Err(Fail::Miss) => {
                    let mut want = std::mem::take(&mut self.h.src.want);
                    if want.is_empty() {
                        return Err("internal: a round wants nothing".into());
                    }
                    self.rounds += 1;
                    if self.rounds as f64 > self.rules.limit("max_rounds") {
                        return Err(format!("more than {} rounds of dependent reads", self.rules.limit("max_rounds")));
                    }
                    want.sort_unstable();
                    want.dedup();
                    // merge ranges that overlap
                    let mut merged: Vec<(u64, u64)> = Vec::new();
                    for (o, n) in want {
                        if let Some(last) = merged.last_mut() {
                            if o <= last.0 + last.1 {
                                last.1 = last.1.max(o + n - last.0);
                                continue;
                            }
                        }
                        merged.push((o, n));
                    }
                    self.requests += merged.len() as u64;
                    self.requested += merged.iter().map(|r| r.1).sum::<u64>();
                    return Ok(Step::Read(merged));
                }
            }
        }
    }

    // ---- phase 1: the layout (spec/zarrify/ims.md §2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ims.md#2-the-source>–§4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ims.md#4-output>)

    /// The object a group's link `name` leads to, None when the link is absent and not
    /// `required` (spec/zarrify/ims.md §2.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ims.md#22-the-imaris-layout>).
    fn group(&mut self, links: &Links, name: &str, required: bool) -> R<Option<u64>> {
        if !links.has(name) {
            if required {
                self.check("link", rules::env([("name", text(name)), ("present", flag(false))]))?;
            }
            return Ok(None);
        }
        let at = self.h.follow(links, name.as_bytes())?;
        self.core.insert(at);
        Ok(Some(at))
    }

    fn opened(&mut self, links: &Links, name: &str) -> R<(u64, Rc<Links>)> {
        let at = self.group(links, name, true)?.unwrap();
        Ok((at, self.h.links(at)?))
    }

    /// The text of a string attribute, or None if it is absent.
    fn attr_text(&self, attrs: Option<&hdf5::Attrs>, name: &str) -> R<Option<String>> {
        let Some(d) = attrs.and_then(|a| a.get(name)) else { return Ok(None) };
        let (dt, _, data) = hdf5::attribute_value(d)?;
        self.check("string", rules::env([("name", text(name)), ("class", num(dt.cls))]))?;
        Ok(Some(decode_text(&data)))
    }

    /// The text of an attribute that never rejects the input.
    fn optional_text(&self, attrs: Option<&hdf5::Attrs>, name: &str) -> R<Option<String>> {
        Ok(lenient(self.attr_text(attrs, name))?.flatten())
    }

    /// (Zarr data type, byte order or None for one byte, integer range) of a Data dataset.
    fn data_type(&self, dt: &hdf5::Datatype) -> R<(String, Option<&'static str>, Option<(i64, i64)>)> {
        let order = if dt.bits & 1 != 0 { "big" } else { "little" };
        let plain = if dt.cls == 0 && [1, 2, 4].contains(&dt.size) {
            le(&dt.props, 0, 2)? == 0 && le(&dt.props, 2, 2)? == 8 * dt.size
        } else {
            true
        };
        let ieee32 = if dt.cls == 1 && dt.size == 4 {
            if dt.bits & 0x40 != 0 || (dt.bits >> 4) & 3 != 2 || (dt.bits >> 8) & 0xFF != 31 {
                false
            } else {
                let (a, b) = (le(&dt.props, 0, 2)?, le(&dt.props, 2, 2)?);
                let mid = dt.props.get(4..8).map(|x| x.to_vec());
                let bias = le(&dt.props, 8, 4)?;
                a == 0 && b == 32 && mid == Some(vec![23, 8, 0, 23]) && bias == 127
            }
        } else {
            true
        };
        self.check(
            "datatype",
            rules::env([("class", num(dt.cls)), ("size", num(dt.size as f64)), ("plain", flag(plain)), ("ieee32", flag(ieee32))]),
        )?;
        let bits = 8 * dt.size;
        if dt.cls == 0 {
            let o = if bits > 8 { Some(order) } else { None };
            if dt.bits & 8 != 0 {
                return Ok((format!("int{bits}"), o, Some((-(1i64 << (bits - 1)), (1i64 << (bits - 1)) - 1))));
            }
            return Ok((format!("uint{bits}"), o, Some((0, (1i64 << bits) - 1))));
        }
        Ok(("float32".into(), Some(order), None))
    }

    fn layout(&mut self) -> R<()> {
        if self.facts.is_some() {
            return Ok(());
        }
        let sb = self.h.superblock()?;
        self.core.insert(sb.root);
        let root = self.h.links(sb.root)?;
        self.check("imaris", rules::env([("dataset", flag(root.has("DataSet")))]))?;
        let (_, dataset) = self.opened(&root, "DataSet")?;
        self.check("imaris", rules::env([("dataset", flag(true)), ("level0", flag(dataset.has("ResolutionLevel 0")))]))?;
        let mut levels = 0u64;
        while levels <= 64 && dataset.has(&format!("ResolutionLevel {levels}")) {
            levels += 1;
        }
        self.check("levels", rules::env([("levels", num(levels as f64))]))?;
        let (_, level0) = self.opened(&dataset, "ResolutionLevel 0")?;
        let mut times = 0u64;
        while level0.has(&format!("TimePoint {times}")) {
            times += 1;
        }
        self.check("times", rules::env([("times", num(times as f64))]))?;
        let (_, first_time) = self.opened(&level0, "TimePoint 0")?;
        let mut channels = 0u64;
        while first_time.has(&format!("Channel {channels}")) {
            channels += 1;
        }
        self.check("channels", rules::env([("channels", num(channels as f64))]))?;
        self.check(
            "datasets",
            rules::env([("levels", num(levels as f64)), ("times", num(times as f64)), ("channels", num(channels as f64))]),
        )?;

        // the datasets of every level, time point and channel, and the metadata: independent reads
        let mut top = Acc::default();
        let images = top.take(self.datasets(&dataset, &level0, &first_time, levels, times, channels));
        let meta = if top.stopped() { None } else { top.take(self.metadata(&root, times, channels)) };
        top.done()?;
        let (level_info, data_type, chunk_list) = images.unwrap();
        let meta = meta.unwrap();
        let facts = self.output(&level_info, &data_type, meta, levels, times, channels)?;
        self.image_sets = chunk_list;
        self.images = level_info.len() as u64 * times * channels;
        self.facts = Some(facts);
        Ok(())
    }

    /// Levels' (sizes, chunk, compressed), the data type, and each dataset's chunks.
    #[allow(clippy::type_complexity)]
    fn datasets(&mut self, dataset: &Links, level0: &Rc<Links>, first_time: &Rc<Links>, levels: u64, times: u64, channels: u64)
                -> R<(Vec<([u64; 3], [u64; 3], bool)>, (String, Option<&'static str>, Option<(i64, i64)>), Vec<Image>)> {
        let mut acc = Acc::default();
        let mut data_type: Option<(String, Option<&'static str>, Option<(i64, i64)>)> = None;
        let mut level_info = Vec::new();
        let mut chunk_list = Vec::new();
        'levels: for r in 0..levels as usize {
            let links = if r == 0 {
                Some(level0.clone())
            } else {
                let x = self.opened(dataset, &format!("ResolutionLevel {r}")).map(|x| x.1);
                acc.take(x)
            };
            let mut info: Option<([u64; 3], [u64; 3], bool)> = None;
            if let Some(links) = links {
                for t in 0..times as usize {
                    let time_links = if r == 0 && t == 0 {
                        Some(first_time.clone())
                    } else {
                        let x = self.opened(&links, &format!("TimePoint {t}")).map(|x| x.1);
                        acc.take(x)
                    };
                    let Some(time_links) = time_links else {
                        if acc.stopped() {
                            break 'levels;
                        }
                        continue;
                    };
                    for c in 0..channels as usize {
                        let one = self.image(&time_links, r, c);
                        if let Some((sizes, ds, at)) = acc.take(one) {
                            let dt = match self.data_type(&ds.datatype) {
                                Ok(v) => v,
                                Err(e) => {
                                    acc.take::<()>(Err(e));
                                    break 'levels;
                                }
                            };
                            let larger = sizes.iter().zip(&ds.dims).any(|(s, n)| s > n);
                            let this = if ds.dims.len() == 3 {
                                Some(([sizes[0], sizes[1], sizes[2]], [ds.chunk[0], ds.chunk[1], ds.chunk[2]], !ds.filters.is_empty()))
                            } else {
                                None
                            };
                            let same_type = data_type.as_ref().is_none_or(|d| *d == dt);
                            let same_level = info.is_none() || info == this;
                            let filters = V::List(ds.filters.iter().map(|&f| V::Num(f as f64)).collect());
                            let r_ = self.check(
                                "image",
                                rules::env([
                                    ("level", num(r as f64)),
                                    ("rank", num(ds.dims.len() as f64)),
                                    ("larger", flag(larger)),
                                    ("filters", filters),
                                    ("same_type", flag(same_type)),
                                    ("same_level", flag(same_level)),
                                ]),
                            );
                            if acc.take(r_).is_none() {
                                if acc.stopped() {
                                    break 'levels;
                                }
                                continue;
                            }
                            data_type.get_or_insert(dt);
                            info.get_or_insert(this.unwrap());
                            let ch = self.h.chunks(at, &ds, false);
                            if let Some(ch) = acc.take(ch) {
                                chunk_list.push(Image { r, t, c, at, grid: [ds.grid[0], ds.grid[1], ds.grid[2]], allocated: !ch.list.is_empty() });
                            }
                        }
                        if acc.stopped() {
                            break 'levels;
                        }
                    }
                }
            }
            if acc.stopped() {
                break;
            }
            level_info.push(info.unwrap_or(([1, 1, 1], [1, 1, 1], false)));
        }
        acc.done()?;
        Ok((level_info, data_type.unwrap(), chunk_list))
    }

    /// A channel group's image size and Data dataset (spec/zarrify/ims.md §2.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ims.md#22-the-imaris-layout>).
    fn image(&mut self, time_links: &Links, r: usize, c: usize) -> R<([u64; 3], hdf5::Dataset, u64)> {
        let channel = self.group(time_links, &format!("Channel {c}"), true)?.unwrap();
        let attrs = self.h.attributes(channel)?;
        let mut sizes = [0u64; 3];
        for (i, axis) in ["Z", "Y", "X"].iter().enumerate() {
            let s = self.attr_text(Some(&attrs), &format!("ImageSize{axis}"))?;
            let (integer, value) = s.as_deref().map(integer_text).unwrap_or((false, None));
            self.check(
                "image_size",
                rules::env([("axis", text(axis)), ("level", num(r as f64)), ("integer", flag(integer)), ("in_range", flag(value.is_some()))]),
            )?;
            sizes[i] = value.unwrap();
        }
        let links = self.h.links(channel)?;
        let data_at = self.group(&links, "Data", true)?.unwrap();
        let ds = self.h.dataset(data_at, false)?;
        Ok((sizes, ds, data_at))
    }

    /// The metadata groups' attributes (spec/zarrify/ims.md §3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ims.md#3-metadata>), and the channels' colors.
    fn metadata(&mut self, root: &Links, times: u64, channels: u64) -> R<Meta> {
        let meta = if root.has("DataSetInfo") {
            let (_, l) = self.opened(root, "DataSetInfo")?;
            Some(l)
        } else {
            None
        };
        let mut acc = Acc::default();
        let image = acc.take(self.meta_group(meta.as_deref(), "Image"));
        let mut groups = Vec::new();
        if channels <= self.rules.limit("max_channels") as u64 {
            for c in 0..channels {
                let g = self.meta_group(meta.as_deref(), &format!("Channel {c}"));
                groups.push(acc.take(g));
                if acc.stopped() {
                    break;
                }
            }
        }
        let time = if acc.stopped() { None } else { acc.take(self.meta_group(meta.as_deref(), "TimeInfo")) };
        acc.done()?;
        let (image, _) = image.unwrap();
        let im = image.as_deref();
        let mut ext_min = [None; 3];
        let mut ext_max = [None; 3];
        for i in 0..3 {
            ext_min[i] = decimal(self.attr_text(im, &format!("ExtMin{i}"))?.as_deref());
        }
        for i in 0..3 {
            ext_max[i] = decimal(self.attr_text(im, &format!("ExtMax{i}"))?.as_deref());
        }
        let unit_text = self.attr_text(im, "Unit")?;
        let mut name = self.attr_text(im, "Name")?;
        if name.as_ref().is_some_and(|n| n.len() > self.rules.limit("max_name") as usize) {
            name = None;
        }
        let mut labels = Vec::new();
        let mut colors = Vec::new();
        let mut ranges = Vec::new();
        let mut acc = Acc::default();
        for (c, g) in groups.into_iter().enumerate() {
            let (attrs, links) = g.unwrap();
            let a = attrs.as_deref();
            let label = self.attr_text(a, "Name")?;
            labels.push(match label {
                Some(l) if !l.is_empty() && l.len() <= self.rules.limit("max_name") as usize => l,
                _ => format!("Channel {c}"),
            });
            let color = self.channel_color(a, links.as_deref());
            match acc.take(color) {
                Some(col) => colors.push(col),
                None => colors.push(String::new()),
            }
            if acc.stopped() {
                break;
            }
            ranges.push(decimals(self.attr_text(a, "ColorRange")?.as_deref(), 2));
        }
        acc.done()?;
        let (time_attrs, _) = time.unwrap();
        let mut period = None;
        if times > 1 {
            let ta = time_attrs.as_deref();
            let first = timestamp(self.attr_text(ta, "TimePoint1")?.as_deref());
            let last = timestamp(self.attr_text(ta, &format!("TimePoint{times}"))?.as_deref());
            if let (Some(f), Some(l)) = (first, last) {
                let elapsed = (l.0 - f.0) as f64 + (l.1 - f.1);
                if elapsed > 0.0 {
                    period = Some(elapsed / (times - 1) as f64);
                }
            }
        }
        Ok(Meta { ext_min, ext_max, unit_text, name, labels, colors, ranges, period })
    }

    /// A group's attributes and links, or none if it is missing.
    #[allow(clippy::type_complexity)]
    fn meta_group(&mut self, meta: Option<&Links>, name: &str) -> R<(Option<Rc<hdf5::Attrs>>, Option<Rc<Links>>)> {
        let Some(meta) = meta else { return Ok((None, None)) };
        let Some(at) = self.group(meta, name, false)? else { return Ok((None, None)) };
        let mut acc = Acc::default();
        let links = acc.take(self.h.links(at));
        let attrs = if acc.stopped() { None } else { acc.take(self.h.attributes(at)) };
        acc.done()?;
        Ok((attrs, links))
    }

    /// A channel's omero color (spec/zarrify/ims.md §4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ims.md#4-output>).
    fn channel_color(&mut self, attrs: Option<&hdf5::Attrs>, links: Option<&Links>) -> R<String> {
        let base = unit_rgb(decimals(self.attr_text(attrs, "Color")?.as_deref(), 3).as_deref());
        let mode = self.optional_text(attrs, "ColorMode")?;
        let table_first = mode.as_deref().is_some_and(|m| m.trim_matches(WS) == "TableColor");
        if base.is_some() && !table_first {
            return Ok(base.unwrap());
        }
        let length = integer(self.optional_text(attrs, "ColorTableLength")?.as_deref());
        let table = match length {
            Some(l) => decimals(self.color_table(attrs, links)?.as_deref(), 3 * l),
            None => None,
        };
        let top = table.as_ref().and_then(|t| unit_rgb(Some(&t[t.len() - 3..])));
        Ok(top.or(base).unwrap_or_else(|| "FFFFFF".into()))
    }

    /// A channel's ColorTable, as text: its attribute, else the small string dataset its link
    /// leads to; None if neither can be read, never rejecting (spec/zarrify/ims.md §3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ims.md#3-metadata>).
    fn color_table(&mut self, attrs: Option<&hdf5::Attrs>, links: Option<&Links>) -> R<Option<String>> {
        if attrs.is_some_and(|a| a.get("ColorTable").is_some()) {
            return self.optional_text(attrs, "ColorTable");
        }
        let Some(links) = links.filter(|l| l.has("ColorTable")) else { return Ok(None) };
        let max = self.rules.limit("max_color_table") as u128;
        let r = (|| -> R<Option<String>> {
            let at = self.h.follow(links, b"ColorTable")?;
            self.core.insert(at);
            let ds = self.h.dataset(at, true)?;
            if ds.datatype.cls != 3 || ds.filters.len() > 0 || !matches!(ds.layout, hdf5::Layout::Compact | hdf5::Layout::Contiguous) {
                return Ok(None);
            }
            let n = hdf5::product(&ds.dims).saturating_mul(ds.datatype.size as u128);
            if n > max {
                return Ok(None);
            }
            let n = n as u64;
            let data = if ds.layout == hdf5::Layout::Compact {
                if ds.compact.1.len() as u64 != n {
                    return Ok(None);
                }
                ds.compact.1.clone()
            } else {
                let Some(a) = ds.contiguous.0 else { return Ok(None) };
                if a as u128 + n as u128 > self.h.size() as u128 {
                    return Ok(None);
                }
                self.h.read(a, n)?
            };
            Ok(Some(decode_text(&data)))
        })();
        Ok(lenient(r)?.flatten())
    }

    /// The facts the projection writes (spec/zarrify/ims.md §4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ims.md#4-output>).
    fn output(&self, level_info: &[([u64; 3], [u64; 3], bool)], data_type: &(String, Option<&'static str>, Option<(i64, i64)>), meta: Meta,
              levels: u64, times: u64, channels: u64) -> R<J> {
        let z0 = level_info[0].0[0];
        let has_z = z0 > 1 || level_info.iter().any(|i| i.1[0] > 1);
        let mut axes: Vec<&str> = Vec::new();
        if times > 1 {
            axes.push("t");
        }
        if channels > 1 {
            axes.push("c");
        }
        if has_z {
            axes.push("z");
        }
        axes.extend(["y", "x"]);
        let most = level_info.iter().map(|i| i.0[0]).max().unwrap_or(1);
        self.check("z_levels", rules::env([("z0", num(z0 as f64)), ("most", num(most as f64))]))?;
        let unit = match meta.unit_text.as_deref() {
            None | Some("") => Some("micrometer"),
            Some(u) => length_unit(u),
        };
        let mut extent: HashMap<&str, f64> = HashMap::new();
        for (axis, i) in [("x", 0usize), ("y", 1), ("z", 2)] {
            if let (Some(lo), Some(hi)) = (meta.ext_min[i], meta.ext_max[i]) {
                let e = hi - lo;
                self.check("extent", rules::env([("i", num(i as f64)), ("finite", flag(e.is_finite()))]))?;
                if e > 0.0 {
                    extent.insert(axis, e);
                }
            }
        }
        let spatial: Vec<&str> = axes.iter().copied().filter(|a| matches!(*a, "z" | "y" | "x")).collect();
        let mut units = Map::new();
        units.insert("t".into(), meta.period.map_or(J::Null, |_| json!("second")));
        for a in &spatial {
            units.insert((*a).into(), if extent.contains_key(a) { unit.map_or(J::Null, |u| json!(u)) } else { J::Null });
        }
        let mut scales = Vec::new();
        let mut arrays = Vec::new();
        for (sizes, chunk, compressed) in level_info {
            let n = |a: &str| match a {
                "z" => sizes[0],
                "y" => sizes[1],
                _ => sizes[2],
            };
            let scale: Vec<J> = axes
                .iter()
                .map(|a| match *a {
                    "t" => meta.period.map_or(json!(1), jf),
                    "c" => json!(1),
                    a => extent.get(a).map_or(json!(1), |e| jf(e / n(a) as f64)),
                })
                .collect();
            scales.push(J::Array(scale));
            let shape: Vec<u64> = axes.iter().map(|a| match *a {
                "t" => times,
                "c" => channels,
                a => n(a),
            }).collect();
            let chunks: Vec<u64> = axes.iter().map(|a| match *a {
                "t" | "c" => 1,
                "z" => chunk[0],
                "y" => chunk[1],
                _ => chunk[2],
            }).collect();
            let mut codecs = vec![match data_type.1 {
                Some(e) => json!({"name": "bytes", "configuration": {"endian": e}}),
                None => json!({"name": "bytes"}),
            }];
            if *compressed {
                codecs.push(json!({"name": "numcodecs.zlib", "configuration": {"level": 1}}));
            }
            arrays.push(json!({"shape": shape, "chunks": chunks, "codecs": codecs, "sizes": sizes, "chunk": chunk}));
        }
        let translations = if spatial.iter().all(|a| extent.contains_key(a)) {
            let t: Vec<J> = axes
                .iter()
                .map(|a| match *a {
                    "x" => jf(meta.ext_min[0].unwrap()),
                    "y" => jf(meta.ext_min[1].unwrap()),
                    "z" => jf(meta.ext_min[2].unwrap()),
                    _ => json!(0),
                })
                .collect();
            J::Array(vec![J::Array(t); levels as usize])
        } else {
            J::Null
        };
        let mut omero = J::Null;
        if channels <= self.rules.limit("max_channels") as u64 {
            let mut chs = Vec::new();
            for (k, label) in meta.labels.iter().enumerate() {
                let rng = meta.ranges.get(k).cloned().flatten();
                let window = match (data_type.2, rng) {
                    (Some((lo, hi)), Some(r)) => Some(omero_window(json!(lo), json!(hi), jf(r[0]), jf(r[1]))),
                    (Some((lo, hi)), None) => Some(omero_window(json!(lo), json!(hi), json!(lo), json!(hi))),
                    (None, Some(r)) => Some(omero_window(jf(r[0]), jf(r[1]), jf(r[0]), jf(r[1]))),
                    (None, None) => None,
                };
                chs.push(omero_channel(label, meta.colors[k].as_str(), window.as_ref()));
            }
            omero = omero_json(chs);
        }
        let name = meta.name.filter(|n| !n.is_empty()).map_or(J::Null, |n| json!(n));
        Ok(json!({
            "axes": axes,
            "units": units,
            "scales": scales,
            "name": name,
            "translations": translations,
            "omero": omero,
            "dtype": data_type.0,
            "arrays": arrays,
            "summary": {
                "levels": levels,
                "sizes": {"t": times, "c": channels, "z": level_info[0].0[0], "y": level_info[0].0[1], "x": level_info[0].0[2]},
                "dataType": data_type.0,
                "chunkShape": level_info[0].1,
                "compressed": level_info.iter().map(|i| i.2).collect::<Vec<_>>(),
                "channels": meta.labels,
            },
        }))
    }

    // ---- phase 2: the walk


    fn walk(&mut self) -> R<()> {
        let sb = self.h.superblock()?;
        if self.depth == 0 && self.level.is_empty() {
            self.level = self.core.iter().copied().collect();
            if !self.level.contains(&sb.root) {
                self.level.insert(0, sb.root);
            }
        }
        if self.extension.is_none() {
            if let Some(e) = sb.extension {
                let mut acc = Acc::default();
                let h = acc.take(lenient(self.h.header(e)));
                acc.done()?;
                self.extension = Some(Visit { header: h.flatten(), ..Default::default() });
            } else {
                self.extension = Some(Visit::default());
            }
        }
        let budget = self.rules.limit("walk_objects") as u64 + self.rules.limit("walk_per_image") as u64 * self.images;
        let max_depth = self.rules.limit("walk_depth") as u64;
        loop {
            let mut acc = Acc::default();
            for at in self.level.clone() {
                if self.visited.contains_key(&at) {
                    continue;
                }
                if let Some(v) = acc.take(visit(&mut self.h, at, self.rules.limit("walk_collections") as usize)) {
                    self.visited.insert(at, v);
                }
            }
            acc.done()?;
            self.depth += 1;
            if self.depth > max_depth {
                return Ok(());
            }
            let mut next = BTreeSet::new();
            for at in &self.level {
                if let Some(l) = self.visited.get(at).and_then(|v| v.links.as_ref()) {
                    for (_, t) in &l.list {
                        if let Target::Hard(a) = t {
                            if !self.visited.contains_key(a) {
                                next.insert(*a);
                            }
                        }
                    }
                }
            }
            let room = budget.saturating_sub(self.visited.len() as u64) as usize;
            let next: Vec<u64> = next.into_iter().take(room).collect();
            if next.is_empty() {
                return Ok(());
            }
            self.level = next;
        }
    }

    // ---- the IR

    pub fn finish(mut self) -> Result<(Ir, J), String> {
        if self.phase != Phase::Done {
            return Err("internal: the parse is not done".into());
        }
        let size = self.h.size();
        let must: HashSet<u64> = self.image_sets.iter().map(|im| im.at).collect();
        let room = self.room.unwrap_or(self.rules.limit("walk_elements") as u64 + size / self.rules.limit("walk_bytes_per_element") as u64);
        let mut e = Emit { ir: Ir::new(size), h: &self.h, chunks_of: HashMap::new(), room, must };
        let sb = self.h.sb.clone().ok_or("internal: no superblock")?;
        e.superblock(&sb)?;
        if let Some(ext) = self.extension.take() {
            if let Some(h) = &ext.header {
                let s = e.ir.struct_(0, "superblock_extension", NO_INDEX, 0, None)?;
                e.header(s, h)?;
            }
        }
        // objects, depth first from the root group, links in ascending byte order of names
        let mut order = Vec::new();
        let mut seen = HashSet::new();
        let mut stack = vec![sb.root];
        while let Some(at) = stack.pop() {
            if !self.visited.contains_key(&at) || !seen.insert(at) {
                continue;
            }
            order.push(at);
            if let Some(l) = &self.visited[&at].links {
                let mut kids: Vec<(&Vec<u8>, u64)> =
                    l.list.iter().filter_map(|(n, t)| if let Target::Hard(a) = t { Some((n, *a)) } else { None }).collect();
                kids.sort();
                for (_, a) in kids.into_iter().rev() {
                    if !seen.contains(&a) {
                        stack.push(a);
                    }
                }
            }
        }
        let mut rest: Vec<u64> = self.visited.keys().copied().filter(|a| !seen.contains(a)).collect();
        rest.sort_unstable();
        order.extend(rest);
        let objects = e.ir.struct_(0, "objects", NO_INDEX, 0, None)?;
        let mut collections: BTreeMap<u64, Rc<hdf5::Collection>> = BTreeMap::new();
        let mut structs = Vec::with_capacity(order.len());
        for i in 0..order.len() {
            structs.push(e.ir.struct_(objects, "", i as u64, 0, None)?);
        }
        // the objects the layout reads first, then the others while the IR has room
        let (first, rest): (Vec<usize>, Vec<usize>) = (0..order.len()).partition(|&i| self.core.contains(&order[i]));
        for i in first.into_iter().chain(rest) {
            if e.full() && !e.must.contains(&order[i]) {
                continue;
            }
            let v = &self.visited[&order[i]];
            e.object(structs[i], order[i], v)?;
            for c in &v.collections {
                collections.insert(c.at, c.clone());
            }
        }
        if !collections.is_empty() && !e.full() {
            let g = e.ir.struct_(0, "global_heaps", NO_INDEX, 0, None)?;
            for (k, c) in collections.values().enumerate() {
                if e.full() {
                    break;
                }
                e.collection(g, k as u64, c)?;
            }
        }
        // the image's datasets, by the structs that hold their chunks (None: no chunk allocated)
        let mut images = Vec::new();
        for im in &self.image_sets {
            let el = match e.chunks_of.get(&im.at) {
                Some(&x) => json!(x),
                None if im.allocated => return Err(format!("internal: the chunks of the dataset at {} are not described", im.at)),
                None => J::Null,
            };
            images.push(json!([im.r, im.t, im.c, el, im.grid]));
        }
        let mut ir = e.ir;
        ir.sort_values();
        check::finish(&mut ir)?;
        let mut facts = self.facts.take().unwrap_or(J::Null);
        facts["images"] = J::Array(images);
        facts["objects"] = json!(order.len());
        facts["requests"] = json!(self.requests);
        facts["requested"] = json!(self.requested);
        facts["rounds"] = json!(self.rounds);
        Ok((ir, facts))
    }
}

struct Meta {
    ext_min: [Option<f64>; 3],
    ext_max: [Option<f64>; 3],
    unit_text: Option<String>,
    name: Option<String>,
    labels: Vec<String>,
    colors: Vec<String>,
    ranges: Vec<Option<Vec<f64>>>,
    period: Option<f64>,
}


crate::parser!(Ims);

/// The IMS profile's file run (spec/zarrify/ims.md).
pub struct Profile;

impl crate::run::FileProfile for Profile {
    /// The HDF5 signature.
    fn detect(&self, head: &[u8]) -> bool {
        head.len() >= 8 && &head[..8] == crate::hdf5::SIGNATURE
    }
    fn parser(&self, size: u64) -> crate::run::AnyParser {
        crate::run::AnyParser::of::<Ims>(size)
    }
    fn project(&self, ir: &crate::ir::Ir, facts: &serde_json::Value, url: &str, _copied: &crate::project::Copied) -> Result<crate::out::Out, String> {
        crate::project::ims::project(ir, facts, url)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn texts_read_as_numbers() {
        for (s, want) in [("1", Some(1.0)), (" -2.5e1\t", Some(-25.0)), ("1.", Some(1.0)), (".5", Some(0.5)), ("+3", Some(3.0)),
                          ("1e400", None), ("e5", None), ("1e", None), (".", None), ("", None), ("0x10", None), ("1 2", None)] {
            assert_eq!(decimal(Some(s)), want, "{s:?}");
        }
        assert_eq!(decimal(None), None);
        assert_eq!(decimals(Some(" 0 0.5\n1 "), 3), Some(vec![0.0, 0.5, 1.0]));
        assert_eq!(decimals(Some("0 0.5"), 3), None);
        assert_eq!(decimals(Some("0 x 1"), 3), None);
        assert_eq!(decimals(Some(""), 1), None);
        for (s, want) in [("12", Some(12)), (" 007 ", Some(7)), ("0", None), ("9007199254740991", Some(9007199254740991)),
                          ("9007199254740992", None), ("00000000000000000001", Some(1)), ("+1", None), ("1.0", None)] {
            assert_eq!(integer(Some(s)), want, "{s:?}");
        }
        assert_eq!(decode_text(b"ab\0cd"), "ab");
        assert_eq!(decode_text(&[0xb5, b'm']), "\u{b5}m");
        assert_eq!(unit_rgb(Some(&[0.0, 0.5, 1.0])).as_deref(), Some("0080FF"));
        assert_eq!(unit_rgb(Some(&[0.0, 1.5, 1.0])), None);
        assert_eq!(length_unit("\u{3bc}m"), Some("micrometer"));
        assert_eq!(length_unit("s"), None);
    }

    #[test]
    fn time_points_read_as_seconds() {
        assert_eq!(timestamp(Some("1970-01-01 00:00:00")), Some((0, 0.0)));
        assert_eq!(timestamp(Some("2000-02-29 12:30:15.25")), Some((951827415, 0.25)));
        assert_eq!(timestamp(Some("1969-12-31 23:59:59")), Some((-1, 0.0)));
        for bad in ["1900-02-29 00:00:00", "2001-13-01 00:00:00", "2001-01-01 24:00:00", "2001-01-01 00:00:00.", "2001-01-01T00:00:00",
                    "2001-01-01 00:00", "2001-01-01 00:00:00.5x"] {
            assert_eq!(timestamp(Some(bad)), None, "{bad}");
        }
    }
}
