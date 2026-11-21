//! A LAS X project across files (spec/zarrify/lif.md §8
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#8-las-x-documents>): the store run
//! of the `lif` format, which a file run starts over as when its bytes are an XLEF, XLCF, XLIF or
//! XLLF document's (spec/zarrify.md §1.9
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#19-sources-beside-the-input>).
//!
//! **Batches.** The documents, level by level: those of a level are read whole in one batch and
//! name those of the next, which one batch of probes finds. Then the LOFs the documents name, each
//! driven by a parser of its own (`crate::lif::Lif`, of the file run's kind), a batch at a time:
//! each one's first 64 KiB, then its XML. Then the values the view shows that the parsers did not
//! keep. Each LOF's file IR joins the store IR under the LOF's object (`Ir::graft`), a document
//! is held, and the run returns a description of the documents and the LOFs. The output's images,
//! found by walking the documents in the order they name one another, are refkv's plan's
//! (`crate::lif::plan::plan_set`).

use crate::beside::{self, Written};
use crate::expr::V;
use crate::ir::{Ir, Res, NO_INDEX};
use crate::lif::{self, Lif, Tree};
use crate::nd2::Step;
use crate::rules::{self, Rules, flag, num, text};
use crate::run::unread_shown;
use crate::store::{StoreParser, StoreStep};
use serde_json::{Map, Value as J, json};
use std::collections::{HashMap, HashSet};

/// How LAS X writes a name (spec/zarrify/lif.md §8.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#82-the-siblings>).
const LEICA: Written = Written { percent: true, windows: true, parents: true };
/// The UTF-8 byte order mark LAS X starts a document with.
const BOM: &[u8] = b"\xEF\xBB\xBF";
/// The root the signature looks for.
const ROOT: &[u8] = b"LMSDataContainerHeader";

/// Whether a file's first bytes are a LAS X document's (spec/zarrify/lif.md §7.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#71-detection>): a UTF-8 byte order
/// mark or `<?xml`, and `LMSDataContainerHeader` within them.
pub fn detect(head: &[u8]) -> bool {
    (head.starts_with(BOM) || head.starts_with(b"<?xml")) && head.windows(ROOT.len()).any(|w| w == ROOT)
}

/// What a name in a document names.
#[derive(Clone, Copy, PartialEq, Debug)]
enum Role {
    Document,
    Lof,
}

impl Role {
    fn noun(self) -> &'static str {
        match self {
            Role::Document => "document",
            Role::Lof => "LOF",
        }
    }
}

/// A child of a `Memory` element that names a file.
struct Entry {
    tag: String,
    file: String,
    offset: Option<String>,
    size: Option<String>,
}

impl Entry {
    /// Whether it is a `Block` of a LOF.
    fn names_lof(&self) -> bool {
        self.tag == "Block" && self.file.to_ascii_lowercase().ends_with(".lof")
    }
}

/// What a document says: its element's attributes, whether it is an image, its `Memory`, and the
/// keys of the documents it names, each with the `UUID` that names it.
struct Document {
    key: String,
    name: J,
    attributes: Map<String, J>,
    image: bool,
    memory: Option<Map<String, J>>,
    entries: Vec<Entry>,
    /// the written key of the document a `Reference` names, its UUID
    references: Vec<(String, Option<String>)>,
}

/// A LOF a document names.
struct Named {
    /// the object's index in the listing
    index: usize,
    key: String,
    parser: Lif,
    done: bool,
    /// its index in `Xlef::docs`: the document that names it
    by: usize,
}

/// A name a document's reads added: its written key, what it names, its level and its document.
struct Wanted {
    key: String,
    role: Role,
    level: u32,
    by: usize,
}

#[derive(PartialEq, Clone, Copy)]
enum Phase {
    /// reading a level of documents, or their names to probe
    Documents,
    /// the LOFs, to where their parsers are done
    Lofs,
    /// the values the view shows that the parsers did not keep
    Fill,
    Done,
}

