//! The N5 store format on the IR (spec/zarrify/n5.md
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/n5.md>, its §5
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/n5.md#5-source-metadata>; conventions
//! §8.9 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#89-stores>), sans-IO: from
//! the listing's IR, it reads the candidates' `attributes.json` (spec/zarrify/n5.md §2
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/n5.md#2-nodes>) from the root down,
//! a round at a time: each round is every candidate not read yet whose candidates above it are
//! read and are groups (a candidate inside a dataset is never read), so a container is read in
//! as many rounds as its candidates nest. Each document is read by spec/zarrify.md §1.6
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#16-json-documents> (`json.rs`)
//! and checked by the schema (`schema/n5.json`: the roles `candidate`, `document` and
//! `dataset`); each is held, one value per top-level member (`ascii[n]`, the value's JSON
//! text), the rest gaps (`json::hold`: a document of more than 4096 members is one value `json`).
//! Every other object stays referenced. The facts are the nodes, for the
//! projection (`project/n5.rs`), which reads the documents back from the IR.

use crate::ir::{Ir, Res};
use crate::rules::{self, num, text, Vars};
use crate::json::{self, Doc, Jv};
use crate::store::{StoreParser, StoreStep};
use serde_json::{json, Value as J};
use std::collections::HashMap;

const DOCUMENT: &str = "attributes.json";

pub static N5_SCHEMA: &str = include_str!("../schema/n5.json");

/// The N5 source model (`schema/n5.json`).
pub fn schema() -> &'static rules::Rules {
    static S: std::sync::OnceLock<rules::Rules> = std::sync::OnceLock::new();
    S.get_or_init(|| rules::Rules::new(N5_SCHEMA).expect("the N5 schema is valid"))
}

pub struct N5 {
    ir: Ir,
    objects: Vec<u32>,
    /// the candidates (directory path, listed object of its document), from the root down
    cands: Vec<(String, usize)>,
    /// each candidate's candidates just below it (those whose nearest candidate above is it)
    kids: Vec<Vec<usize>>,
    sizes: Vec<u64>,
    /// the candidates to read next: those whose nearest candidate above is a group just read
    next: Vec<usize>,
    started: bool,
    asked: Vec<usize>,
    got: HashMap<usize, Vec<u8>>,
    docs: Vec<(usize, Doc)>,
    nodes: Vec<(String, bool)>,
    error: Option<String>,
}

/// The parent directory of a path (`""` for a path of one segment).
fn parent(p: &str) -> &str {
    p.rfind('/').map_or("", |i| &p[..i])
}

fn depth(p: &str) -> usize {
    if p.is_empty() { 0 } else { p.matches('/').count() + 1 }
}

/// The document's key of a candidate.
pub fn document_key(path: &str) -> String {
    if path.is_empty() { DOCUMENT.to_string() } else { format!("{path}/{DOCUMENT}") }
}

impl N5 {
    pub fn new(ir: Ir, objects: Vec<u32>, keys: &[(String, u64)]) -> N5 {
        let mut cands: Vec<(String, usize)> = keys
            .iter()
            .enumerate()
            .filter_map(|(i, (k, _))| {
                if k == DOCUMENT {
                    Some((String::new(), i))
                } else {
                    k.strip_suffix(&format!("/{DOCUMENT}")).map(|p| (p.to_string(), i))
                }
            })
            .collect();
        cands.sort_by(|a, b| (depth(&a.0), a.0.as_bytes()).cmp(&(depth(&b.0), b.0.as_bytes())));
        // each candidate under its nearest candidate above (keys are at most a few KiB long,
        // so the walk up is short)
        let at: HashMap<&str, usize> = cands.iter().enumerate().map(|(c, x)| (x.0.as_str(), c)).collect();
        let mut kids = vec![Vec::new(); cands.len()];
        for (c, (path, _)) in cands.iter().enumerate() {
            let mut p = path.as_str();
            while !p.is_empty() {
                p = parent(p);
                if let Some(&u) = at.get(p) {
                    kids[u].push(c);
                    break;
                }
            }
        }
        let next = if cands.first().is_some_and(|c| c.0.is_empty()) { vec![0] } else { Vec::new() };
        N5 {
            ir,
            objects,
            cands,
            kids,
            sizes: keys.iter().map(|k| k.1).collect(),
            next,
            started: false,
            asked: Vec::new(),
            got: HashMap::new(),
            docs: Vec::new(),
            nodes: Vec::new(),
            error: None,
        }
    }

