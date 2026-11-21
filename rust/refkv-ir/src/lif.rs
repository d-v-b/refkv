//! A sans-IO parser of Leica LIF, LOF and LIFEXT files into the IR (spec/zarrify/lif.md §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#7-lif-profile>, and the source
//! model of §5.4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#54-elements>).
//!
//! Rounds: (1) the file's first 64 KiB, the header block and usually its whole XML; (2) the rest
//! of the XML, when it is longer; (3) the walk over the memory blocks, a batch of block headers
//! at a time, at the offsets the XML's `Memory` elements predict, so that blocks written in the
//! document's order take one round. A LOF, whose one memory block comes first and whose XML comes
//! after it, reads the XML block at the end of the data instead of a walk. The XML is read into a
//! tree of its tags; after the walk the IR is emitted, with each image's stored array, and the
//! description (`describe.rs`). No byte of a block's data is read. The images' checks
//! (`schema/lif.json`) and their layout are refkv's plan's (`plan.rs`), made from the description.

use crate::fed::{Fed, Word};
use crate::ir::*;
use crate::nd2::Step;
use crate::rules::{self, Rules, flag, num, text};
use crate::sans;
use crate::xml::{self, Ev, TagRef};
use serde_json::{Map, Value as J, json};
use std::collections::HashMap;

#[path = "lif/describe.rs"]
mod describe;
#[path = "lif/plan.rs"]
pub mod plan;
#[path = "lif/stored.rs"]
mod stored;

/// The header block's fixed part: test value, length, magic byte, XML characters.
const HEAD: u64 = 13;
const HEAD_TYPE: &str = "{test:<u4,length:<u4,magic:u1,characters:<u4}";
/// A memory block's fixed part, by the XML's version: 32-bit (1) or 64-bit (2) data sizes.
const BLOCK_TYPES: [&str; 2] = [
    "{test:<u4,length:<u4,magic:u1,size:<u4,magic2:u1,characters:<u4}",
    "{test:<u4,length:<u4,magic:u1,size:<u8,magic2:u1,characters:<u4}",
];
/// A LOF's version fields and its memory block's fixed part (spec/zarrify/lif.md §2.5
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#25-leica-object-files>).
const OBJECT_VERSIONS: u64 = 10;
const OBJECT_VERSIONS_TYPE: &str = "{magic:u1,version:<u4,magic2:u1,version2:<u4}";
const OBJECT_BLOCK: u64 = 9;
const OBJECT_BLOCK_TYPE: &str = "{magic:u1,size:<u8}";
/// The header block's XML text in a LOF.
const OBJECT_FILE: &str = "LMS_Object_File";
/// The test value and the magic byte (spec/zarrify/lif.md §2.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#21-blocks>).
pub const TEST: u32 = 0x70;
pub const MAGIC: u8 = 0x2A;
/// The first read: the header block and, usually, its XML.
const FIRST: u64 = 1 << 16;
/// What a LOF's second read asks for, from the end of the block's data: its XML, usually whole.
const OBJECT_XML: u64 = 1 << 20;
/// The block headers a walk round asks for, and the ID bytes it reads with a header it guesses.
const PREDICT: usize = 4096;
const ID_GUESS: u64 = 256;
/// The blocks the walk reads at most (spec/zarrify/lif.md §7.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#73-rejection>).
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
pub struct Tree<'a> {
    x: &'a str,
    nodes: Vec<Node<'a>>,
    /// the first element at depth 0, and the end of its subtree (the next element at depth 0)
    root: Option<(u32, u32)>,
}