pub struct Xlef {
    ir: Option<Ir>,
    rules: &'static Rules,
    tree: beside::Tree,
    /// each object's key, size and element, by its index in the listing
    keys: Vec<(String, u64)>,
    elems: Vec<u32>,
    object: HashMap<String, usize>,
    /// a written key the host found at another key (the section's **Case**)
    written: HashMap<String, String>,
    phase: Phase,
    /// the documents to read in the next batch, and each one's level
    unread: Vec<(usize, u32)>,
    /// a batch of documents being read, and their bytes
    reading: Vec<(usize, u32)>,
    got: HashMap<usize, Vec<u8>>,
    wanted: Vec<Wanted>,
    probing: bool,
    /// the bytes of the documents so far
    spent: u64,
    roles: HashMap<usize, Role>,
    /// the named document's version
    version: u64,
    docs: Vec<Document>,
    doc_of: HashMap<usize, usize>,
    lofs: Vec<Named>,
    lof_of: HashMap<usize, usize>,
    /// the parsers' (object's index, by, IR, description), in `lofs`' order
    parsed: Vec<(usize, usize, Ir, J)>,
    /// the values to fill: (index in `parsed`, element, offset, length)
    fill: Vec<(usize, u32, u64, u64)>,
    finished: Option<(Ir, J)>,
}

fn dir_of(key: &str) -> &str {
    key.rfind('/').map_or("", |k| &key[..k])
}

/// The rejection of documents that name documents too deep (spec/zarrify/lif.md §8.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#83-limits>).
pub(crate) fn depth() -> String {
    format!("budget: documents name documents more than {} levels deep", beside::MAX_DEPTH)
}

impl Xlef {
    /// The listing's IR, its objects' elements and keys: one key, the named file.
    pub fn new(ir: Ir, objects: Vec<u32>, keys: &[(String, u64)]) -> Res<Xlef> {
        let [(key, _)] = keys else {
            return Err(format!("a LAS X run starts from the named file alone, not {} objects", keys.len()));
        };
        let mut x = Xlef {
            ir: Some(ir),
            rules: rules::of("lif"),
            tree: beside::Tree::default(),
            keys: keys.to_vec(),
            elems: objects,
            object: HashMap::from([(key.clone(), 0)]),
            written: HashMap::new(),
            phase: Phase::Documents,
            unread: Vec::new(),
            reading: Vec::new(),
            got: HashMap::new(),
            wanted: Vec::new(),
            probing: false,
            spent: 0,
            roles: HashMap::from([(0, Role::Document)]),
            version: 2,
            docs: Vec::new(),
            doc_of: HashMap::new(),
            lofs: Vec::new(),
            lof_of: HashMap::new(),
            parsed: Vec::new(),
            fill: Vec::new(),
            finished: None,
        };
        x.charge(0)?;
        x.unread.push((0, 1));
        Ok(x)
    }

    /// A document's size against the limits on what is read whole (spec/zarrify/lif.md §8.3
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#83-limits>).
    fn charge(&mut self, index: usize) -> Res<()> {
        let (key, size) = &self.keys[index];
        self.spent = self.spent.saturating_add(*size);
        self.rules.check(
            "document_size",
            rules::env([("key", text(key)), ("size", num(*size as f64)), ("total", num(self.spent as f64))]),
        )
    }