    /// Reads the documents of the round just served, in the order of the candidates; the
    /// candidates below each group read are the next round's (those below a dataset are not
    /// nodes, and are never read).
    fn take_round(&mut self) -> Res<()> {
        let rules = schema();
        for c in std::mem::take(&mut self.asked) {
            let (path, k) = self.cands[c].clone();
            let key = document_key(&path);
            let b = self.got.remove(&k).ok_or("internal: a document not read")?;
            let doc = json::parse(&b).map_err(|e| format!("{key}: {e}"))?;
            rules.check("document", &mut Vars(vec![("key", text(&key)), ("value", doc.value.to_v())]))?;
            let dataset = doc.value.has("dimensions");
            if dataset {
                check_dataset(if path.is_empty() { "/" } else { &path }, &doc.value)?;
            } else {
                self.next.extend_from_slice(&self.kids[c]);
            }
            self.nodes.push((path, dataset));
            self.docs.push((k, doc));
        }
        self.next.sort_unstable();
        Ok(())
    }
}

/// The schema's role `dataset` (spec/zarrify/n5.md §3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/n5.md#3-datasets>) for the document
/// `doc` of the dataset at `path`.
pub fn check_dataset(path: &str, doc: &Jv) -> Res<()> {
    let names = |v: &Jv| crate::expr::V::List(v.members().iter().map(|(k, _)| crate::expr::V::Str(k.text.clone())).collect());
    let c = compression(doc);
    let get = |k: &str| doc.get(k).map(Jv::to_v).unwrap_or(crate::expr::V::Absent);
    let mut env = Vars(vec![
        ("path", text(path)),
        ("dimensions", get("dimensions")),
        ("blockSize", get("blockSize")),
        ("dataType", get("dataType")),
        ("compressionType", get("compressionType")),
        ("members", names(doc)),
        ("c", c.as_ref().map(Jv::to_v).unwrap_or(crate::expr::V::Absent)),
        ("c_members", c.as_ref().map(names).unwrap_or(crate::expr::V::List(vec![]))),
    ]);
    schema().check("dataset", &mut env)
}

/// A dataset's compression object: its member `compression`, or `{"type": t}` for its member
/// `compressionType` `t` when it has no `compression` (N5 before 1.0.0).
pub fn compression(doc: &Jv) -> Option<Jv> {
    match doc.get("compression") {
        Some(c) => Some(c.clone()),
        None => doc.get("compressionType").map(|t| Jv::obj(vec![("type", t.clone())])),
    }
}

impl StoreParser for N5 {
    fn step(&mut self) -> Res<StoreStep> {
        if let Some(e) = self.error.take() {
            return Err(e);
        }
        if !self.started && self.next.is_empty() {
            // the host chose the profile by the root's attributes.json (spec/zarrify.md §1.4
            // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>)
            return Err(format!("not an N5 store: the root has no {DOCUMENT}"));
        }
        self.started = true;
        self.take_round()?;
        if self.next.is_empty() {
            return Ok(StoreStep::Done);
        }
        let round = std::mem::take(&mut self.next);
        let rules = schema();
        let mut reads = Vec::with_capacity(round.len());
        for &c in &round {
            let (path, k) = &self.cands[c];
            let size = self.sizes[*k];
            rules.check("candidate", &mut Vars(vec![("key", text(&document_key(path))), ("size", num(size as f64))]))?;
            reads.push((*k, 0, size));
        }
        self.asked = round;
        Ok(StoreStep::Read(reads))
    }

    fn feed(&mut self, object: usize, offset: u64, data: &[u8]) {
        if offset != 0 || data.len() as u64 != self.sizes[object] {
            self.error.get_or_insert_with(|| "internal: a document read in part".into());
            return;
        }
        self.got.insert(object, data.to_vec());
    }

    fn finish(self: Box<Self>) -> Res<(Ir, J)> {
        let N5 { mut ir, objects, docs, nodes, error, .. } = *self;
        if let Some(e) = error {
            return Err(e);
        }
        for (k, doc) in &docs {
            let o = objects[*k];
            let n = ir.len[o as usize];
            json::hold(&mut ir, o, n, &doc.members)?;
        }
        crate::check::finish(&mut ir)?;
        let nodes: Vec<J> = nodes.iter().map(|(p, d)| json!([p, if *d { "array" } else { "group" }])).collect();
        Ok((ir, json!({"nodes": nodes})))
    }
}