impl<'a> Tree<'a> {
    pub fn build(x: &'a str) -> Tree<'a> {
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

    /// The root: the first element at depth 0.
    pub fn root(&self) -> Option<u32> {
        self.root.map(|r| r.0)
    }

    pub fn name(&self, n: u32) -> &'a str {
        self.nodes[n as usize].tag.name
    }

    /// The first attribute `name`, references decoded.
    pub fn attr(&self, n: u32, name: &str) -> Option<String> {
        self.nodes[n as usize].tag.attr(self.x, name).map(|a| a.0)
    }

    /// Every attribute, references decoded, in order; of a repeated name the first.
    pub fn attrs(&self, n: u32) -> Map<String, J> {
        let mut out = Map::new();
        for (k, v, _) in self.nodes[n as usize].tag.attrs(self.x) {
            if !out.contains_key(&k) {
                out.insert(k, J::String(xml::decode_refs(&v)));
            }
        }
        out
    }

    pub fn children(&self, n: u32) -> impl Iterator<Item = u32> + '_ {
        let mut c = self.nodes[n as usize].first;
        std::iter::from_fn(move || {
            (c != NONE).then(|| {
                let out = c;
                c = self.nodes[c as usize].next;
                out
            })
        })
    }

    pub fn named(&self, n: u32, name: &'a str) -> impl Iterator<Item = u32> + '_ {
        self.children(n).filter(move |&c| self.name(c) == name)
    }

    pub fn child(&self, n: u32, name: &'a str) -> Option<u32> {
        self.named(n, name).next()
    }

    /// The first child along each step of `path`.
    pub fn at(&self, n: u32, path: &[&'a str]) -> Option<u32> {
        path.iter().try_fold(n, |m, step| self.child(m, step))
    }
}

/// An attribute's integer: its text, whitespace trimmed, is one or more digits of a value at most
/// 2^53 - 1.
pub fn integer(v: Option<&str>) -> Option<u64> {
    let t = v?.trim_matches([' ', '\t', '\r', '\n']);
    if t.is_empty() || !t.bytes().all(|c| c.is_ascii_digit()) || t.trim_start_matches('0').len() > 16 {
        return None;
    }
    t.parse::<u64>().ok().filter(|&x| x <= MAX_SAFE)
}

/// An integer attribute as a role's given: its value, its text when not an integer, else absent.
pub fn given(v: &Option<String>) -> crate::expr::V {
    match (v, integer(v.as_deref())) {
        (_, Some(x)) => num(x as f64),
        (Some(t), None) => text(t),
        (None, None) => crate::expr::V::Absent,
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

/// One image element, read from the XML: what its stored array uses, and what the description
/// holds of it (spec/zarrify/lif.md §5.2
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
    /// a LOF's version fields and memory block's fixed part
    Object,
    /// a LOF's XML block's fixed part
    ObjectHead,
    /// a LOF's XML
    ObjectXml,
    Done,
}

/// Which file of the family this is (spec/zarrify/lif.md §2.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#22-the-xml-header>).
#[derive(Clone, Copy, PartialEq, Debug)]
enum File {
    Lif,
    Lof,
    Lifext,
}

/// A LOF's parts that a LIF file does not have (spec/zarrify/lif.md §2.5
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#25-leica-object-files>): its
/// version fields, and its XML block, at `at`.
#[derive(Default)]
struct Object {
    versions: Vec<u8>,
    at: u64,
    head: Vec<u8>,
    chars: u64,
    xml: Vec<u8>,
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
    file: File,
    object: Object,
    /// of a LOF of a set, where its image starts in the block's data (`Lif::in_set`)
    offset: u64,
    /// the `UniqueID` of the first `Element` of its XML
    unique: Option<String>,
    /// the XML's root element's name, and whether it is a LOF's XML of an older writer, whose root
    /// is `Data` (`object_xml`)
    root: Option<String>,
    older: bool,
    /// the root's Version: of a LIF file, the width of a block's data size
    version: u64,
    predicted: Vec<Predicted>,
    next_predicted: usize,
    blocks: Vec<Block>,
    at: u64,
    tail: Option<(u64, u64)>,
    facts: J,
    /// what each image's stored array needs (`stored.rs`), with the image's index, in the images'
    /// order: of each image that has one (`reached`)
    reached: Vec<(usize, stored::Reached)>,
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
            file: File::Lif,
            object: Object::default(),
            offset: 0,
            unique: None,
            root: None,
            older: false,
            version: 2,
            predicted: Vec::new(),
            next_predicted: 0,
            blocks: Vec::new(),
            at: 0,
            tail: None,
            facts: J::Null,
            reached: Vec::new(),
            asked: sans::Asked::default(),
        }
    }

    /// A parser of a LOF that a document names, whose `Block` has the `Offset` `offset`
    /// (spec/zarrify/lif.md §8.4
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#84-the-images>).
    pub fn in_set(size: u64, offset: u64) -> Self {
        Lif { offset, ..Lif::new(size) }
    }

    /// Whether the file is a LOF, once its XML is read.
    pub fn is_lof(&self) -> bool {
        self.file == File::Lof
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
                    // a LAS X document: its input is a store run's (`crate::xlef`)
                    if self.fed.get(0, self.size.min(552)).is_some_and(crate::xlef::detect) {
                        return Ok(Step::Beside);
                    }
                    self.header()?;
                    self.phase = Phase::Xml;
                    let end = HEAD + 2 * self.chars;
                    if !self.fed.has(HEAD, 2 * self.chars) {
                        return Ok(self.asked.ask(vec![(HEAD, end - HEAD)]));
                    }
                }
                Phase::Xml => {
                    self.read_xml()?;
                    if self.file == File::Lof {
                        self.phase = Phase::Object;
                        continue;
                    }
                    self.fed.clear();
                    self.at = HEAD + 2 * self.chars;
                    self.phase = Phase::Walk;
                }
                Phase::Object => {
                    self.object_parts()?;
                    self.phase = Phase::ObjectHead;
                    let at = self.object.at;
                    if !self.fed.has(at, HEAD) {
                        return Ok(self.asked.ask(vec![(at, (self.size - at).min(OBJECT_XML))]));
                    }
                }
                Phase::ObjectHead => {
                    self.object_head()?;
                    self.phase = Phase::ObjectXml;
                    let (at, n) = (self.object.at + HEAD, 2 * self.object.chars);
                    if !self.fed.has(at, n) {
                        return Ok(self.asked.ask(vec![(at, n)]));
                    }
                }
                Phase::ObjectXml => {
                    self.object_xml()?;
                    self.fed.clear();
                    self.layout()?;
                    self.phase = Phase::Done;
                    return Ok(Step::Done);
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
        if x.as_deref() == Some(OBJECT_FILE) {
            self.file = File::Lof;
            return Ok(());
        }
        self.document(x.as_deref())
    }

    /// A LOF's parts after its header block (spec/zarrify/lif.md §2.5
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#25-leica-object-files>): its
    /// version fields, its memory block's fixed part and where its XML block is. The memory block
    /// is the file's one, whatever its XML says of it.
    fn object_parts(&mut self) -> Res<()> {
        let start = HEAD + 2 * self.chars;
        let (versions, block) = (start + OBJECT_VERSIONS, start + OBJECT_VERSIONS + OBJECT_BLOCK);
        let h = self.fed.get(start, OBJECT_VERSIONS + OBJECT_BLOCK).map(|h| h.to_vec());
        let byte = |at: usize| h.as_ref().map_or(0, |h| h[at]);
        let data_size = h.as_ref().map_or(0, |h| u64::le_at(h, OBJECT_VERSIONS as usize + 1));
        let end = block as u128 + data_size as u128 + HEAD as u128;
        self.rules.check(
            "object",
            rules::env([
                ("parts", num(block as f64)),
                ("size", num(self.size as f64)),
                ("magic", num(byte(0))),
                ("magic2", num(byte(5))),
                ("magic3", num(byte(OBJECT_VERSIONS as usize))),
                ("data", num(data_size as f64)),
                ("xml_at", num(end as f64)),
            ]),
        )?;
        let h = h.ok_or("internal: no object parts")?;
        self.object.versions = h[..OBJECT_VERSIONS as usize].to_vec();
        self.blocks.push(Block { start: versions, fields: h[OBJECT_VERSIONS as usize..].to_vec(), id: Vec::new(), size: data_size, image: None });
        self.object.at = block + data_size;
        Ok(())
    }

    /// A LOF's XML block's fixed part.
    fn object_head(&mut self) -> Res<()> {
        let h = self.fed.get(self.object.at, HEAD).ok_or("internal: no XML block")?.to_vec();
        let chars = u32::le_at(&h, 9) as u64;
        self.rules.check(
            "object_xml",
            rules::env([
                ("test", num(u32::le_at(&h, 0))),
                ("magic", num(h[8])),
                ("chars", num(chars as f64)),
                ("at", num(self.object.at as f64)),
                ("size", num(self.size as f64)),
            ]),
        )?;
        self.object.chars = chars;
        self.object.head = h;
        Ok(())
    }

    /// A LOF's XML: LAS X's, as a LIF file's XML header, or an older writer's, whose root is the
    /// image element's `Data` and is read as if it were that of `LMSDataContainerHeader` and an
    /// `Element` that has no attributes (spec/zarrify/lif.md §2.5
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#25-leica-object-files>).
    fn object_xml(&mut self) -> Res<()> {
        let n = 2 * self.object.chars;
        let xml = self.fed.get(self.object.at + HEAD, n).ok_or("internal: no XML")?;
        let units: Vec<u16> = xml.chunks_exact(2).map(|c| u16::le_at(c, 0)).collect();
        if n <= SHOWN {
            self.object.xml = xml.to_vec();
        }
        let x = String::from_utf16(&units).ok();
        self.older = x.as_deref().is_some_and(|x| x.starts_with("<Data>"));
        let x = x.map(|x| {
            if x.starts_with("<Data>") {
                format!("<LMSDataContainerHeader Version=\"2\"><Element>{x}</Element></LMSDataContainerHeader>")
            } else {
                x
            }
        });
        self.document(x.as_deref())
    }

    /// The XML of the file: its root and version, the `Memory` elements the walk predicts, and
    /// its images.
    fn document(&mut self, x: Option<&str>) -> Res<()> {
        let tree = x.map(Tree::build);
        let root = tree.as_ref().and_then(|t| t.root.map(|r| t.name(r.0).to_string()));
        let version = tree.as_ref().and_then(|t| t.root.and_then(|r| t.attr(r.0, "Version")));
        self.rules.check(
            "xml",
            rules::env([
                ("utf16", flag(x.is_some())),
                ("root", root.as_deref().map_or(crate::expr::V::Absent, text)),
                ("version", given(&version)),
            ]),
        )?;
        self.version = integer(version.as_deref()).unwrap_or(2);
        self.root = root.clone();
        if root.as_deref() == Some("LMSDataContainerEnhancedHeader") {
            self.file = File::Lifext;
        }
        let tree = tree.ok_or("internal: no XML tree")?;
        self.unique = tree.root().and_then(|r| tree.child(r, "Element")).and_then(|e| tree.attr(e, "UniqueID"));
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

    /// A memory block's data size is 32 bits wide only in a LIF file of version 1 (spec/zarrify/lif.md
    /// §2.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#21-blocks>): a LIFEXT
    /// file of `Version` 1 has 64-bit sizes.
    fn narrow(&self) -> bool {
        self.version == 1 && self.file == File::Lif
    }

    fn fixed(&self) -> u64 {
        if self.narrow() { 18 } else { 22 }
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
            let size = if self.narrow() { u32::le_at(&h, 9) as u64 } else { u64::le_at(&h, 9) };
            let w = if self.narrow() { 4 } else { 8 };
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

    /// The images, the memory block each names, and what each one's stored array needs, then the
    /// IR and the description. The images' checks and layout are refkv's plan's (`plan.rs`).
    fn layout(&mut self) -> Res<()> {
        let images = std::mem::take(&mut self.images);
        let mut by_id: HashMap<Vec<u8>, usize> = HashMap::new();
        for (i, b) in self.blocks.iter().enumerate() {
            by_id.entry(b.id.clone()).or_insert(i);
        }
        if self.file == File::Lof {
            self.rules.check("object_images", rules::env([("count", num(images.len() as f64))]))?;
        }
        let mut described = Vec::with_capacity(images.len());
        for (k, im) in images.iter().enumerate() {
            // a LOF's one block has no ID: its XML's MemoryBlockID, if it has one, names nothing
            let block = if self.file == File::Lof {
                Some(0)
            } else {
                im.memory.as_ref().and_then(|id| {
                    let key: Vec<u8> = id.encode_utf16().flat_map(|u| u.to_le_bytes()).collect();
                    by_id.get(&key).copied()
                })
            };
            // the block's data is that of the first image that names it
            if let Some(b) = block.filter(|&b| self.blocks[b].image.is_none()) {
                self.blocks[b].image = Some(k);
                if let Some(r) = reached(im, b, self.offset) {
                    self.reached.push((k, r));
                }
            }
            described.push(describe::image(im, block));
        }
        self.emit()?;
        let mut facts = self.describe(described);
        self.asked.put(&mut facts);
        self.facts = facts;
        Ok(())
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
        let ty = if self.file == File::Lof { OBJECT_BLOCK_TYPE } else { BLOCK_TYPES[!self.narrow() as usize] };
        if self.file == File::Lof {
            let o = self.object.versions.as_slice();
            self.ir.value(0, "versions", NO_INDEX, OBJECT_VERSIONS_TYPE, 0, (HEAD + xml_len, OBJECT_VERSIONS), Some(o))?;
        }
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
        if self.file == File::Lof {
            let (at, n) = (self.object.at, 2 * self.object.chars);
            let x = self.ir.struct_(0, "description", NO_INDEX, 0, Some((at, HEAD + n)))?;
            self.ir.value(x, "fields", NO_INDEX, HEAD_TYPE, 0, (at, HEAD), Some(&self.object.head))?;
            if n > 0 {
                let shown = (n <= SHOWN).then_some(self.object.xml.as_slice());
                self.ir.value(x, "xml", NO_INDEX, &format!("utf16[{n}]"), 0, (at + HEAD, n), shown)?;
            }
            let end = at + HEAD + n;
            if end < self.size {
                self.tail = Some((end, self.size - end));
            }
        }
        if let Some((o, n)) = self.tail {
            self.ir.value(0, "tail", NO_INDEX, &format!("bytes[{n}]"), 0, (o, n), None)?;
        }
        Ok(())
    }

    /// The finished IR, with its images described (`stored.rs`), and the description, each image's
    /// with `declared_by`, the key of its stored array: the element of the XML that declares it, and
    /// its index among the images that XML declares.
    pub fn finish(self) -> Result<(Ir, J), String> {
        let (mut ir, mut facts) = sans::finish(self.phase == Phase::Done, self.ir, self.facts)?;
        let Some(images) = facts["images"].as_array_mut().filter(|i| !i.is_empty()) else {
            return Ok((ir, facts));
        };
        let key = stored::declaring(&ir).ok_or("internal: no element holds the XML of the images")?;
        for (k, im) in images.iter_mut().enumerate() {
            im["declared_by"] = json!([key, k]);
        }
        // the chunk map's limit, of the file (spec/stored-array.md §6
        // <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#6-in-the-ir>)
        let limit = (1u64 << 22).saturating_add(self.size.saturating_mul(4));
        let (arrays, chains, map) = stored::described(&ir, key, &self.reached, limit)?;
        ir.describe(arrays, chains, map)?;
        Ok((ir, facts))
    }
}

crate::parser!(Lif);

/// What an image's stored array needs (`stored::Reached`), of an image whose dimensions'
/// `NumberOfElements` (each at least 1) and `BytesInc`, and channels' `Resolution` (1 to 64 bits)
/// and `BytesInc`, are integers, with at least one of each; its sample is the first channel's
/// size. None of another, which has no stored array, and which refkv's plan rejects.
fn reached(im: &Image, block: usize, start: u64) -> Option<stored::Reached> {
    let dims: Vec<(u64, u64)> = im
        .dims
        .iter()
        .map(|d| Some((integer(d[1].as_deref()).filter(|&n| n >= 1)?, integer(d[2].as_deref())?)))
        .collect::<Option<_>>()?;
    let channels: Vec<(u64, u64)> = im
        .channels
        .iter()
        .map(|c| Some((integer(c[1].as_deref()).filter(|r| (1..=64).contains(r))?, integer(c[2].as_deref())?)))
        .collect::<Option<_>>()?;
    let resolution = channels.first()?.0;
    if dims.is_empty() {
        return None;
    }
    let mut offsets: Vec<u64> = channels.iter().map(|c| c.1).collect();
    offsets.sort_unstable();
    Some(stored::Reached { dims, offsets, item: plan::item(resolution), block, start })
}

/// An image element's attributes the layout reads (spec/zarrify/lif.md §2.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#23-images>).
fn read_image(t: &Tree, n: u32) -> Image {
    let desc = t.at(n, &["Data", "Image", "ImageDescription"]);
    let list = |name, item| -> Vec<u32> {
        desc.and_then(|d| t.child(d, name)).map(|l| t.named(l, item).collect()).unwrap_or_default()
    };
    let (dims, channels) = (list("Dimensions", "DimensionDescription"), list("Channels", "ChannelDescription"));
    let mut path = Vec::new();
    // in a LIFEXT file, the memory block in the parent LIF of the image this image is of
    // (spec/zarrify/lif.md §2.6 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#26-lifext-files>)
    let mut parent = None;
    let mut e = n;
    while e != NONE {
        if t.name(e) == "Element" {
            path.push(t.attr(e, "Name").map_or(J::Null, J::String));
        } else if t.name(e) == "ChildrenOf" {
            parent = t.attr(e, "MemoryBlockID");
        }
        e = t.nodes[e as usize].parent;
    }
    path.reverse();
    let memory = t.child(n, "Memory");
    let mut meta = Map::new();
    meta.insert("path".into(), J::Array(path));
    if let Some(parent) = parent {
        meta.insert("parent".into(), J::String(parent));
    }
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

/// The LIF profile's file run (spec/zarrify/lif.md).
pub struct Profile;

impl crate::run::FileProfile for Profile {
    /// The test value at byte 0 and the magic byte at byte 8 (`detect`), or a LAS X document
    /// (`crate::xlef::detect`), whose file run finds that it is one and starts over as a store run.
    fn detect(&self, head: &[u8]) -> bool {
        detect(head) || crate::xlef::detect(head)
    }
    fn parser(&self, size: u64) -> crate::run::AnyParser {
        crate::run::AnyParser::of::<Lif>(size)
    }
    fn project(&self, ir: &crate::ir::Ir, facts: &serde_json::Value, url: &str, _copied: &crate::project::Copied) -> Result<crate::out::Out, String> {
        crate::project::lif::project(ir, facts, url)
    }
    /// The projection reads nothing; refkv's plan is made here, so that a run rejects an input the
    /// plan rejects before it is done (`plan::plan`).
    fn reads(&self, _ir: &crate::ir::Ir, facts: &serde_json::Value) -> Result<Vec<(u64, u64)>, String> {
        plan::plan(facts).map(|_| Vec::new())
    }
    /// The parser returns a description of the file (spec/zarrify/lif.md §9
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#9-the-description>).
    fn described(&self) -> bool {
        true
    }
    /// A LAS X project's store run returns a description of its documents and LOFs
    /// (spec/zarrify/lif.md §9 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#9-the-description>).
    fn store_described(&self) -> bool {
        true
    }
}

/// The parser of the profile's store run (`StoreFormat`; crate::store).
pub fn store_parser(ir: crate::ir::Ir, objects: Vec<u32>, keys: &[(String, u64)]) -> crate::ir::Res<Box<dyn crate::store::StoreParser>> {
    Ok(Box::new(crate::xlef::Xlef::new(ir, objects, keys)?))
}
