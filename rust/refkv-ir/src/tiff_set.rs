//! A TIFF image across files (spec/zarrify/tiff.md §7.4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#74-several-files>): the store run
//! of the `tiff` format, which a file run starts over as when its file names siblings
//! (spec/zarrify.md §1.9
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#19-sources-beside-the-input>). Each
//! file has a parser of its own, of the file run's kind, and its own file IR, which joins the store
//! IR under the file's object once it is done (`Ir::graft`). The named file's parser decides
//! which files there are and which plane each holds.

use crate::beside::Tree;
use crate::ir::{Ir, Res};
use crate::nd2::Step;
use crate::run::unread_shown;
use crate::store::{StoreParser, StoreStep};
use crate::tiff::Tiff;
use serde_json::{json, Value as J};

#[derive(PartialEq, Clone, Copy)]
enum Phase {
    /// the named file, to where it names its siblings
    Named,
    /// the siblings, to where they wait for their planes
    Siblings,
    /// the named file, which now knows the planes
    Layout,
    /// the siblings' planes
    Planes,
    /// the values the mirror's view shows that the parsers did not keep
    Fill,
    /// the bytes the `omero` windows sample
    Samples,
    Done,
}

struct File {
    /// the object's index in the listing
    index: usize,
    parser: Tiff,
    /// the parser has nothing more to read until it is told more
    waiting: bool,
}

pub struct TiffSet {
    ir: Option<Ir>,
    tree: Tree,
    named_key: String,
    named: Option<File>,
    siblings: Vec<File>,
    keys: Vec<String>,
    elems: Vec<u32>,
    phase: Phase,
    /// the parsed files, in the listing's order: (IR, facts, tile elements, IFD elements)
    parsed: Vec<(Ir, J, Vec<Option<u32>>, Vec<u32>)>,
    fill: Vec<(usize, u32, u64, u64)>,
    /// the store IR and the facts, once the files' IRs have joined it
    finished: Option<(Ir, J)>,
    /// the virtual offset of each range the windows sample, by its (object, offset)
    sampling: Vec<(usize, u64, u64)>,
    sampled: Vec<(u64, Vec<u8>)>,
}

impl TiffSet {
    /// The listing's IR, its objects' elements and keys: one key, the named file.
    pub fn new(ir: Ir, objects: Vec<u32>, keys: &[(String, u64)]) -> Res<TiffSet> {
        let [(key, size)] = keys else {
            return Err(format!("a TIFF store run starts from the named file alone, not {} objects", keys.len()));
        };
        Ok(TiffSet {
            ir: Some(ir),
            tree: Tree::default(),
            named_key: key.clone(),
            named: Some(File { index: 0, parser: Tiff::named(*size, key), waiting: false }),
            siblings: Vec::new(),
            keys: vec![key.clone()],
            elems: objects,
            phase: Phase::Named,
            parsed: Vec::new(),
            fill: Vec::new(),
            finished: None,
            sampling: Vec::new(),
            sampled: Vec::new(),
        })
    }

    /// A step of every file that has reads to make, all in one batch: None when none has. Each
    /// file ends a run of steps by waiting (`Step::Beside`) or done.
    fn sibling_reads(&mut self) -> Res<Option<StoreStep>> {
        let mut asked = Vec::new();
        for f in &mut self.siblings {
            while !f.waiting && !f.parser.is_done() {
                match f.parser.step()? {
                    Step::Read(r) => {
                        asked.extend(r.into_iter().map(|(o, n)| (f.index, o, n)));
                        break;
                    }
                    Step::Beside => f.waiting = true,
                    Step::Done => f.waiting = true,
                }
            }
        }
        Ok((!asked.is_empty()).then_some(StoreStep::Read(asked)))
    }

