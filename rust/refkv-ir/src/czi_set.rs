//! A CZI image across numbered files (spec/zarrify/czi.md §7.9
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/czi.md#79-several-files>): the store run
//! of the `czi` format, which a file run starts over as when its file has siblings
//! (spec/zarrify.md §1.9
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#19-sources-beside-the-input>). Each
//! file has a parser of its own, of the file run's kind, and its own file IR, which joins the store
//! IR under the file's object once they are all done (`Ir::graft`). The parts are read one at a
//! time, in order, since whether there is another may depend on what the last one holds.

use crate::beside::{Tree, MAX_FILES};
use crate::czi::{Czi, Placed, Planes};
use crate::ir::{Ir, Res};
use crate::nd2::Step;
use crate::run::unread_shown;
use crate::store::{StoreParser, StoreStep};
use serde_json::{json, Value as J};

#[derive(PartialEq, Clone, Copy)]
enum Phase {
    /// the named file
    Named,
    /// which part is next, if any
    Next,
    /// waiting for the answer to a probe
    Probed,
    /// the last part found
    Part,
    /// the values the mirror's view shows that the parsers did not keep
    Fill,
    Done,
}

/// `<base>.czi` of the part named `<base>(<p>).czi` or `<base> (<p>).czi`, when `key` has that form.
pub fn master_key(key: &str) -> Option<String> {
    let (stem, ext) = split_extension(key);
    let inner = stem.strip_suffix(')')?;
    let (base, digits) = inner.rsplit_once('(')?;
    if digits.is_empty() || !digits.bytes().all(|c| c.is_ascii_digit()) {
        return None;
    }
    let base = base.strip_suffix(' ').unwrap_or(base);
    (!base.is_empty()).then(|| format!("{base}{ext}"))
}

/// A key's name before its `.czi`, whatever its case, and that extension as it is written; a key
/// with no such extension is all name, and its parts' keys end in `.czi`.
fn split_extension(key: &str) -> (&str, &str) {
    let at = key.len().saturating_sub(4);
    match key.get(at..) {
        Some(e) if e.eq_ignore_ascii_case(".czi") => key.split_at(at),
        _ => (key, ".czi"),
    }
}

/// The key of part `p` of the file `key`, in each of its spellings.
fn spellings(key: &str, p: i32) -> [String; 2] {
    let (stem, ext) = split_extension(key);
    [format!("{stem}({p}){ext}"), format!("{stem} ({p}){ext}")]
}

/// A message of a part's parser, with the file it is about when it does not say.
fn blamed(key: &str, m: String) -> String {
    if m.starts_with("the file \"") || m.starts_with("budget:") || m.starts_with("internal:") {
        m
    } else {
        format!("{key}: {m}")
    }
}

pub struct CziSet {
    ir: Option<Ir>,
    tree: Tree,
    named_key: String,
    named: Czi,
    parts: Vec<Czi>,
    /// the found files' keys: the named file, then the parts
    keys: Vec<String>,
    elems: Vec<u32>,
    phase: Phase,
    /// the part to look for, and whether its first spelling is known to be absent
    next: i32,
    second: bool,
    highest: i32,
    planes: Option<Planes>,
    /// the parsed files, in the listing's order: (IR, what it places, the key of each entry's
    /// description, its subblocks)
    parsed: Vec<(Ir, Vec<Placed>, Vec<Option<u32>>, usize)>,
    fill: Vec<(usize, u32, u64, u64)>,
    finished: Option<(Ir, J)>,
}

impl CziSet {
    /// The listing's IR, its objects' elements and keys: one key, the named file.
    pub fn new(ir: Ir, objects: Vec<u32>, keys: &[(String, u64)]) -> Res<CziSet> {
        let [(key, size)] = keys else {
            return Err(format!("a CZI store run starts from the named file alone, not {} objects", keys.len()));
        };
        Ok(CziSet {
            ir: Some(ir),
            tree: Tree::default(),
            named_key: key.clone(),
            named: Czi::named(*size, key),
            parts: Vec::new(),
            keys: vec![key.clone()],
            elems: objects,
            phase: Phase::Named,
            next: 1,
            second: false,
            highest: 0,
            planes: None,
            parsed: Vec::new(),
            fill: Vec::new(),
            finished: None,
        })
    }

    /// The parsers' IRs, then the reads of what the view shows that they did not keep.
    fn finish_parsers(&mut self) -> Res<Option<StoreStep>> {
        let mut parsed = vec![self.named.take_file()?];
        for p in &mut self.parts {
            parsed.push(p.take_file()?);
        }
        for (k, (ir, ..)) in parsed.iter().enumerate() {
            self.fill.extend(unread_shown(ir, "czi").into_iter().map(|(i, o, n)| (k, i, o, n)));
        }
        self.parsed = parsed;
        let mut asked: Vec<(usize, u64, u64)> = self.fill.iter().map(|&(k, _, o, n)| (k, o, n)).collect();
        asked.sort_unstable();
        asked.dedup();
        Ok((!asked.is_empty()).then_some(StoreStep::Read(asked)))
    }

