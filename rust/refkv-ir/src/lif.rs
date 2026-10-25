//! A sans-IO parser of Leica LIF files into the IR (spec/zarrify/lif.md §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#7-lif-profile>, and the source
//! model of §5.4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#54-elements>).
//!
//! Rounds: (1) the file's first 64 KiB, the header block and usually its whole XML; (2) the rest
//! of the XML, when it is longer; (3) the walk over the memory blocks, a batch of block headers
//! at a time, at the offsets the XML's `Memory` elements predict, so that blocks written in the
//! document's order take one round. The XML is read into a tree of its tags; the images, their
//! checks (`schema/lif.json`) and their layout are derived after the walk, and the IR is
//! emitted then. No byte of a block's data is read.

use crate::fed::{Fed, Word};
use crate::ir::*;
use crate::nd2::Step;
use crate::out::MAX_PAYLOAD;
use crate::rules::{self, Rules, flag, num, text};
use crate::sans;
use crate::xml::{self, Ev, TagRef};
use serde_json::{Map, Value as J, json};
use std::collections::HashMap;

/// The header block's fixed part: test value, length, magic byte, XML characters.
const HEAD: u64 = 13;
const HEAD_TYPE: &str = "{test:<u4,length:<u4,magic:u1,characters:<u4}";
/// A memory block's fixed part, by the XML's version: 32-bit (1) or 64-bit (2) data sizes.
const BLOCK_TYPES: [&str; 2] = [
    "{test:<u4,length:<u4,magic:u1,size:<u4,magic2:u1,characters:<u4}",
    "{test:<u4,length:<u4,magic:u1,size:<u8,magic2:u1,characters:<u4}",
];
/// The test value and the magic byte (spec/zarrify/lif.md §2.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#21-blocks>).
pub const TEST: u32 = 0x70;
pub const MAGIC: u8 = 0x2A;
/// The first read: the header block and, usually, its XML.
const FIRST: u64 = 1 << 16;
/// The block headers a walk round asks for, and the ID bytes it reads with a header it guesses.
const PREDICT: usize = 4096;
const ID_GUESS: u64 = 256;
/// The bytes of a band of rows, and the blocks the walk reads at most (spec/zarrify/lif.md §7.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#73-rejection>).
const BAND: u64 = 1 << 24;
const MAX_BLOCKS: usize = 1 << 20;
/// The longest value the mirror's view shows.
const SHOWN: u64 = 1024;
/// The largest integer JSON and binary64 hold exactly.
const MAX_SAFE: u64 = (1 << 53) - 1;

/// Whether a file's first bytes are a LIF file's (spec/zarrify/lif.md §7.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#71-detection>): the test value
/// 0x70 as a 32-bit integer at byte 0, and the magic byte at byte 8.
pub fn detect(head: &[u8]) -> bool {
    head.len() >= 9 && head[..4] == [0x70, 0, 0, 0] && head[8] == MAGIC
}

// ---- the XML header as a tree of tags (spec/zarrify/lif.md §2.2
// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#22-the-xml-header>)

struct Node<'a> {
    tag: TagRef<'a>,
    parent: u32,
    first: u32,
    next: u32,
    last: u32,
}

/// The elements of the tag scan, nested as the CZI convention nests them (spec/zarrify/czi.md
/// §2.7 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#27-the-metadata-xml>): a
/// start tag that is not self-closing opens an element, an end tag closes the innermost open
/// element of its name and every element opened after it, and one that matches none is ignored.
struct Tree<'a> {
    x: &'a str,
    nodes: Vec<Node<'a>>,
    /// the first element at depth 0, and the end of its subtree (the next element at depth 0)
    root: Option<(u32, u32)>,
}

impl<'a> Tree<'a> {
    fn build(x: &'a str) -> Tree<'a> {
        let mut nodes: Vec<Node<'a>> = Vec::new();
        let mut open: Vec<u32> = Vec::new();
        let mut counts: HashMap<&'a str, u32> = HashMap::new();
        let mut root: Option<(u32, u32)> = None;
        for ev in xml::scan(x) {
            let Ev::Tag(t) = ev else { continue };
            if t.is_end {
                if counts.get(t.name).copied().unwrap_or(0) > 0 {
                    while let Some(n) = open.pop() {
                        let name = nodes[n as usize].tag.name;
                        *counts.entry(name).or_default() -= 1;
                        if name == t.name {
                            break;
                        }
                    }
                }
                continue;
            }
            let id = nodes.len() as u32;
            let parent = open.last().copied().unwrap_or(NONE);
            nodes.push(Node { tag: t, parent, first: NONE, next: NONE, last: NONE });
            if parent == NONE {
                match root {
                    None => root = Some((id, u32::MAX)),
                    Some((r, u32::MAX)) => root = Some((r, id)),
                    _ => {}
                }
            } else {
                let p = &mut nodes[parent as usize];
                let prev = p.last;
                p.last = id;
                if prev == NONE {
                    p.first = id;
                } else {
                    nodes[prev as usize].next = id;
                }
            }
            if !t.self_closing {
                open.push(id);
                *counts.entry(t.name).or_default() += 1;
            }
        }
        let n = nodes.len() as u32;
        Tree { x, nodes, root: root.map(|(r, e)| (r, e.min(n))) }
    }

    fn name(&self, n: u32) -> &'a str {
        self.nodes[n as usize].tag.name
    }

