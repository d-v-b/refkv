//! References to elements of an IR, checked against the IR (design/ARCHITECTURE.md §3.3
//! <https://github.com/d-v-b/refkv/blob/main/design/ARCHITECTURE.md#33-invariants>,
//! "the end of the amplification class"): every reference is charged to the run's
//! budget, and so is every repeated one, a reference to the source ranges a reference already
//! used, which the IR can only reach through an alias (it claims no byte twice),
//! whatever the format.

use crate::ir::Ir;
use crate::out::{payload_size, Out, Part, MAX_PAYLOAD};
use serde_json::Value as J;
use std::collections::HashSet;

/// A part of a form's recipe, relative to its element's first byte.
enum Step {
    /// bytes from an offset: to the element's end (None), or of a length
    Src(u64, Option<u64>),
    /// a shared data source of the IR
    Shared(usize),
}

/// The references of an output, charged to a budget: at most 2^22 + 4 · size
/// references, and at most 2^20 repeats, references whose source ranges (virtual offset, length,
/// in order) an earlier one has.
pub struct Refs<'a> {
    pub ir: &'a Ir,
    pub out: Out,
    /// each form's recipe (None for a form without one)
    recipes: Vec<Option<Vec<Step>>>,
    seen: HashSet<Vec<(u64, u64)>>,
    limits: [u64; 2],
    spent: [u64; 2],
    /// of a store IR: each element's object (`in_object_of`)
    sources: Option<crate::ir::Sources>,
    /// the object the ranges being added are of, and its virtual offset
    object: Option<(String, u64)>,
}

const WHAT: [&str; 2] = ["refs", "repeats"];

fn recipe(form: &str) -> Option<Vec<Step>> {
    let v: J = serde_json::from_str(form).ok()?;
    v.get("recipe")?
        .as_array()?
        .iter()
        .map(|p| match p[0].as_str()? {
            "src" => Some(Step::Src(p[1].as_u64()?, p[2].as_u64())),
            "shared" => Some(Step::Shared(p[1].as_u64()? as usize)),
            _ => None,
        })
        .collect()
}

