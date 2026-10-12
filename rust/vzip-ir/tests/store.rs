//! Store IRs (conventions §8.9
//! <https://github.com/d-v-b/vzip/blob/main/spec/conventions.md#89-stores>): many sources, the
//! objects of a listing. One test for the IRs that keep the invariants (per object coverage
//! and injectivity, referenced objects as one leaf, the keys' names), and one per way to break
//! them.

use vzip_ir::check::{check, finish, held_leaves};
use vzip_ir::ir::*;

/// A store of `.zgroup` (held: one text value), `a/.zarray` (held: two values), the chunk
/// objects `a/0.0` .. `a/0.3` (referenced), an empty object `a/empty`, and `b/c~d` (referenced).
fn store() -> (Ir, Vec<u32>) {
    let keys: Vec<(String, u64)> = [(".zgroup", 24u64), ("a/.zarray", 40), ("a/0.0", 100), ("a/0.1", 90), ("a/0.2", 80),
        ("a/0.3", 70), ("a/empty", 0), ("b/c~d", 5)]
        .iter().map(|(k, n)| (k.to_string(), *n)).collect();
    let mut ir = Ir::new_store();
    let objs = ir.listing(&keys).unwrap();
    ir.value(objs[0], "text", NO_INDEX, "ascii[24]", 0, (0, 24), None).unwrap();
    ir.value(objs[1], "head", NO_INDEX, "ascii[10]", 0, (0, 10), None).unwrap();
    ir.value(objs[1], "tail", NO_INDEX, "ascii[30]", 0, (10, 30), None).unwrap();
    (ir, objs)
}

#[test]
fn a_store_ir_keeps_the_invariants_per_object_and_its_keys() {
    // segment names: the full name is the segment, trailing digits an index
    for (seg, shadowed, name, nidx) in [
        ("0", false, "", 0), ("12", false, "", 12), ("a.12", false, "a.", 12), ("007", false, "00", 7),
        ("00", false, "0", 0), (".zarray", false, ".zarray", NO_INDEX), ("c~d", false, "c%7Ed~", 0),
        ("a", true, "a~", 1), ("99999999999999999999", false, "99999999999999999999", NO_INDEX),
    ] {
        assert_eq!(key_name(seg, shadowed), (name.to_string(), nidx), "{seg}");
        assert_eq!(key_segment(name, nidx), seg, "{seg}");
    }
    let (mut ir, objs) = store();
    assert_eq!((ir.objects, ir.size), (8, 24 + 40 + 100 + 90 + 80 + 70 + 5));
    finish(&mut ir).unwrap();
    check(&ir).unwrap();
    let keys: Vec<String> = objs.iter().map(|&o| ir.key(o)).collect();
    assert_eq!(keys, [".zgroup", "a/.zarray", "a/0.0", "a/0.1", "a/0.2", "a/0.3", "a/empty", "b/c~d"]);
    // an object whose key is a directory too is named apart from it
    let mut both = Ir::new_store();
    let o = both.listing(&[("a".into(), 3), ("a/b".into(), 4)]).unwrap();
    assert_eq!((both.name_of(o[0]), both.key(o[0]), both.key(o[1])), ("a~1".to_string(), "a".to_string(), "a/b".to_string()));
    finish(&mut both).unwrap();
    check(&both).unwrap();
    // the held objects rebuild from their leaves, each in its own source
    assert_eq!(held_leaves(&ir).unwrap(), vec![(objs[0], vec![(0, 24)]), (objs[1], vec![(0, 40)])]);
    // bytes a held object's elements leave are gaps of that object, at its offsets
    let (mut ir, objs) = store();
    ir.value(objs[7], "x", NO_INDEX, "u1", 0, (1, 1), None).unwrap(); // b/c~d is held now
    finish(&mut ir).unwrap();
    check(&ir).unwrap();
    let gaps: Vec<(String, u64, u64)> = (0..ir.len()).filter(|&i| ir.kind[i] == GAP)
        .map(|i| (ir.path(i as u32), ir.start[i], ir.len[i])).collect();
    assert_eq!(gaps, [("b/c%7Ed~0/gaps/0".to_string(), 0, 1), ("b/c%7Ed~0/gaps/2".to_string(), 2, 3)]);
    // two leaves of one object over one byte: the second an alias of the first
    let (mut ir, objs) = store();
    ir.value(objs[0], "again", NO_INDEX, "u1", 0, (3, 1), None).unwrap();
    finish(&mut ir).unwrap();
    check(&ir).unwrap();
    assert_eq!(ir.kind[ir.len() - 1], ALIAS);
    // runs inside an object
    let mut ir = Ir::new_store();
    let o = ir.listing(&[("f".into(), 64)]).unwrap()[0];
    let mut prev = None;
    for k in 0..8u64 {
        let r = ir.value(o, "w/", k, "<u8", 0, (8 * k, 8), None).unwrap();
        if let Some(p) = prev {
            if ir.fold(p, r) {
                continue;
            }
        }
        prev = Some(r);
    }
    assert_eq!(ir.runs.len(), 1);
    finish(&mut ir).unwrap();
    check(&ir).unwrap();
    assert_eq!(held_leaves(&ir).unwrap(), vec![(o, vec![(0, 64)])]);
}