    /// The first attribute `name`, references decoded.
    fn attr(&self, n: u32, name: &str) -> Option<String> {
        self.nodes[n as usize].tag.attr(self.x, name).map(|a| a.0)
    }

    /// Every attribute, references decoded, in order; of a repeated name the first.
    fn attrs(&self, n: u32) -> Map<String, J> {
        let mut out = Map::new();
        for (k, v, _) in self.nodes[n as usize].tag.attrs(self.x) {
            if !out.contains_key(&k) {
                out.insert(k, J::String(xml::decode_refs(&v)));
            }
        }
        out
    }

    fn children(&self, n: u32) -> impl Iterator<Item = u32> + '_ {
        let mut c = self.nodes[n as usize].first;
        std::iter::from_fn(move || {
            (c != NONE).then(|| {
                let out = c;
                c = self.nodes[c as usize].next;
                out
            })
        })
    }

    fn named(&self, n: u32, name: &'a str) -> impl Iterator<Item = u32> + '_ {
        self.children(n).filter(move |&c| self.name(c) == name)
    }

    fn child(&self, n: u32, name: &'a str) -> Option<u32> {
        self.named(n, name).next()
    }

    /// The first child along each step of `path`.
    fn at(&self, n: u32, path: &[&'a str]) -> Option<u32> {
        path.iter().try_fold(n, |m, step| self.child(m, step))
    }
}

/// An attribute's integer: its text, whitespace trimmed, is one or more digits of a value at most
/// 2^53 - 1.
fn integer(v: Option<&str>) -> Option<u64> {
    let t = v?.trim_matches([' ', '\t', '\r', '\n']);
    if t.is_empty() || !t.bytes().all(|c| c.is_ascii_digit()) || t.trim_start_matches('0').len() > 16 {
        return None;
    }
    t.parse::<u64>().ok().filter(|&x| x <= MAX_SAFE)
}

/// An attribute's decimal (whitespace trimmed), when finite.
fn decimal(v: Option<&str>) -> Option<f64> {
    xml::decimal_value(v?.trim_matches([' ', '\t', '\r', '\n']), false)
}

/// An integer attribute as a role's given: its value, its text when not an integer, else absent.
fn given(v: &Option<String>) -> crate::expr::V {
    match (v, integer(v.as_deref())) {
        (_, Some(x)) => num(x as f64),
        (Some(t), None) => text(t),
        (None, None) => crate::expr::V::Absent,
    }
}

/// A unit symbol (conventions §5 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#5-units>):
/// its unit and, for a length, its size in meters.
fn unit(s: &str) -> Option<(&'static str, Option<f64>)> {
    Some(match s {
        "\u{b5}m" | "\u{3bc}m" | "um" => ("micrometer", Some(1e-6)),
        "nm" => ("nanometer", Some(1e-9)),
        "mm" => ("millimeter", Some(1e-3)),
        "cm" => ("centimeter", Some(1e-2)),
        "m" => ("meter", Some(1.0)),
        "\u{c5}" | "\u{212b}" => ("angstrom", Some(1e-10)),
        "pm" => ("picometer", Some(1e-12)),
        "in" => ("inch", Some(0.0254)),
        "ft" => ("foot", Some(0.3048)),
        "s" => ("second", None),
        "ms" => ("millisecond", None),
        "min" => ("minute", None),
        "h" => ("hour", None),
        _ => return None,
    })
}

/// A channel's color from its ChannelTag (1, 2, 3: red, green, blue) or its LUTName
/// (spec/zarrify/lif.md §4.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#43-image-arrays>).
fn color(tag: Option<u64>, lut: Option<&str>) -> &'static str {
    match tag {
        Some(1) => return "FF0000",
        Some(2) => return "00FF00",
        Some(3) => return "0000FF",
        _ => {}
    }
    match lut.map(|l| l.to_ascii_lowercase()).as_deref() {
        Some("red") => "FF0000",
        Some("green") => "00FF00",
        Some("blue") => "0000FF",
        Some("cyan") => "00FFFF",
        Some("magenta") => "FF00FF",
        Some("yellow") => "FFFF00",
        _ => "FFFFFF",
    }
}

// ---- the blocks

/// A memory block the walk found.
struct Block {
    start: u64,
    fields: Vec<u8>,
    id: Vec<u8>,
    size: u64,
    /// the image that names it
    image: Option<usize>,
}

impl Block {
    fn header(&self) -> u64 {
        self.fields.len() as u64 + self.id.len() as u64
    }
    fn data(&self) -> u64 {
        self.start + self.header()
    }
}

/// A `Memory` element of the XML, in document order: what the walk predicts.
struct Predicted {
    id: Vec<u8>,
    size: u64,
}

