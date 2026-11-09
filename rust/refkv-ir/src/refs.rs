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

/// A part of a form's recipe, relative to its element's first byte (conventions §8.5
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#85-forms-and-recipes>).
#[derive(Debug, PartialEq)]
enum Step {
    /// `["src", o, n]`: bytes from an offset: to the element's end (None), or of a length
    Src(u64, Option<u64>),
    /// `["rows", o, s, n, c]`: c runs of n bytes, the first at o, each s bytes after the one before
    Rows { o: u64, s: u64, n: u64, c: u64 },
    /// `["shared", k]`: a shared data source of the IR
    Shared(usize),
}

/// The references of an output, charged to a budget: at most 2^22 + 4 · size
/// references, and at most 2^20 repeats, references whose source ranges (virtual offset, length,
/// in order) an earlier one has.
pub struct Refs<'a> {
    pub ir: &'a Ir,
    pub out: Out,
    /// each form's recipe (None for a form without one), or why it does not follow the recipe grammar
    recipes: Vec<Result<Option<Vec<Step>>, String>>,
    seen: HashSet<Vec<(u64, u64)>>,
    limits: [u64; 2],
    spent: [u64; 2],
    /// of a store IR: each element's object (`in_object_of`)
    sources: Option<crate::ir::Sources>,
    /// the object the ranges being added are of, and its virtual offset
    object: Option<(String, u64)>,
    /// of a store IR: each object's virtual offset, by key (for the parts that name one)
    bases: Option<std::collections::HashMap<String, u64>>,
}

const WHAT: [&str; 2] = ["refs", "repeats"];