    /// The document `index`, read whole: what it says, and the names it adds to `wanted`.
    fn read_document(&mut self, index: usize, level: u32, bytes: &[u8]) -> Res<()> {
        let key = self.keys[index].0.clone();
        let body = bytes.strip_prefix(BOM).unwrap_or(bytes);
        let xml = std::str::from_utf8(body).ok();
        let tree = xml.map(Tree::build);
        let root = tree.as_ref().and_then(|t| t.root());
        let root_name = tree.as_ref().zip(root).map(|(t, r)| t.name(r).to_string());
        let version = tree.as_ref().zip(root).and_then(|(t, r)| t.attr(r, "Version"));
        let element = tree.as_ref().zip(root).and_then(|(t, r)| t.child(r, "Element"));
        self.rules.check(
            "document",
            rules::env([
                ("key", text(&key)),
                ("utf8", flag(xml.is_some())),
                ("root", root_name.as_deref().map_or(V::Absent, text)),
                ("version", lif::given(&version)),
                ("element", flag(element.is_some())),
            ]),
        )?;
        if index == 0 {
            self.version = lif::integer(version.as_deref()).unwrap_or(2);
        }
        let (tree, element) = (tree.ok_or("internal: no XML tree")?, element.ok_or("internal: no element")?);
        let memory = tree.child(element, "Memory");
        let entries: Vec<Entry> = memory.map_or(Vec::new(), |m| {
            tree.children(m)
                .filter_map(|c| {
                    tree.attr(c, "File").map(|file| Entry {
                        tag: tree.name(c).to_string(),
                        file,
                        offset: tree.attr(c, "Offset"),
                        size: tree.attr(c, "Size"),
                    })
                })
                .collect()
        });
        let at = self.docs.len();
        let rules = self.rules;
        let dir = dir_of(&key);
        let written = |file: &str, what: &str| -> Res<String> {
            let k = beside::written_key(file.as_bytes(), dir, LEICA);
            rules.check(
                "name",
                rules::env([("name", text(file)), ("what", text(what)), ("key", text(&key)), ("below", flag(k.is_some()))]),
            )?;
            k.ok_or_else(|| "internal: no key".to_string())
        };
        let lof = match entries.as_slice() {
            [e] if e.names_lof() => {
                rules.check(
                    "block",
                    rules::env([("key", text(&key)), ("offset", lif::given(&e.offset)), ("size", lif::given(&e.size))]),
                )?;
                Some(written(&e.file, "Block")?)
            }
            _ => None,
        };
        let mut references = Vec::new();
        if let Some(children) = tree.child(element, "Children") {
            for r in tree.named(children, "Reference") {
                if let Some(file) = tree.attr(r, "File") {
                    references.push((written(&file, "Reference")?, tree.attr(r, "UUID")));
                }
            }
        }
        for (k, _) in &references {
            self.wanted.push(Wanted { key: k.clone(), role: Role::Document, level: level + 1, by: at });
        }
        if let Some(k) = &lof {
            self.wanted.push(Wanted { key: k.clone(), role: Role::Lof, level, by: at });
        }
        self.doc_of.insert(index, at);
        self.docs.push(Document {
            name: tree.attr(element, "Name").map_or(J::Null, J::String),
            attributes: tree.attrs(element),
            image: tree.at(element, &["Data", "Image"]).is_some(),
            memory: memory.map(|m| tree.attrs(m)),
            entries,
            references,
            key,
        });
        Ok(())
    }

    /// The object a written key names now, if any.
    fn known(&self, key: &str) -> Option<usize> {
        self.object.get(self.written.get(key).map_or(key, String::as_str)).copied()
    }

    /// The keys of `wanted` no object answers yet, each once, in order.
    fn to_probe(&self) -> Res<Vec<String>> {
        let mut seen = HashSet::new();
        let mut out = Vec::new();
        for w in &self.wanted {
            if self.known(&w.key).is_none() && seen.insert(w.key.as_str()) {
                if w.role == Role::Document && w.level > beside::MAX_DEPTH {
                    return Err(depth());
                }
                out.push(w.key.clone());
            }
        }
        Ok(out)
    }