/// One image element, read from the XML: what its checks and layout use, and its source
/// metadata (spec/zarrify/lif.md §5.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#52-the-images>).
struct Image {
    name: Option<String>,
    memory: Option<String>,
    /// DimID, NumberOfElements, BytesInc, Origin, Length, Unit, BitInc (attributes as given)
    dims: Vec<[Option<String>; 7]>,
    /// DataType, Resolution, BytesInc, ChannelTag, LUTName, BitInc (as given)
    channels: Vec<[Option<String>; 6]>,
    /// path, attributes, memory, dimensions and channels
    meta: Map<String, J>,
    /// the TileScanInfo attachment's attributes, and its tiles'
    scan: Option<(Map<String, J>, Vec<J>)>,
}

#[derive(Clone, Copy, PartialEq, Debug)]
enum Phase {
    Start,
    Head,
    Xml,
    Walk,
    Done,
}

pub struct Lif {
    size: u64,
    phase: Phase,
    fed: Fed,
    ir: Ir,
    rules: &'static Rules,
    /// the XML header's characters
    chars: u64,
    head: Vec<u8>,
    /// the XML header's bytes, when the view shows them (conventions §8.7
    /// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#87-the-view>)
    xml: Vec<u8>,
    images: Vec<Image>,
    /// 1 or 2: the width of a block's data size
    version: u64,
    predicted: Vec<Predicted>,
    next_predicted: usize,
    blocks: Vec<Block>,
    at: u64,
    tail: Option<(u64, u64)>,
    facts: J,
    pub asked: sans::Asked,
}

impl Lif {
    pub fn new(size: u64) -> Self {
        Lif {
            size,
            phase: Phase::Start,
            fed: Fed::default(),
            ir: Ir::new(size),
            rules: rules::of("lif"),
            chars: 0,
            head: Vec::new(),
            xml: Vec::new(),
            images: Vec::new(),
            version: 2,
            predicted: Vec::new(),
            next_predicted: 0,
            blocks: Vec::new(),
            at: 0,
            tail: None,
            facts: J::Null,
            asked: sans::Asked::default(),
        }
    }

    pub fn feed(&mut self, offset: u64, data: Vec<u8>) {
        self.fed.feed(offset, data);
    }

    pub fn step(&mut self) -> Result<Step, String> {
        loop {
            match self.phase {
                Phase::Start => {
                    self.phase = Phase::Head;
                    return Ok(self.asked.ask(vec![(0, self.size.min(FIRST))]));
                }
                Phase::Head => {
                    self.header()?;
                    self.phase = Phase::Xml;
                    let end = HEAD + 2 * self.chars;
                    if !self.fed.has(HEAD, 2 * self.chars) {
                        return Ok(self.asked.ask(vec![(HEAD, end - HEAD)]));
                    }
                }
                Phase::Xml => {
                    self.read_xml()?;
                    self.fed.clear();
                    self.at = HEAD + 2 * self.chars;
                    self.phase = Phase::Walk;
                }
                Phase::Walk => {
                    if let Some(ask) = self.walk() {
                        self.fed.clear();
                        return Ok(self.asked.ask(ask));
                    }
                    self.fed.clear();
                    self.layout()?;
                    self.phase = Phase::Done;
                    return Ok(Step::Done);
                }
                Phase::Done => return Ok(Step::Done),
            }
        }
    }

    /// The header block (spec/zarrify/lif.md §2.1
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#21-blocks>): its test value,
    /// magic byte and XML length.
    fn header(&mut self) -> Res<()> {
        self.rules.check("file", rules::env([("size", num(self.size as f64))]))?;
        let h = self.fed.get(0, HEAD).ok_or("internal: no header block")?.to_vec();
        let chars = u32::le_at(&h, 9) as u64;
        self.rules.check(
            "header",
            rules::env([
                ("test", num(u32::le_at(&h, 0))),
                ("magic", num(h[8])),
                ("chars", num(chars as f64)),
                ("size", num(self.size as f64)),
            ]),
        )?;
        self.chars = chars;
        self.head = h;
        Ok(())
    }

    /// The XML header (spec/zarrify/lif.md §2.2
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#22-the-xml-header>): its root
    /// and version, and the `Memory` elements the walk predicts.
    fn read_xml(&mut self) -> Res<()> {
        let xml = self.fed.get(HEAD, 2 * self.chars).ok_or("internal: no XML")?;
        let units: Vec<u16> = xml.chunks_exact(2).map(|c| u16::le_at(c, 0)).collect();
        if xml.len() as u64 <= SHOWN {
            self.xml = xml.to_vec();
        }
        let x = String::from_utf16(&units).ok();
        let lof = x.as_deref() == Some("LMS_Object_File");
        let tree = x.as_deref().map(Tree::build);
        let root = tree.as_ref().and_then(|t| t.root.map(|r| t.name(r.0).to_string()));
        let version = tree.as_ref().and_then(|t| t.root.and_then(|r| t.attr(r.0, "Version")));
        self.rules.check(
            "xml",
            rules::env([
                ("utf16", flag(x.is_some())),
                ("lof", flag(lof)),
                ("root", root.as_deref().map_or(crate::expr::V::Absent, text)),
                ("version", given(&version)),
            ]),
        )?;
        self.version = integer(version.as_deref()).unwrap_or(2);
        let tree = tree.ok_or("internal: no XML tree")?;
        // the file's own element's block, then the blocks of data, each ID once: LAS X names
        // empty and repeated IDs it writes no block for
        let (r, end) = tree.root.ok_or("internal: no root")?;
        let mut seen = std::collections::HashSet::new();
        for n in r..end {
            let Some(m) = (tree.name(n) == "Element").then(|| tree.child(n, "Memory")).flatten() else { continue };
            let (Some(id), Some(size)) = (tree.attr(m, "MemoryBlockID"), integer(tree.attr(m, "Size").as_deref())) else { continue };
            let id: Vec<u8> = id.encode_utf16().flat_map(|u| u.to_le_bytes()).collect();
            if (size > 0 || seen.is_empty()) && seen.insert(id.clone()) {
                self.predicted.push(Predicted { id, size });
            }
        }
        self.images = (r..end)
            .filter(|&n| tree.name(n) == "Element" && tree.at(n, &["Data", "Image"]).is_some())
            .map(|n| read_image(&tree, n))
            .collect();
        Ok(())
    }