    /// The parsers' IRs and facts, then the reads of what the view shows that they did not keep.
    fn finish_parsers(&mut self) -> Res<Option<StoreStep>> {
        let mut files = Vec::with_capacity(1 + self.siblings.len());
        files.push(self.named.take().ok_or("internal: no named file")?);
        files.append(&mut self.siblings);
        for f in files {
            let (tiles, ifds) = (f.parser.tile_elements(), f.parser.ifd_elements());
            let (ir, facts) = f.parser.finish()?;
            for (i, o, n) in unread_shown(&ir, "tiff") {
                self.fill.push((f.index, i, o, n));
            }
            self.parsed.push((ir, facts, tiles, ifds));
        }
        let mut asked: Vec<(usize, u64, u64)> = self.fill.iter().map(|&(k, _, o, n)| (k, o, n)).collect();
        asked.sort_unstable();
        asked.dedup();
        Ok((!asked.is_empty()).then_some(StoreStep::Read(asked)))
    }
}

impl TiffSet {
    /// Joins the files' IRs into the store IR, gives each plane its tiles or strips, and asks for
    /// the bytes the windows sample.
    fn join(&mut self) -> Res<Option<StoreStep>> {
        let mut ir = self.ir.take().ok_or("internal: no IR")?;
        let mut tiles: Vec<Vec<Option<u32>>> = Vec::with_capacity(self.parsed.len());
        let mut ifds: Vec<Vec<u32>> = Vec::with_capacity(self.parsed.len());
        let mut facts = J::Null;
        for (k, (mut file, f, t, i)) in std::mem::take(&mut self.parsed).into_iter().enumerate() {
            file.sort_values();
            let e = self.elems[k];
            ir.describe_in_place(e)?;
            let at = ir.graft(&file, e)?;
            tiles.push(t.into_iter().map(|x| x.map(|i| at[i as usize])).collect());
            ifds.push(i.into_iter().map(|i| at[i as usize]).collect());
            if k == 0 {
                facts = f;
            }
        }
        // each plane's tiles or strips, and its IFD (the key of its description), in the file that
        // holds it
        let plan = facts["plan"].take();
        let of = |p: &J| p.as_array().map(|fi| (fi[0].as_u64().unwrap_or(0) as usize, fi[1].as_u64().unwrap_or(0) as usize));
        let planes: Vec<J> = plan.as_array().into_iter().flatten().map(|p| {
            of(p).map_or(J::Null, |(f, i)| json!(tiles.get(f).and_then(|t| t.get(i)).copied().flatten()))
        }).collect();
        let declared_by: Vec<J> = plan.as_array().into_iter().flatten().map(|p| {
            of(p).map_or(J::Null, |(f, i)| json!(ifds.get(f).and_then(|t| t.get(i))))
        }).collect();
        if let Some(lv) = facts["levels"].get_mut(0) {
            lv["planes"] = J::Array(planes);
            lv["declared_by"] = J::Array(declared_by);
        }
        facts["named_key"] = json!(self.named_key);
        facts["files"] = json!(self.keys[1..]);
        let (ir, facts) = crate::sans::finish(true, ir, facts)?;
        let sources = ir.sources();
        let mut asked = Vec::new();
        let mut ranges = crate::project::tiff::set_reads(&ir, &facts)?;
        ranges.sort_unstable();
        ranges.dedup();
        for (at, n) in ranges {
            let (o, base) = sources.at(&ir, at).ok_or("internal: a sampled range in no object")?;
            let index = self.elems.iter().position(|&e| e == o).ok_or("internal: a sampled range in an unknown object")?;
            asked.push((index, at - base, n));
            self.sampling.push((index, at - base, at));
        }
        self.finished = Some((ir, facts));
        Ok((!asked.is_empty()).then_some(StoreStep::Read(asked)))
    }
}