    /// `wanted`, all known or found: the documents to read next, and the LOFs' parsers.
    fn settle(&mut self) -> Res<()> {
        for w in std::mem::take(&mut self.wanted) {
            let index = self.known(&w.key).ok_or("internal: a wanted key is no object")?;
            let key = self.keys[index].0.clone();
            let before = self.roles.insert(index, w.role);
            if before.is_some_and(|r| r != w.role) {
                self.rules.check("role", rules::env([("key", text(&key)), ("one", flag(false))]))?;
            }
            match w.role {
                Role::Document if before.is_none() => {
                    if w.level > beside::MAX_DEPTH {
                        return Err(depth());
                    }
                    self.charge(index)?;
                    self.unread.push((index, w.level));
                }
                Role::Document => {}
                Role::Lof => {
                    if before.is_some() {
                        // a LOF has one block, which one element has
                        let a = &self.docs[self.lofs[self.lof_of[&index]].by].key;
                        let b = &self.docs[w.by].key;
                        self.rules.check(
                            "named_twice",
                            rules::env([("key", text(&key)), ("a", text(a)), ("b", text(b)), ("once", flag(false))]),
                        )?;
                    }
                    let entry = &self.docs[w.by].entries[0];
                    let offset = lif::integer(entry.offset.as_deref()).unwrap_or(0);
                    let size = self.keys[index].1;
                    self.lof_of.insert(index, self.lofs.len());
                    self.lofs.push(Named { index, key, parser: Lif::in_set(size, offset), done: false, by: w.by });
                }
            }
        }
        Ok(())
    }

    /// The next batch of the LOFs' reads: a step of each that has one.
    fn lof_reads(&mut self) -> Res<Vec<(usize, u64, u64)>> {
        let mut asked = Vec::new();
        for f in &mut self.lofs {
            while !f.done {
                match f.parser.step().map_err(|m| format!("the LOF {:?}: {m}", f.key))? {
                    Step::Read(r) => {
                        asked.extend(r.into_iter().map(|(o, n)| (f.index, o, n)));
                        break;
                    }
                    // a document's bytes, not a LOF's
                    Step::Beside => self.rules.check("lof_file", rules::env([("key", text(&f.key)), ("lof", flag(false))]))?,
                    Step::Done => f.done = true,
                }
            }
        }
        Ok(asked)
    }

    /// The parsers' IRs and descriptions, each checked against the document that names it; the
    /// reads of what the view shows that the parsers did not keep.
    fn finish_parsers(&mut self) -> Res<Vec<(usize, u64, u64)>> {
        let mut asked = Vec::new();
        for f in std::mem::take(&mut self.lofs) {
            let Named { index, key, parser, by, .. } = f;
            self.rules.check("lof_file", rules::env([("key", text(&key)), ("lof", flag(parser.is_lof()))]))?;
            let (ir, mut description) = parser.finish()?;
            if let Some(m) = description.as_object_mut() {
                for count in ["requests", "requested", "rounds"] {
                    m.remove(count);
                }
            }
            self.check_lof(&self.docs[by], &key, &description)?;
            let at = self.parsed.len();
            for (e, o, n) in unread_shown(&ir, "lif") {
                self.fill.push((at, e, o, n));
                asked.push((index, o, n));
            }
            self.parsed.push((index, by, ir, description));
        }
        asked.sort_unstable();
        asked.dedup();
        Ok(asked)
    }

    /// A LOF against the `Block` that names it: that it has an image where the document is one,
    /// and its `UniqueID`. Whether the `Block`'s range fits the LOF's block, and the image the
    /// range, is refkv's plan's (`lif::plan::plan_set`), after the image's own checks.
    fn check_lof(&self, doc: &Document, key: &str, description: &J) -> Res<()> {
        let images = description["images"].as_array().map_or(0, Vec::len);
        self.rules.check(
            "lof_image",
            rules::env([("document", text(&doc.key)), ("key", text(key)), ("has", flag(!doc.image || images > 0))]),
        )?;
        let unique = doc.attributes.get("UniqueID").and_then(J::as_str);
        let theirs = description["unique_id"].as_str();
        self.rules.check(
            "identity",
            rules::env([
                ("noun", text("LOF")),
                ("key", text(key)),
                ("same", flag(unique.is_none() || theirs.is_none() || unique == theirs)),
            ]),
        )
    }