    fn fixed(&self) -> u64 {
        if self.version == 1 { 18 } else { 22 }
    }

    /// Walks the blocks from `at` while their headers are held (spec/zarrify/lif.md §7.2
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#72-reading>); the reads of the
    /// next round when a header is not, or None when the walk is over.
    fn walk(&mut self) -> Option<Vec<(u64, u64)>> {
        let fixed = self.fixed();
        loop {
            let at = self.at;
            if at == self.size {
                return None;
            }
            if self.blocks.len() >= MAX_BLOCKS || self.size - at < fixed {
                self.tail = Some((at, self.size - at));
                return None;
            }
            let Some(h) = self.fed.get(at, fixed).map(|h| h.to_vec()) else {
                return Some(self.predict(at, None));
            };
            let size = if self.version == 1 { u32::le_at(&h, 9) as u64 } else { u64::le_at(&h, 9) };
            let w = if self.version == 1 { 4 } else { 8 };
            let chars = u32::le_at(&h, 10 + w) as u64;
            let id_end = at + fixed + 2 * chars;
            let ok = u32::le_at(&h, 0) == TEST
                && h[8] == MAGIC
                && h[9 + w] == MAGIC
                && id_end <= self.size
                && size <= MAX_SAFE
                && size <= self.size - id_end;
            if !ok {
                self.tail = Some((at, self.size - at));
                return None;
            }
            let Some(id) = self.fed.get(at + fixed, 2 * chars).map(|b| b.to_vec()) else {
                // the whole header: the round's reads replace what the last one held
                return Some(self.predict(at, Some(fixed + 2 * chars)));
            };
            if let Some(k) = self.predicted[self.next_predicted..].iter().position(|p| p.id == id) {
                self.next_predicted += k + 1;
            }
            self.blocks.push(Block { start: at, fields: h, id, size, image: None });
            self.at = id_end + size;
        }
    }

    /// A round's reads: the block header at `at` (`header` bytes when its ID's length is known,
    /// else its fixed part and a guess), then the headers the XML's `Memory` elements predict
    /// after it, in document order.
    fn predict(&self, at: u64, header: Option<u64>) -> Vec<(u64, u64)> {
        let fixed = self.fixed();
        let mut out = vec![(at, header.unwrap_or(fixed + ID_GUESS).min(self.size - at))];
        let mut o = at;
        for p in self.predicted[self.next_predicted..].iter().take(PREDICT) {
            let len = fixed + p.id.len() as u64;
            if o != at {
                out.push((o, len.min(self.size - o)));
            }
            let Some(next) = o.checked_add(len).and_then(|e| e.checked_add(p.size)) else { break };
            if next >= self.size {
                break;
            }
            o = next;
        }
        out
    }

    /// The images, their checks and their layout (spec/zarrify/lif.md §3
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#3-placement>), then the IR.
    fn layout(&mut self) -> Res<()> {
        let images = std::mem::take(&mut self.images);
        let mut by_id: HashMap<Vec<u8>, Vec<usize>> = HashMap::new();
        for (i, b) in self.blocks.iter().enumerate() {
            by_id.entry(b.id.clone()).or_default().push(i);
        }
        let mut named: Vec<Option<usize>> = vec![None; self.blocks.len()];
        let mut facts = Vec::new();
        let mut series = 0u64;
        for (k, im) in images.iter().enumerate() {
            let shown = im.name.clone().unwrap_or_default();
            let mut f = self.image(k, &shown, im)?;
            let id = im.memory.clone().unwrap_or_default();
            let key: Vec<u8> = id.encode_utf16().flat_map(|u| u.to_le_bytes()).collect();
            let found = by_id.get(key.as_slice()).cloned().unwrap_or_default();
            let block = found.first().copied();
            let other = block.and_then(|b| named[b]);
            let held = block.map_or(0, |b| self.blocks[b].size);
            let need = f.end;
            self.rules.check(
                "memory",
                rules::env([
                    ("k", num(k as f64)),
                    ("name", text(&shown)),
                    ("id", text(&id)),
                    ("found", num(found.len() as f64)),
                    ("other", other.map_or(crate::expr::V::Absent, |o| num(o as f64))),
                    ("need", num(need as f64)),
                    ("need_shown", text(&need.to_string())),
                    ("held", num(held as f64)),
                ]),
            )?;
            let b = block.ok_or("internal: no block")?;
            named[b] = Some(k);
            self.blocks[b].image = Some(k);
            f.place(self.blocks[b].data(), b);
            series += f.fields.len() as u64;
            self.rules.check("series", rules::env([("count", num(series as f64))]))?;
            facts.push(image_facts(im, &f, k));
        }
        self.emit()?;
        let mut facts = json!({"version": self.version, "blocks": self.blocks.len(), "images": facts, "series": series});
        self.asked.put(&mut facts);
        self.facts = facts;
        Ok(())
    }