#[test]
fn a_key_listed_twice_is_refused() {
    let e = Ir::new_store().listing(&[("a".into(), 1), ("a".into(), 2)]).unwrap_err();
    assert_eq!(e, "key \"a\" is listed twice");
}

#[test]
fn a_key_with_an_empty_segment_is_refused() {
    let e = Ir::new_store().listing(&[("a//b".into(), 1)]).unwrap_err();
    assert_eq!(e, "key \"a//b\" has an empty, . or .. segment");
}

#[test]
fn an_object_in_a_file_ir_is_refused() {
    let mut ir = Ir::new(10);
    assert_eq!(ir.object(0, "a", NO_INDEX, 1).unwrap_err(), "an object in a file IR");
    ir.add(OBJECT, 0, "a", NO_INDEX, "", 0, Some((0, 10))).unwrap();
    assert!(check(&ir).unwrap_err().contains("is an object, in a file IR"));
}

#[test]
fn an_extent_outside_every_object_is_refused() {
    let (mut ir, _) = store();
    ir.value(0, "stray", NO_INDEX, "u1", 0, (0, 1), None).unwrap();
    assert!(finish(&mut ir).unwrap_err().contains("is outside every object of the store, and is not a directory"));
}

#[test]
fn an_object_inside_an_object_is_refused() {
    let (mut ir, objs) = store();
    ir.object(objs[0], "inner", NO_INDEX, 3).unwrap();
    assert!(finish(&mut ir).unwrap_err().contains("is inside object \".zgroup\""));
}

#[test]
fn an_object_that_does_not_span_its_source_is_refused() {
    let (mut ir, objs) = store();
    ir.start[objs[2] as usize] = 5;
    assert!(check(&ir).unwrap_err().contains("does not span its source"));
}

#[test]
fn a_leaf_past_its_object_is_refused() {
    let (mut ir, objs) = store();
    ir.value(objs[1], "over", NO_INDEX, "ascii[8]", 0, (36, 8), None).unwrap();
    assert!(finish(&mut ir).unwrap_err().contains("claims bytes past the end of object \"a/.zarray\" (40 bytes)"));
}

#[test]
fn a_run_past_its_object_is_refused() {
    let (mut ir, objs) = store();
    let r = ir.value(objs[1], "w/", 0, "u1", 0, (0, 1), None).unwrap();
    ir.runs.push((r, 41, 1));
    assert!(finish(&mut ir).unwrap_err().contains("claims bytes past the end of object \"a/.zarray\""));
}

#[test]
fn a_length_in_an_empty_object_is_refused() {
    let (mut ir, objs) = store();
    ir.value(objs[6], "x", NO_INDEX, "u1", 0, (0, 1), None).unwrap();
    assert!(finish(&mut ir).unwrap_err().contains("has a length in the empty object \"a/empty\""));
}

#[test]
fn bytes_of_an_object_claimed_twice_are_refused() {
    let (mut ir, objs) = store();
    finish(&mut ir).unwrap();
    ir.finished = false;
    ir.value(objs[1], "dup", NO_INDEX, "u1", 0, (12, 1), None).unwrap();
    ir.build_children();
    let e = check(&ir).unwrap_err();
    assert!(e.starts_with("bytes [12, 13) of object \"a/.zarray\" are claimed by"), "{e}");
}

#[test]
fn bytes_of_an_object_claimed_by_no_element_are_refused() {
    let (mut ir, _) = store();
    let tail = ir.len() - 1;
    ir.len[tail] = 20; // "tail" ends at 30: bytes 30..40 of a/.zarray unclaimed
    ir.build_children();
    let e = check(&ir).unwrap_err();
    assert!(e.starts_with("bytes [30, 40) of object \"a/.zarray\" are claimed by no element"), "{e}");
}

#[test]
fn a_count_of_objects_other_than_the_description_s_is_refused() {
    let (mut ir, _) = store();
    ir.objects += 1;
    assert_eq!(check(&ir).unwrap_err(), "the store IR has 8 objects, its description 9");
}

#[test]
fn a_store_root_with_an_extent_is_refused() {
    let (mut ir, _) = store();
    ir.len[0] = 3;
    assert_eq!(check(&ir).unwrap_err(), "the root's extent is (0, 3): a store's root has none");
}