    /// Each `Reference` against the document it names (the section's **Identity**).
    fn check_references(&self) -> Res<()> {
        for d in &self.docs {
            for (k, uuid) in &d.references {
                let index = self.known(k).ok_or("internal: a reference names no object")?;
                let target = &self.docs[self.doc_of[&index]];
                let unique = target.attributes.get("UniqueID").and_then(J::as_str);
                self.rules.check(
                    "identity",
                    rules::env([
                        ("noun", text("document")),
                        ("key", text(&target.key)),
                        ("same", flag(unique.is_none() || uuid.is_none() || unique == uuid.as_deref())),
                    ]),
                )?;
            }
        }
        Ok(())
    }

    /// The documents and the LOFs' IRs, joined into the store IR, and the description of the
    /// project (spec/zarrify/lif.md §9
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#9-the-description>): its
    /// documents, in the order they were read, and its LOFs, each with its own description. Which
    /// images the output presents, in the order the documents name one another, is refkv's plan's
    /// (`lif::plan::plan_set`).
    fn join(&mut self) -> Res<()> {
        let mut ir = self.ir.take().ok_or("internal: no IR")?;
        // a document is held, by the bytes the run keeps; a LOF is described in place
        for d in &self.docs {
            let index = self.object[&d.key];
            ir.value(self.elems[index], "xml", NO_INDEX, "xml", 0, (0, self.keys[index].1), None)?;
        }
        let mut lofs = Vec::with_capacity(self.parsed.len());
        let mut lof_of: HashMap<usize, usize> = HashMap::new();
        for (index, by, mut file, mut description) in std::mem::take(&mut self.parsed) {
            file.sort_values();
            let e = self.elems[index];
            ir.describe_in_place(e)?;
            let at = ir.graft(&file, e)?;
            // `declared_by`: the key of each image's stored array, the LOF's XML, by its id in the
            // store (spec/stored-array.md §2.1
            // <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#21-keys>)
            for im in description["images"].as_array_mut().into_iter().flatten() {
                let key = im["declared_by"][0].as_u64().and_then(|k| at.get(k as usize));
                im["declared_by"][0] = json!(key.ok_or("internal: a LOF's image names no element of its XML")?);
            }
            lof_of.insert(by, lofs.len());
            let (key, size) = &self.keys[index];
            lofs.push(json!({"key": key, "size": size, "object": e, "document": by, "description": description}));
        }
        let mut documents = Vec::with_capacity(self.docs.len());
        for (d, doc) in self.docs.iter().enumerate() {
            let mut references = Vec::with_capacity(doc.references.len());
            for (k, uuid) in &doc.references {
                let index = self.known(k).ok_or("internal: a reference names no object")?;
                references.push(json!({"key": self.keys[index].0, "uuid": uuid, "document": self.doc_of[&index]}));
            }
            let entries: Vec<J> =
                doc.entries.iter().map(|e| json!({"tag": e.tag, "file": e.file, "offset": e.offset, "size": e.size})).collect();
            documents.push(json!({
                "key": doc.key, "name": doc.name, "attributes": doc.attributes, "image": doc.image, "memory": doc.memory,
                "entries": entries, "references": references, "lof": lof_of.get(&d),
            }));
        }
        let files: Vec<&str> = self.keys[1..].iter().map(|k| k.0.as_str()).collect();
        let description = json!({
            "version": self.version,
            "named_key": self.keys[0].0,
            "files": files,
            "documents": documents,
            "lofs": lofs,
        });
        self.finished = Some(crate::sans::finish(true, ir, description)?);
        Ok(())
    }
}