    /// An image's checks and layout (spec/zarrify/lif.md §3
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#3-placement>).
    fn image(&self, k: usize, name: &str, im: &Image) -> Res<Layout> {
        let base = |extra: Vec<(&'static str, crate::expr::V)>| {
            let mut v = rules::env([("k", num(k as f64)), ("name", text(name))]);
            for (a, b) in extra {
                v.insert(a, b);
            }
            v
        };
        self.rules.check(
            "image",
            base(vec![
                ("memory", flag(im.memory.is_some())),
                ("dimensions", num(im.dims.len() as f64)),
                ("channels", num(im.channels.len() as f64)),
            ]),
        )?;
        // (DimID, NumberOfElements, BytesInc)
        let mut dims: Vec<(u64, u64, u64)> = Vec::new();
        for (j, d) in im.dims.iter().enumerate() {
            self.rules.check(
                "dimension",
                base(vec![
                    ("j", num(j as f64)),
                    ("dim_id", given(&d[0])),
                    ("elements", given(&d[1])),
                    ("bytes_inc", given(&d[2])),
                    ("bit_inc", given(&d[6])),
                ]),
            )?;
            dims.push((integer(d[0].as_deref()).unwrap(), integer(d[1].as_deref()).unwrap(), integer(d[2].as_deref()).unwrap()));
        }
        let mut channels: Vec<(u64, u64, u64)> = Vec::new(); // (DataType, item size, BytesInc)
        let mut resolutions = Vec::new();
        for (j, c) in im.channels.iter().enumerate() {
            self.rules.check(
                "channel",
                base(vec![
                    ("j", num(j as f64)),
                    ("data_type", given(&c[0])),
                    ("resolution", given(&c[1])),
                    ("bytes_inc", given(&c[2])),
                    ("bit_inc", given(&c[5])),
                ]),
            )?;
            let (dt, res) = (integer(c[0].as_deref()).unwrap(), integer(c[1].as_deref()).unwrap());
            let item = match res {
                1..=8 => 1,
                9..=16 => 2,
                17..=32 => 4,
                _ => 8,
            };
            channels.push((dt, item, integer(c[2].as_deref()).unwrap()));
            resolutions.push(res);
        }
        // the references a run may make (refs.rs), which bound the planes before they are listed
        let references = (1u128 << 22) + 4 * self.size as u128;
        Layout::of(self.rules, base(vec![("references", num(references as f64))]), &dims, &channels, &resolutions, im)
    }

    /// The IR (spec/zarrify/lif.md §5.4
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#54-elements>).
    fn emit(&mut self) -> Res<()> {
        let xml_len = 2 * self.chars;
        let header = self.ir.struct_(0, "header", NO_INDEX, 0, Some((0, HEAD + xml_len)))?;
        self.ir.value(header, "fields", NO_INDEX, HEAD_TYPE, 0, (0, HEAD), Some(&self.head))?;
        if xml_len > 0 {
            // held when the view shows it (conventions §8.7
            // <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#87-the-view>)
            let shown = (xml_len <= SHOWN).then_some(self.xml.as_slice());
            self.ir.value(header, "xml", NO_INDEX, &format!("utf16[{xml_len}]"), 0, (HEAD, xml_len), shown)?;
        }
        let ty = BLOCK_TYPES[(self.version == 2) as usize];
        for (i, b) in self.blocks.iter().enumerate() {
            let s = self.ir.struct_(0, "blocks/", i as u64, 0, Some((b.start, b.header() + b.size)))?;
            self.ir.value(s, "fields", NO_INDEX, ty, 0, (b.start, b.fields.len() as u64), Some(&b.fields))?;
            if !b.id.is_empty() {
                let n = b.id.len() as u64;
                self.ir.value(s, "id", NO_INDEX, &format!("utf16[{n}]"), 0, (b.start + b.fields.len() as u64, n), Some(&b.id))?;
            }
            if b.size == 0 {
                continue;
            }
            match b.image {
                Some(k) => {
                    let form = json!({"geometry": {"image": k}, "codec": {"endian": "little"}, "recipe": [["src", 0, null]]});
                    self.ir.data(s, "data", NO_INDEX, 0, (b.data(), b.size), &form.to_string())?;
                }
                None => {
                    self.ir.value(s, "data", NO_INDEX, &format!("bytes[{}]", b.size), 0, (b.data(), b.size), None)?;
                }
            }
        }
        if let Some((o, n)) = self.tail {
            self.ir.value(0, "tail", NO_INDEX, &format!("bytes[{n}]"), 0, (o, n), None)?;
        }
        Ok(())
    }