    /// Joins the files' IRs into the store IR, and places their subblocks as one image's: the
    /// named file's, then each part's, in order.
    fn join(&mut self) -> Res<()> {
        let mut ir = self.ir.take().ok_or("internal: no IR")?;
        let mut placed = Vec::new();
        // `declared_by`: the key of each entry's description, the named file's entries, then each
        // part's, as they are placed
        let mut declared_by: Vec<Option<u32>> = Vec::new();
        let (mut offset, mut subblocks) = (0, 0);
        for (k, (mut file, ps, keys, own)) in std::mem::take(&mut self.parsed).into_iter().enumerate() {
            file.sort_values();
            let e = self.elems[k];
            ir.describe_in_place(e)?;
            // the graft carries the file's descriptions, their elements renumbered as `at` gives
            let at = ir.graft(&file, e)?;
            placed.extend(ps.into_iter().map(|mut p| {
                p.el = at[p.el as usize];
                p.key = at[p.key as usize];
                p.index += offset;
                p
            }));
            offset += keys.len();
            declared_by.extend(keys.into_iter().map(|key| key.map(|key| at[key as usize])));
            subblocks += own;
        }
        let mut facts = self.named.layout_of(placed, subblocks)?;
        facts["declared_by"] = json!(declared_by);
        facts["root"]["parts"] = json!(self.keys[1..]);
        facts["named_key"] = json!(self.named_key);
        self.finished = Some(crate::sans::finish(true, ir, facts)?);
        Ok(())
    }
}

impl StoreParser for CziSet {
    fn step(&mut self) -> Res<StoreStep> {
        loop {
            match self.phase {
                Phase::Named => match self.named.step()? {
                    Step::Read(r) if r.is_empty() => {}
                    Step::Read(r) => return Ok(StoreStep::Read(r.into_iter().map(|(o, n)| (0, o, n)).collect())),
                    Step::Beside => return Err("internal: a named file ended its store run".into()),
                    Step::Done => {
                        self.highest = self.named.highest();
                        if self.highest as u64 > MAX_FILES {
                            return Err(format!("budget: the input names more than {MAX_FILES} files"));
                        }
                        self.planes = Some(self.named.planes());
                        self.phase = Phase::Next;
                    }
                },
                Phase::Next => {
                    let complete = self.planes.as_ref().is_none_or(Planes::complete);
                    if self.next > self.highest && complete {
                        self.phase = Phase::Fill;
                        if let Some(s) = self.finish_parsers()? {
                            return Ok(s);
                        }
                        continue;
                    }
                    self.phase = Phase::Probed;
                    let [first, second] = spellings(&self.named_key, self.next);
                    return Ok(StoreStep::Probe(vec![if self.second { second } else { first }]));
                }
                Phase::Probed => return Err("internal: a step before the probe was answered".into()),
                Phase::Part => {
                    let k = self.parts.len();
                    let part = self.parts.last_mut().ok_or("internal: no part")?;
                    match part.step().map_err(|m| blamed(&self.keys[k], m))? {
                        Step::Read(r) if r.is_empty() => {}
                        Step::Read(r) => return Ok(StoreStep::Read(r.into_iter().map(|(o, n)| (k, o, n)).collect())),
                        Step::Beside => return Err("internal: a part ended its store run".into()),
                        Step::Done => {
                            if let Some(planes) = self.planes.as_mut() {
                                part.add_planes(planes);
                            }
                            self.next += 1;
                            self.second = false;
                            self.phase = Phase::Next;
                        }
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
        if self.phase == Phase::Fill {
            for &(k, i, o, n) in &self.fill {
                if k == object && o == offset && n == data.len() as u64 {
                    self.parsed[k].0.put_value(i, data);
                }
            }
        } else if object == 0 {
            self.named.feed(offset, data.to_vec());
        } else if let Some(p) = self.parts.get_mut(object - 1) {
            p.feed(offset, data.to_vec());
        }
    }

    fn found(&mut self, key: &str, index: usize, size: Option<u64>) -> Res<Option<u32>> {
        let required = self.next <= self.highest;
        let Some(size) = size else {
            if !self.second {
                self.second = true;
                self.phase = Phase::Next;
                return Ok(None);
            }
            if required {
                let [first, _] = spellings(&self.named_key, self.next);
                return Err(format!("the file \"{first}\" does not exist"));
            }
            // the first absent ends the family
            self.highest = self.next - 1;
            self.planes = None;
            self.phase = Phase::Next;
            return Ok(None);
        };
        if index != self.keys.len() {
            return Err("internal: a found object out of order".into());
        }
        let ir = self.ir.as_mut().ok_or("internal: no IR")?;
        let e = self.tree.object(ir, key, size)?;
        self.keys.push(key.to_string());
        self.elems.push(e);
        let given = self.named.given_to(self.next);
        let complete = self.planes.as_ref().is_none_or(Planes::complete);
        self.parts.push(Czi::part(size, key, self.next, self.named.master()?, given, complete));
        self.phase = Phase::Part;
        Ok(Some(e))
    }

    fn finish(mut self: Box<Self>) -> Res<(Ir, J)> {
        if self.phase != Phase::Done || !self.named.is_done() {
            return Err("internal: the parse is not done".into());
        }
        self.finished.take().ok_or_else(|| "internal: the files were not joined".to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_part_names_its_master_by_either_spelling() {
        let cases = [
            ("name(1).czi", Some("name.czi")),
            ("name (12).czi", Some("name.czi")),
            ("a b(2).CZI", Some("a b.CZI")),
            ("a(1)(2).czi", Some("a(1).czi")),
            ("name.czi", None),
            ("name().czi", None),
            ("name(x).czi", None),
            ("(1).czi", None),
        ];
        for (key, want) in cases {
            assert_eq!(master_key(key).as_deref(), want, "{key}");
        }
    }

    #[test]
    fn a_part_is_spelled_two_ways() {
        assert_eq!(spellings("a b.czi", 3), ["a b(3).czi".to_string(), "a b (3).czi".to_string()]);
        assert_eq!(spellings("a.CZI", 1), ["a(1).CZI".to_string(), "a (1).CZI".to_string()]);
        assert_eq!(spellings("a.dat", 1), ["a.dat(1).czi".to_string(), "a.dat (1).czi".to_string()]);
    }
}