impl StoreParser for Xlef {
    fn step(&mut self) -> Res<StoreStep> {
        loop {
            match self.phase {
                Phase::Documents => {
                    for (index, level) in std::mem::take(&mut self.reading) {
                        let bytes = self.got.remove(&index).ok_or("internal: a document was not read")?;
                        self.read_document(index, level, &bytes)?;
                    }
                    if self.probing {
                        self.probing = false;
                        self.settle()?;
                    } else if !self.wanted.is_empty() {
                        let keys = self.to_probe()?;
                        if !keys.is_empty() {
                            self.probing = true;
                            return Ok(StoreStep::Probe(keys));
                        }
                        self.settle()?;
                    }
                    if !self.unread.is_empty() {
                        self.reading = std::mem::take(&mut self.unread);
                        return Ok(StoreStep::Read(self.reading.iter().map(|&(i, _)| (i, 0, self.keys[i].1)).collect()));
                    }
                    self.check_references()?;
                    self.phase = Phase::Lofs;
                }
                Phase::Lofs => {
                    let asked = self.lof_reads()?;
                    if !asked.is_empty() {
                        return Ok(StoreStep::Read(asked));
                    }
                    self.phase = Phase::Fill;
                    let asked = self.finish_parsers()?;
                    if !asked.is_empty() {
                        return Ok(StoreStep::Read(asked));
                    }
                }
                Phase::Fill => {
                    self.join()?;
                    self.phase = Phase::Done;
                    return Ok(StoreStep::Done);
                }
                Phase::Done => return Ok(StoreStep::Done),
            }
        }
    }

    fn feed(&mut self, object: usize, offset: u64, data: &[u8]) {
        match self.phase {
            Phase::Documents => {
                self.got.insert(object, data.to_vec());
            }
            Phase::Lofs => {
                if let Some(&k) = self.lof_of.get(&object) {
                    self.lofs[k].parser.feed(offset, data.to_vec());
                }
            }
            Phase::Fill => {
                for &(at, i, o, n) in &self.fill {
                    if self.parsed[at].0 == object && o == offset && n == data.len() as u64 {
                        self.parsed[at].2.put_value(i, data);
                    }
                }
            }
            Phase::Done => {}
        }
    }

    fn resolved(&mut self, written: &str, key: &str) -> Res<()> {
        self.written.insert(written.to_string(), key.to_string());
        Ok(())
    }

    fn found(&mut self, key: &str, index: usize, size: Option<u64>) -> Res<Option<u32>> {
        let Some(size) = size else {
            let role = self.wanted.iter().find(|w| w.key == key).map_or(Role::Document, |w| w.role);
            self.rules.check("absent", rules::env([("noun", text(role.noun())), ("key", text(key)), ("found", flag(false))]))?;
            return Ok(None);
        };
        if index != self.keys.len() {
            return Err("internal: a found object out of order".into());
        }
        let ir = self.ir.as_mut().ok_or("internal: no IR")?;
        let e = self.tree.object(ir, key, size)?;
        self.keys.push((key.to_string(), size));
        self.elems.push(e);
        self.object.insert(key.to_string(), index);
        Ok(Some(e))
    }

    fn finish(mut self: Box<Self>) -> Res<(Ir, J)> {
        if self.phase != Phase::Done {
            return Err("internal: the parse is not done".into());
        }
        self.finished.take().ok_or_else(|| "internal: the documents were not joined".to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_signature_is_a_document_of_las_x() {
        let cases: &[(&[u8], bool)] = &[
            (b"\xEF\xBB\xBF<?xml version=\"1.0\"?>\r\n<LMSDataContainerHeader Version=\"2\">", true),
            (b"<?xml version=\"1.0\"?><LMSDataContainerHeader Version=\"2\">", true),
            (b"\xEF\xBB\xBF<LMSDataContainerHeader Version=\"2\">", true),
            (b"<LMSDataContainerHeader Version=\"2\">", false),
            (b"<?xml version=\"1.0\"?><OME>", false),
            (b"p\0\0\0\0\0\0\0*\0\0\0\0<LMSDataContainerHeader", false),
        ];
        for (head, want) in cases {
            assert_eq!(detect(head), *want, "{}", String::from_utf8_lossy(head));
        }
    }
}