    pub fn finish(self) -> Result<(Ir, J), String> {
        sans::finish(self.phase == Phase::Done, self.ir, self.facts)
    }
}

crate::parser!(Lif);

/// An image element's attributes the layout reads (spec/zarrify/lif.md §2.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#23-images>).
fn read_image(t: &Tree, n: u32) -> Image {
    let desc = t.at(n, &["Data", "Image", "ImageDescription"]);
    let list = |name, item| -> Vec<u32> {
        desc.and_then(|d| t.child(d, name)).map(|l| t.named(l, item).collect()).unwrap_or_default()
    };
    let (dims, channels) = (list("Dimensions", "DimensionDescription"), list("Channels", "ChannelDescription"));
    let mut path = Vec::new();
    let mut e = n;
    while e != NONE {
        if t.name(e) == "Element" {
            path.push(t.attr(e, "Name").map_or(J::Null, J::String));
        }
        e = t.nodes[e as usize].parent;
    }
    path.reverse();
    let memory = t.child(n, "Memory");
    let mut meta = Map::new();
    meta.insert("path".into(), J::Array(path));
    meta.insert("attributes".into(), J::Object(t.attrs(n)));
    if let Some(m) = memory {
        meta.insert("memory".into(), J::Object(t.attrs(m)));
    }
    meta.insert("dimensions".into(), J::Array(dims.iter().map(|&d| J::Object(t.attrs(d))).collect()));
    meta.insert("channels".into(), J::Array(channels.iter().map(|&c| J::Object(t.attrs(c))).collect()));
    let scan = t
        .at(n, &["Data", "Image"])
        .and_then(|i| t.named(i, "Attachment").find(|&a| t.attr(a, "Name").as_deref() == Some("TileScanInfo")))
        .map(|s| (t.attrs(s), t.named(s, "Tile").map(|c| J::Object(t.attrs(c))).collect()));
    Image {
        name: t.attr(n, "Name"),
        memory: memory.and_then(|m| t.attr(m, "MemoryBlockID")),
        dims: dims.iter().map(|&c| ["DimID", "NumberOfElements", "BytesInc", "Origin", "Length", "Unit", "BitInc"].map(|k| t.attr(c, k))).collect(),
        channels: channels.iter().map(|&c| ["DataType", "Resolution", "BytesInc", "ChannelTag", "LUTName", "BitInc"].map(|k| t.attr(c, k))).collect(),
        meta,
        scan,
    }
}

/// An image's layout (spec/zarrify/lif.md §3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#3-placement>).
struct Layout {
    data_type: &'static str,
    item: u64,
    /// the channels' BytesInc, ascending, and each one's index in the XML
    offsets: Vec<u64>,
    order: Vec<usize>,
    interleaved: bool,
    axes: Vec<&'static str>,
    shape: Vec<u64>,
    chunk_shape: Vec<u64>,
    /// (count, stride) of t and z (count 1 without the axis)
    t: (u64, u64),
    z: (u64, u64),
    rows: u64,
    row: u64,
    y_stride: u64,
    band: u64,
    /// the field dimensions (index in the list, DimID, count, stride), and each field's
    /// indexes and offset
    field_dims: Vec<(usize, u64, u64, u64)>,
    fields: Vec<(Vec<u64>, u64)>,
    scale: Vec<J>,
    units: Map<String, J>,
    resolution: Vec<u64>,
    /// the bytes of the block the image needs
    end: u128,
    base: u64,
    block: usize,
}