/// A form's recipe, read by the grammar of conventions §8.5
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#85-forms-and-recipes>: None for a
/// form with no recipe (or a null one), and an error for a recipe that is not a list of parts each
/// with exactly the fields its kind has: `["src", o, n]` (`n` an integer or null), `["shared", k]`
/// and `["rows", o, s, n, c]`, every number a non-negative integer.
fn recipe(form: &str) -> Result<Option<Vec<Step>>, String> {
    let Ok(v) = serde_json::from_str::<J>(form) else { return Ok(None) };
    let parts = match v.get("recipe") {
        None | Some(J::Null) => return Ok(None),
        Some(J::Array(parts)) => parts,
        Some(r) => return Err(format!("its recipe {r} is not a list of parts")),
    };
    let step = |p: &J| -> Option<Step> {
        let p = p.as_array()?;
        let at = |k: usize| p[k].as_u64();
        Some(match (p.first()?.as_str()?, p.len()) {
            ("src", 3) => Step::Src(at(1)?, if p[2].is_null() { None } else { Some(at(2)?) }),
            ("shared", 2) => Step::Shared(usize::try_from(at(1)?).ok()?),
            ("rows", 5) => Step::Rows { o: at(1)?, s: at(2)?, n: at(3)?, c: at(4)? },
            _ => return None,
        })
    };
    parts
        .iter()
        .map(|p| step(p).ok_or_else(|| format!("its recipe has a part {p} that the recipe grammar does not have")))
        .collect::<Result<_, _>>()
        .map(Some)
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
            bases: None,
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

    /// An element's recipe (relative to its first byte) as output parts. A `src` part to the
    /// element's end must start in it; a `rows` part whose runs meet is one range, and one whose
    /// runs do not may have no more runs than a reference payload has bytes.
    pub fn parts(&mut self, start: u64, length: u64, form: u32) -> Result<Vec<Part>, String> {
        let steps = match self.recipes.get(form as usize) {
            Some(Ok(Some(steps))) => steps,
            Some(Err(e)) => return Err(format!("form {form}: {e}")),
            _ => return Err(format!("internal: form {form} has no recipe")),
        };
        let past = || format!("form {form}: a recipe part past 2^64 bytes");
        let at = |o: u64| start.checked_add(o).ok_or_else(past);
        let mut out = Vec::with_capacity(steps.len());
        for s in steps {
            match *s {
                Step::Src(o, n) => {
                    let n = match n {
                        Some(n) => n,
                        None => length.checked_sub(o).ok_or_else(|| {
                            format!("form {form}: a src part to the element's end from {o}, past its {length} bytes")
                        })?,
                    };
                    out.push(Part::Src(at(o)?, n));
                }
                Step::Rows { o, s, n, c } if c <= 1 || s == n => {
                    if c > 0 {
                        out.push(Part::Src(at(o)?, n.checked_mul(c).ok_or_else(past)?));
                    }
                }
                Step::Rows { o, s, n, c } => {
                    if c > MAX_PAYLOAD as u64 {
                        return Err(format!("form {form}: a rows part of {c} runs, more than a reference payload holds"));
                    }
                    let first = at(o)?;
                    for k in 0..c {
                        let off = k.checked_mul(s).and_then(|d| first.checked_add(d)).ok_or_else(past)?;
                        out.push(Part::Src(off, n));
                    }
                }
                Step::Shared(k) => {
                    let b = self.ir.shared.get(k).ok_or("internal: no shared source")?;
                    out.push(self.out.shared(b));
                }
            }
        }
        Ok(out)
    }

    /// `parts`' ranges of the source (of the object `in_object_of` names, or of the object a part
    /// names), at their virtual offsets: what a repeat compares.
    fn ranges(&mut self, parts: &[Part]) -> Vec<(u64, u64)> {
        let base = self.base();
        let mut out = Vec::new();
        for p in parts {
            match p {
                Part::Src(o, n) => out.push((base.wrapping_add(*o), *n)),
                Part::Obj(k, o, n) => {
                    let ir = self.ir;
                    let bases = self.bases.get_or_insert_with(|| {
                        let s = ir.sources();
                        s.objects.iter().map(|&o| (ir.key(o), s.base[o as usize])).collect()
                    });
                    // an object of no IR's (none is made) is a range of its own offset space
                    out.push((bases.get(k).copied().unwrap_or(u64::MAX).wrapping_add(*o), *n));
                }
                _ => {}
            }
        }
        out
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
        // a chunk of an object's ranges, named again, is a repeat too
        refs.chunk("e".into(), 0, 4, 0, Some(vec![Part::Obj("x".into(), 1, 2)])).unwrap();
        refs.chunk("f".into(), 0, 4, 0, Some(vec![Part::Obj("x".into(), 1, 2)])).unwrap();
        assert_eq!(refs.spent, [6, 3]);
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

    /// The parts of the element at [start, start + length) by the recipe `recipe` (a JSON list),
    /// with the shared source 0 "tail".
    fn recipe_parts(recipe: &str, start: u64, length: u64) -> Result<Vec<Part>, String> {
        let mut ir = Ir::new(1 << 20);
        ir.forms.id(&format!(r#"{{"recipe":{recipe}}}"#));
        ir.shared.push(b"tail".to_vec());
        Refs::new(&ir, Out::new("u")).parts(start, length, 0)
    }

    #[test]
    fn recipes_follow_the_grammar() {
        let cases: [(&str, Vec<Part>); 9] = [
            (r#"[["src",4,null]]"#, vec![Part::Src(14, 16)]),
            (r#"[["src",20,null]]"#, vec![Part::Src(30, 0)]),
            (r#"[["src",2,5],["shared",0]]"#, vec![Part::Src(12, 5), Part::Data(1, 0, 4)]),
            // ND2's padded rows: runs apart, each its own range
            (r#"[["rows",0,8,6,3]]"#, vec![Part::Src(10, 6), Part::Src(18, 6), Part::Src(26, 6)]),
            // runs that meet, and a single run, are one range
            (r#"[["rows",2,4,4,3]]"#, vec![Part::Src(12, 12)]),
            (r#"[["rows",2,100,5,1]]"#, vec![Part::Src(12, 5)]),
            (r#"[["rows",2,100,5,0]]"#, vec![]),
            (r#"[["rows",0,8,6,2],["src",0,1]]"#, vec![Part::Src(10, 6), Part::Src(18, 6), Part::Src(10, 1)]),
            (r#"[]"#, vec![]),
        ];
        for (recipe, want) in cases {
            assert_eq!(recipe_parts(recipe, 10, 20), Ok(want), "{recipe}");
        }
        // a form with no recipe, or a null one, has none
        for form in [r#"{"codec":["bytes"]}"#, r#"{"recipe":null}"#] {
            assert_eq!(recipe(form), Ok(None), "{form}");
        }
    }

    #[test]
    fn rejects_a_src_part_whose_length_is_not_an_integer() {
        for n in [r#""x""#, "-1", "1.5", "[]"] {
            let recipe = format!(r#"[["src",0,{n}]]"#);
            assert_eq!(recipe_parts(&recipe, 10, 20),
                       Err(format!(r#"form 0: its recipe has a part ["src",0,{n}] that the recipe grammar does not have"#)), "{n}");
        }
    }

    #[test]
    fn rejects_a_part_with_fields_past_its_kind() {
        for p in [r#"["src",0,4,1]"#, r#"["shared",0,0]"#, r#"["rows",0,8,6,3,0]"#] {
            assert_eq!(recipe_parts(&format!("[{p}]"), 10, 20),
                       Err(format!("form 0: its recipe has a part {p} that the recipe grammar does not have")), "{p}");
        }
    }

    #[test]
    fn rejects_a_part_missing_fields_of_its_kind() {
        for p in [r#"["src",0]"#, r#"["shared"]"#, r#"["rows",0,8,6]"#] {
            assert_eq!(recipe_parts(&format!("[{p}]"), 10, 20),
                       Err(format!("form 0: its recipe has a part {p} that the recipe grammar does not have")), "{p}");
        }
    }

    #[test]
    fn rejects_a_rows_part_whose_fields_are_not_integers() {
        for p in [r#"["rows",-1,8,6,3]"#, r#"["rows",0,null,6,3]"#, r#"["rows",0,8,"6",3]"#, r#"["rows",0,8,6,3.5]"#] {
            assert_eq!(recipe_parts(&format!("[{p}]"), 10, 20),
                       Err(format!("form 0: its recipe has a part {p} that the recipe grammar does not have")), "{p}");
        }
    }

    #[test]
    fn rejects_a_part_of_no_kind_of_the_grammar() {
        for p in [r#"["pad",4]"#, r#"[]"#, r#""src""#, r#"[0,4,null]"#] {
            assert_eq!(recipe_parts(&format!("[{p}]"), 10, 20),
                       Err(format!("form 0: its recipe has a part {p} that the recipe grammar does not have")), "{p}");
        }
    }

    #[test]
    fn rejects_a_recipe_that_is_not_a_list() {
        assert_eq!(recipe_parts(r#"{"src":[0,4]}"#, 10, 20),
                   Err(r#"form 0: its recipe {"src":[0,4]} is not a list of parts"#.into()));
    }

    #[test]
    fn rejects_a_src_part_to_the_end_from_past_the_element() {
        assert_eq!(recipe_parts(r#"[["src",21,null]]"#, 10, 20),
                   Err("form 0: a src part to the element's end from 21, past its 20 bytes".into()));
    }

    #[test]
    fn rejects_a_rows_part_of_more_runs_than_a_payload_holds() {
        assert_eq!(recipe_parts(r#"[["rows",0,8,6,65520]]"#, 10, 20),
                   Err("form 0: a rows part of 65520 runs, more than a reference payload holds".into()));
    }

    #[test]
    fn rejects_a_part_past_2_to_the_64_bytes() {
        for recipe in [r#"[["src",18446744073709551615,1]]"#, r#"[["rows",0,4,4,4611686018427387905]]"#,
                       r#"[["rows",0,18446744073709551615,1,2]]"#] {
            assert_eq!(recipe_parts(recipe, 10, 20), Err("form 0: a recipe part past 2^64 bytes".into()), "{recipe}");
        }
    }

    #[test]
    fn rejects_a_form_with_no_recipe() {
        let mut ir = Ir::new(100);
        ir.forms.id(r#"{"codec":["bytes"]}"#);
        let mut refs = Refs::new(&ir, Out::new("u"));
        assert_eq!(refs.parts(0, 160, 0), Err("internal: form 0 has no recipe".into()));
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