impl<'a> Refs<'a> {
    pub fn new(ir: &'a Ir, out: Out) -> Refs<'a> {
        Refs {
            ir,
            out,
            recipes: ir.forms.list.iter().map(|f| recipe(f)).collect(),
            seen: HashSet::new(),
            limits: [(1 << 22) + 4 * ir.size, 1 << 20],
            spent: [0, 0],
            sources: None,
            object: None,
        }
    }

    /// The ranges added from now on are of the object that holds element `e`, when the IR is a
    /// store's (conventions §8.9
    /// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#89-stores>); a file IR's stay
    /// ranges of source 0.
    pub fn in_object_of(&mut self, e: u32) {
        if !self.ir.store {
            return;
        }
        let ir = self.ir;
        let sources = self.sources.get_or_insert_with(|| ir.sources());
        self.object = sources.of.get(e as usize).filter(|&&o| o != crate::ir::NONE)
            .map(|&o| (ir.key(o), sources.base[o as usize]));
    }

    fn base(&self) -> u64 {
        self.object.as_ref().map_or(0, |o| o.1)
    }

    /// `parts` as ranges of the object they are of (`in_object_of`).
    fn placed(&self, parts: Vec<Part>) -> Vec<Part> {
        let Some((key, _)) = &self.object else { return parts };
        parts.into_iter().map(|p| match p {
            Part::Src(o, n) => Part::Obj(key.clone(), o, n),
            p => p,
        }).collect()
    }

    fn charge(&mut self, k: usize) -> Result<(), String> {
        self.spent[k] += 1;
        if self.spent[k] > self.limits[k] {
            return Err(format!("budget: more than {} {}", self.limits[k], WHAT[k]));
        }
        Ok(())
    }

    /// An element's recipe (relative to its first byte) as output parts.
    pub fn parts(&mut self, start: u64, length: u64, form: u32) -> Result<Vec<Part>, String> {
        let steps = self
            .recipes
            .get(form as usize)
            .and_then(|r| r.as_ref())
            .ok_or_else(|| format!("internal: form {form} has no recipe"))?;
        let mut out = Vec::with_capacity(steps.len());
        for s in steps {
            out.push(match *s {
                Step::Src(o, n) => Part::Src(start + o, n.unwrap_or(length.wrapping_sub(o))),
                Step::Shared(k) => {
                    let b = self.ir.shared.get(k).ok_or("internal: no shared source")?;
                    self.out.shared(b)
                }
            });
        }
        Ok(out)
    }

    /// `parts`' ranges of the source (of the object `in_object_of` names), at their virtual
    /// offsets: what a repeat compares.
    fn ranges(&self, parts: &[Part]) -> Vec<(u64, u64)> {
        let base = self.base();
        parts.iter().filter_map(|p| match p {
            Part::Src(o, n) => Some((base.wrapping_add(*o), *n)),
            _ => None,
        }).collect()
    }

    /// Charges a reference to `parts`, and a repeat when an earlier one had its source ranges.
    fn charge_ranges(&mut self, parts: &[Part]) -> Result<(), String> {
        self.charge(0)?;
        let ranges = self.ranges(parts);
        if !ranges.is_empty() && !self.seen.insert(ranges) {
            self.charge(1)?;
        }
        Ok(())
    }

    /// The chunk `key` is the element at [start, start + length): its recipe (form
    /// `form`), or `parts` of it.
    pub fn chunk(&mut self, key: String, start: u64, length: u64, form: u32, parts: Option<Vec<Part>>) -> Result<(), String> {
        let parts = match parts {
            Some(p) => p,
            None => self.parts(start, length, form)?,
        };
        self.charge_ranges(&parts)?;
        let parts = self.placed(parts);
        if payload_size(&parts) > MAX_PAYLOAD {
            return Err(format!("{key}: a reference payload of more than {MAX_PAYLOAD} bytes"));
        }
        self.out.refs(&key, parts);
        Ok(())
    }

    /// The chunk `key` is the elements `strips` (start, length, form), each charged as `chunk`
    /// charges one, in order and with the ranges that meet merged, then `tail`.
    pub fn joined(&mut self, key: String, strips: &[(u64, u64, u32)], tail: Option<Part>) -> Result<(), String> {
        let mut parts: Vec<Part> = Vec::new();
        for &(start, length, form) in strips {
            let strip = self.parts(start, length, form)?;
            self.charge_ranges(&strip)?;
            for part in strip {
                match (parts.last_mut(), &part) {
                    (Some(Part::Src(o, n)), Part::Src(a, b)) if *o + *n == *a => *n += *b,
                    _ => parts.push(part),
                }
            }
        }
        parts.extend(tail);
        let parts = self.placed(parts);
        if payload_size(&parts) > MAX_PAYLOAD {
            return Err(format!("{key}: a reference payload of more than {MAX_PAYLOAD} bytes"));
        }
        self.out.refs(&key, parts);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// An IR of `size` bytes with one form (the element's bytes from 4, then shared source 0).
    fn ir(size: u64) -> Ir {
        let mut ir = Ir::new(size);
        ir.forms.id(r#"{"recipe":[["src",4,null],["src",0,2],["shared",0]]}"#);
        ir.shared.push(b"tail".to_vec());
        ir
    }

    #[test]
    fn references_name_recipes_and_charge_repeats() {
        let ir = ir(100);
        let mut refs = Refs::new(&ir, Out::new("u"));
        refs.chunk("a".into(), 10, 20, 0, None).unwrap();
        refs.chunk("b".into(), 10, 20, 0, Some(vec![Part::Src(10, 5)])).unwrap();
        refs.chunk("c".into(), 10, 20, 0, None).unwrap();
        // "b" names other bytes of the element, "c" the same ranges as "a": a repeat
        refs.chunk("d".into(), 0, 4, 0, Some(vec![Part::Src(10, 5)])).unwrap();
        // "d" names "b"'s ranges, from another element: a repeat too
        assert_eq!(refs.spent, [4, 2]);
        let whole = vec![Part::Src(14, 16), Part::Src(10, 2), Part::Data(1, 0, 4)];
        assert_eq!(refs.out.refs, vec![("a".into(), whole.clone()), ("b".into(), vec![Part::Src(10, 5)]), ("c".into(), whole),
                                       ("d".into(), vec![Part::Src(10, 5)])]);
        assert_eq!(refs.out.data, vec![b"tail".to_vec()]);
    }

    #[test]
    fn joined_merges_the_ranges_that_meet() {
        let mut ir = Ir::new(100);
        ir.forms.id(r#"{"recipe":[["src",0,null]]}"#);
        let mut refs = Refs::new(&ir, Out::new("u"));
        let cases: [(&str, &[(u64, u64, u32)], Option<Part>, Vec<Part>); 4] = [
            ("contiguous", &[(0, 4, 0), (4, 4, 0), (8, 4, 0)], None, vec![Part::Src(0, 12)]),
            ("a gap", &[(0, 4, 0), (10, 4, 0)], None, vec![Part::Src(0, 4), Part::Src(10, 4)]),
            ("out of order", &[(10, 4, 0), (0, 4, 0)], None, vec![Part::Src(10, 4), Part::Src(0, 4)]),
            ("a tail", &[(20, 4, 0), (24, 4, 0)], Some(Part::Data(1, 0, 8)), vec![Part::Src(20, 8), Part::Data(1, 0, 8)]),
        ];
        for (key, strips, tail, want) in cases {
            refs.joined(key.into(), strips, tail).unwrap();
            assert_eq!(refs.out.reference(key), Some(want.as_slice()), "{key}");
        }
        // each strip is charged, and a strip used again is a repeat
        assert_eq!(refs.spent, [9, 3]);
    }

    #[test]
    fn rejects_a_joined_chunk_past_the_budget() {
        let mut ir = Ir::new(100);
        ir.forms.id(r#"{"recipe":[["src",0,null]]}"#);
        let mut refs = Refs::new(&ir, Out::new("u"));
        refs.limits[0] = 2;
        assert_eq!(refs.joined("k".into(), &[(0, 4, 0), (4, 4, 0), (8, 4, 0)], None), Err("budget: more than 2 refs".into()));
    }

    #[test]
    fn rejects_a_joined_chunk_whose_payload_is_past_the_limit() {
        let mut ir = Ir::new(1 << 40);
        ir.forms.id(r#"{"recipe":[["src",0,null]]}"#);
        let mut refs = Refs::new(&ir, Out::new("u"));
        let strips: Vec<_> = (0..8192).map(|k| (k << 30, 1 << 20, 0)).collect();
        assert_eq!(refs.joined("k".into(), &strips, None), Err("k: a reference payload of more than 65519 bytes".into()));
    }

    #[test]
    fn rejects_a_form_whose_recipe_has_a_part_it_cannot_follow() {
        let mut ir = Ir::new(100);
        ir.forms.id(r#"{"recipe":[["rows",0,40,39,4]]}"#);
        let mut refs = Refs::new(&ir, Out::new("u"));
        assert_eq!(refs.parts(0, 160, 0).err().unwrap(), "internal: form 0 has no recipe");
    }

    #[test]
    fn rejects_references_past_the_budget() {
        let ir = ir(100);
        let mut refs = Refs::new(&ir, Out::new("u"));
        assert_eq!(refs.limits[0], (1 << 22) + 400);
        refs.limits[0] = 2;
        refs.chunk("a".into(), 0, 8, 0, None).unwrap();
        refs.chunk("b".into(), 8, 8, 0, None).unwrap();
        assert_eq!(refs.chunk("c".into(), 16, 8, 0, None), Err("budget: more than 2 refs".into()));
    }

    #[test]
    fn rejects_repeats_past_the_budget() {
        let ir = ir(100);
        let mut refs = Refs::new(&ir, Out::new("u"));
        assert_eq!(refs.limits[1], 1 << 20);
        refs.limits[1] = 1;
        refs.chunk("a".into(), 0, 8, 0, None).unwrap();
        refs.chunk("b".into(), 0, 8, 0, None).unwrap();
        assert_eq!(refs.chunk("c".into(), 0, 8, 0, None), Err("budget: more than 1 repeats".into()));
    }

    #[test]
    fn rejects_a_payload_past_the_limit() {
        let ir = ir(1 << 40);
        let mut refs = Refs::new(&ir, Out::new("u"));
        let parts = (0..8192).map(|k| Part::Src(k << 30, 1 << 20)).collect();
        assert_eq!(refs.chunk("k".into(), 0, 8, 0, Some(parts)), Err("k: a reference payload of more than 65519 bytes".into()));
    }
}