impl Layout {
    fn of(rules: &Rules, mut env: rules::Vars<'static>, dims: &[(u64, u64, u64)], channels: &[(u64, u64, u64)],
          resolutions: &[u64], im: &Image) -> Res<Layout> {
        let used: Vec<usize> = (0..dims.len()).filter(|&j| dims[j].1 > 1).collect();
        let x = used.iter().copied().find(|&j| dims[j].0 == 1).or_else(|| dims.iter().position(|d| d.0 == 1));
        let mut seen: HashMap<u64, usize> = HashMap::new();
        let repeated = used.iter().find_map(|&j| seen.insert(dims[j].0, j).map(|_| dims[j].0));
        let types: Vec<(u64, u64)> = {
            let mut v: Vec<(u64, u64)> = channels.iter().map(|c| (c.0, c.1)).collect();
            v.sort_unstable();
            v.dedup();
            v
        };
        let (dt, item) = (channels[0].0, channels[0].1);
        let mut order: Vec<usize> = (0..channels.len()).collect();
        order.sort_by_key(|&j| (channels[j].2, j));
        let offsets: Vec<u64> = order.iter().map(|&j| channels[j].2).collect();
        let distinct = offsets.windows(2).all(|w| w[0] < w[1]);
        let m = channels.len() as u64;
        let sx = x.map_or(0, |j| dims[j].2);
        let nx = x.map_or(1, |j| dims[j].1);
        let interleaved = m > 1 && sx == m * item && offsets.iter().enumerate().all(|(j, &o)| o == j as u64 * item);
        let planar = sx == item;
        let y = used.iter().copied().find(|&j| dims[j].0 == 2);
        let (ny, sy) = y.map_or((1, 0), |j| (dims[j].1, dims[j].2));
        let row = nx.saturating_mul(sx);
        let rows_overlap = ny > 1 && sy < row;
        let find = |id: u64| used.iter().copied().find(|&j| dims[j].0 == id).map(|j| (dims[j].1, dims[j].2));
        let (t, z) = (find(4), find(3));
        let mut axes = Vec::new();
        let mut shape = Vec::new();
        let mut chunk = Vec::new();
        if let Some((n, _)) = t {
            axes.push("t");
            shape.push(n);
            chunk.push(1);
        }
        if m > 1 {
            axes.push("c");
            shape.push(m);
            chunk.push(if interleaved { m } else { 1 });
        }
        if let Some((n, _)) = z {
            axes.push("z");
            shape.push(n);
            chunk.push(1);
        }
        axes.extend(["y", "x"]);
        shape.extend([ny, nx]);
        chunk.extend([1, nx]);
        let biggest = shape.iter().copied().max().unwrap_or(1);
        let end: u128 = offsets.last().copied().unwrap_or(0) as u128
            + dims.iter().map(|d| (d.1.saturating_sub(1)) as u128 * d.2 as u128).sum::<u128>()
            + item as u128;
        let field_dims: Vec<(usize, u64, u64, u64)> =
            used.iter().copied().filter(|&j| !(1..=4).contains(&dims[j].0)).map(|j| (j, dims[j].0, dims[j].1, dims[j].2)).collect();
        let field_count: u128 = field_dims.iter().map(|d| d.2 as u128).product();
        env.insert("x", flag(x.is_some()));
        env.insert("repeated", repeated.map_or(crate::expr::V::Absent, |r| num(r as f64)));
        env.insert("types", num(types.len() as f64));
        env.insert("distinct", flag(distinct));
        env.insert("offsets", text(&format!("{offsets:?}")));
        env.insert("x_bytes_inc", num(sx as f64));
        env.insert("arranged", flag(interleaved || planar));
        env.insert("rows_overlap", flag(rows_overlap));
        env.insert("row", num(row as f64));
        env.insert("y_bytes_inc", num(sy as f64));
        env.insert("extent", num(biggest as f64));
        env.insert("fields", num(field_count as f64));
        let chunked = if interleaved { 1 } else { m as u128 };
        let planes = t.map_or(1, |p| p.0 as u128) * chunked * z.map_or(1, |p| p.0 as u128) * field_count;
        env.insert("planes", num(planes as f64));
        rules.check("layout", env)?;
        let data_type = match (dt, item) {
            (0, 1) => "uint8",
            (0, 2) => "uint16",
            (0, 4) => "uint32",
            (0, _) => "uint64",
            (_, 2) => "float16",
            (_, 4) => "float32",
            _ => "float64",
        };
        // scales and units (spec/zarrify/lif.md §4.3
        // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#43-image-arrays>)
        let mut units = Map::new();
        let scale = axes
            .iter()
            .map(|&a| {
                let id = match a {
                    "t" => 4,
                    "z" => 3,
                    "y" => 2,
                    "x" => 1,
                    _ => return json!(1),
                };
                let Some(j) = used.iter().copied().find(|&j| dims[j].0 == id) else { return json!(1) };
                let d = &im.dims[j];
                let step = decimal(d[4].as_deref()).map(|l| (l / (dims[j].1 - 1) as f64).abs());
                let Some(step) = step.filter(|s| s.is_finite() && *s > 0.0) else { return json!(1) };
                match (unit(d[5].as_deref().unwrap_or("")), a) {
                    (Some((_, Some(m))), "x" | "y" | "z") => {
                        let v = step * (m / 1e-6);
                        if !v.is_finite() || v <= 0.0 {
                            return json!(1);
                        }
                        units.insert(a.into(), json!("micrometer"));
                        json!(v)
                    }
                    (Some((u, None)), "t") => {
                        units.insert(a.into(), json!(u));
                        json!(step)
                    }
                    _ => json!(step),
                }
            })
            .collect();
        let mut resolution = vec![0; channels.len()];
        for (c, &j) in order.iter().enumerate() {
            resolution[c] = resolutions[j];
        }
        Ok(Layout {
            data_type,
            item,
            offsets,
            order,
            interleaved,
            axes,
            shape,
            chunk_shape: chunk,
            t: t.unwrap_or((1, 0)),
            z: z.unwrap_or((1, 0)),
            rows: ny,
            row,
            y_stride: if ny == 1 { row } else { sy },
            band: 1,
            field_dims,
            fields: Vec::new(),
            scale,
            units,
            resolution,
            end,
            base: 0,
            block: 0,
        })
    }