impl StoreParser for TiffSet {
    fn step(&mut self) -> Res<StoreStep> {
        loop {
            match self.phase {
                Phase::Named | Phase::Layout => {
                    let f = self.named.as_mut().ok_or("internal: no named file")?;
                    match f.parser.step()? {
                        Step::Read(r) => return Ok(StoreStep::Read(r.into_iter().map(|(o, n)| (0, o, n)).collect())),
                        Step::Beside => {
                            let wanted = f.parser.wanted();
                            self.phase = Phase::Siblings;
                            if !wanted.is_empty() {
                                return Ok(StoreStep::Probe(wanted));
                            }
                        }
                        Step::Done => {
                            let plan = f.parser.sibling_plan();
                            let expected = f.parser.expected().ok_or("internal: no format")?;
                            for (s, planes) in self.siblings.iter_mut().zip(plan) {
                                s.parser.give_planes(planes, expected.clone());
                                s.waiting = false;
                            }
                            self.phase = Phase::Planes;
                        }
                    }
                }
                Phase::Siblings => {
                    if let Some(s) = self.sibling_reads()? {
                        return Ok(s);
                    }
                    let named = self.named.as_mut().ok_or("internal: no named file")?;
                    for (k, s) in self.siblings.iter().enumerate() {
                        named.parser.check_sibling(&self.keys[k + 1], &s.parser)?;
                    }
                    let parsers: Vec<&Tiff> = self.siblings.iter().map(|s| &s.parser).collect();
                    let more = named.parser.next_of_family(&parsers);
                    if let Some(key) = more {
                        return Ok(StoreStep::Probe(vec![key]));
                    }
                    named.parser.give_siblings(&parsers);
                    self.phase = Phase::Layout;
                }
                Phase::Planes => {
                    if let Some(s) = self.sibling_reads()? {
                        return Ok(s);
                    }
                    self.phase = Phase::Fill;
                    if let Some(s) = self.finish_parsers()? {
                        return Ok(s);
                    }
                }
                Phase::Fill => {
                    self.phase = Phase::Samples;
                    if let Some(s) = self.join()? {
                        return Ok(s);
                    }
                }
                Phase::Samples => {
                    self.phase = Phase::Done;
                    return Ok(StoreStep::Done);
                }
                Phase::Done => return Ok(StoreStep::Done),
            }
        }
    }

    fn feed(&mut self, object: usize, offset: u64, data: &[u8]) {
        if self.phase == Phase::Samples {
            if let Some(&(_, _, at)) = self.sampling.iter().find(|s| s.0 == object && s.1 == offset) {
                self.sampled.push((at, data.to_vec()));
            }
            return;
        }
        if self.phase == Phase::Fill {
            for &(k, i, o, n) in &self.fill {
                if k == object && o == offset && n == data.len() as u64 {
                    self.parsed[k].0.put_value(i, data);
                }
            }
            return;
        }
        if object == 0 {
            if let Some(f) = self.named.as_mut() {
                f.parser.feed(offset, data.to_vec());
            }
        } else if let Some(f) = self.siblings.get_mut(object - 1) {
            f.parser.feed(offset, data.to_vec());
        }
    }

    fn found(&mut self, key: &str, index: usize, size: Option<u64>) -> Res<Option<u32>> {
        let named = self.named.as_mut().ok_or("internal: no named file")?;
        let Some(size) = size else {
            named.parser.sibling_absent(key)?;
            return Ok(None);
        };
        if index != self.keys.len() {
            return Err("internal: a found object out of order".into());
        }
        let ir = self.ir.as_mut().ok_or("internal: no IR")?;
        let e = self.tree.object(ir, key, size)?;
        self.keys.push(key.to_string());
        self.elems.push(e);
        self.siblings.push(File { index, parser: Tiff::sibling(size), waiting: false });
        Ok(Some(e))
    }

    fn finish(mut self: Box<Self>) -> Res<(Ir, J)> {
        if self.phase != Phase::Done {
            return Err("internal: the parse is not done".into());
        }
        let (ir, mut facts) = self.finished.take().ok_or("internal: the files were not joined")?;
        self.sampled.sort_unstable_by_key(|s| s.0);
        facts["set_bytes"] = J::Array(self.sampled.iter().map(|(at, b)| json!([at, b])).collect());
        Ok((ir, facts))
    }
}