    /// The fields' offsets and the band height (spec/zarrify/lif.md §3.4
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#34-rows-and-bands>), once
    /// the image is known to lie within its block, whose data starts at `base`.
    fn place(&mut self, base: u64, block: usize) {
        self.base = base;
        self.block = block;
        let count: u64 = self.field_dims.iter().map(|d| d.2).product();
        self.fields = (0..count)
            .map(|f| {
                let mut rest = f;
                let mut idx = Vec::new();
                let mut off = 0;
                for d in &self.field_dims {
                    idx.push(rest % d.2);
                    off += rest % d.2 * d.3;
                    rest /= d.2;
                }
                (idx, off)
            })
            .collect();
        let (ny, row, sy) = (self.rows, self.row, self.y_stride);
        // the farthest plane: its last time, channel, depth and field
        let far = base
            + if self.interleaved { 0 } else { self.offsets.last().copied().unwrap_or(0) }
            + (self.t.0 - 1) * self.t.1
            + (self.z.0 - 1) * self.z.1
            + self.fields.last().map_or(0, |f| f.1);
        let most = (BAND / row.max(1)).clamp(1, ny);
        self.band = (1..=most)
            .rev()
            .find(|&b| ny % b == 0 && (sy == row || rows_payload(far + (ny - b) * sy, sy, row, b) <= MAX_PAYLOAD as u64))
            .unwrap_or(1);
        let n = self.chunk_shape.len();
        self.chunk_shape[n - 2] = self.band;
    }
}

/// The size of a varint (spec/zarrify.md §1.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>).
fn varint_size(v: u64) -> u64 {
    (64 - v.leading_zeros() as u64).div_ceil(7).max(1)
}

/// The reference payload of `count` ranges of `length` bytes, `stride` bytes apart from `start`
/// (each offset and length above 0, `stride` too), as `out::payload_size` gives it, counted by
/// the offsets' varint sizes rather than built.
fn rows_payload(start: u64, stride: u64, length: u64, count: u64) -> u64 {
    if count == 1 {
        return 2 + varint_size(start) + varint_size(length);
    }
    // each range a Concat entry: tag, its length (one byte), then 0x18 varint(o) 0x20 varint(n)
    let mut offsets = 0;
    let mut below = 0;
    for k in 1..=9u64 {
        let limit = 1u64 << (7 * k);
        let n = if start >= limit { 0 } else { count.min((limit - start).div_ceil(stride)) };
        offsets += (n - below) * k;
        below = n;
    }
    offsets += (count - below) * 10;
    count * (4 + varint_size(length)) + offsets
}

/// An image's facts: its source metadata and its layout, for the projection.
fn image_facts(im: &Image, f: &Layout, k: usize) -> J {
    // a mosaic's tiles: when its one field dimension is the mosaic's (DimID 10)
    let tiles: &[J] = match &im.scan {
        Some((_, tiles)) if f.field_dims.len() == 1 && f.field_dims[0].1 == 10 => tiles,
        _ => &[],
    };
    let colors: Vec<&str> = f.order.iter().map(|&j| color(integer(im.channels[j][3].as_deref()), im.channels[j][4].as_deref())).collect();
    let mut meta = Map::new();
    meta.insert("image".into(), json!(k));
    meta.extend(im.meta.clone());
    if let Some((scan, _)) = im.scan.as_ref().filter(|_| !tiles.is_empty()) {
        meta.insert("tile_scan".into(), J::Object(scan.clone()));
    }
    json!({
        "name": im.name, "meta": meta, "data_type": f.data_type, "item": f.item,
        "interleaved": f.interleaved, "axes": f.axes, "shape": f.shape, "chunk_shape": f.chunk_shape,
        "offsets": f.offsets, "order": f.order, "colors": colors, "resolution": f.resolution,
        "t": [f.t.0, f.t.1], "z": [f.z.0, f.z.1], "rows": f.rows, "row": f.row, "y_stride": f.y_stride,
        "band": f.band, "base": f.base, "block": f.block,
        "field_dims": f.field_dims.iter().map(|d| d.1).collect::<Vec<_>>(),
        "fields": f.fields.iter().map(|(i, o)| json!([i, o])).collect::<Vec<_>>(),
        "tiles": tiles,
        "scale": f.scale, "units": f.units,
    })
}

/// The LIF profile's file run (spec/zarrify/lif.md).
pub struct Profile;

impl crate::run::FileProfile for Profile {
    /// The test value at byte 0 and the magic byte at byte 8 (`detect`).
    fn detect(&self, head: &[u8]) -> bool {
        detect(head)
    }
    fn parser(&self, size: u64) -> crate::run::AnyParser {
        crate::run::AnyParser::of::<Lif>(size)
    }
    fn project(&self, ir: &crate::ir::Ir, facts: &serde_json::Value, url: &str, _copied: &crate::project::Copied) -> Result<crate::out::Out, String> {
        crate::project::lif::project(ir, facts, url)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::out::{payload_size, Part};

    #[test]
    fn rows_payload_is_the_references() {
        for (start, stride, length, count) in [(1, 9, 6, 1), (100, 9, 6, 50), (16000, 9, 6, 200), (73728, 9, 6, 8192), (1 << 40, 1 << 20, 3, 7), (5, 300, 200, 4096)] {
            let parts: Vec<Part> = (0..count).map(|r| Part::Src(start + r * stride, length)).collect();
            assert_eq!(rows_payload(start, stride, length, count), payload_size(&parts) as u64, "{start} {stride} {count}");
        }
    }
}
