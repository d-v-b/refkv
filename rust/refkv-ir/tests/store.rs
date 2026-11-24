//! Store IRs (conventions §8.9
//! <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#89-stores>): many sources, the
//! objects of a listing. One test for the IRs that keep the invariants (per object coverage
//! and injectivity, referenced objects as one leaf, the keys' names), and one per way to break
//! them; one for the limits a store parses under, one for a store at its row limit and one for a
//! store past it.

use refkv_ir::error::Error;
use refkv_ir::check::{check, finish, held_leaves};
use refkv_ir::ir::*;

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

/// The limits a store parses under are of its totals, `o` objects of `Z` bytes in all, whatever
/// the sizes that sum to `Z`: its records' is the limit of its table's rows (conventions §8.9
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#89-stores>), `2^22 + floor(Z / 4)
/// + 4 × o`, as its descriptions' is (spec/stored-array.md §6
/// <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#6-in-the-ir>).
#[test]
fn a_stores_limits_are_of_its_objects_count_and_total_size() {
    for sizes in [vec![], vec![0], vec![3; 4], vec![1, 2, 3, 4, 5], vec![63, 1, 1000, 7], vec![u64::MAX - 5, 3]] {
        let keys: Vec<(String, u64)> = sizes.iter().enumerate().map(|(k, &s)| (format!("d/k{k}"), s)).collect();
        let mut ir = Ir::new_store();
        ir.listing(&keys).unwrap();
        let (z, o) = (sizes.iter().sum::<u64>(), sizes.len() as u64);
        assert_eq!((ir.size, ir.objects), (z, o), "{sizes:?}");
        let b = ir.budget.as_ref().unwrap();
        let rows = (1 << 22) + z / 4 + 4 * o;
        assert_eq!(rows, refkv_ir::mirror::store_row_limit(z, o), "{sizes:?}");
        assert_eq!((b.records, b.descriptions, b.elements), (rows, rows, (1 << 30) + z / 4 + 4 * o), "{sizes:?}");
        assert_eq!(b.rows, (1u64 << 22).saturating_add(z.saturating_mul(4)).saturating_add(4 * o), "{sizes:?}");
        assert_eq!((b.read, b.reads), ((1u64 << 24).saturating_add(z.saturating_mul(2)), (1 << 20) + z / 64 + o), "{sizes:?}");
    }
}

/// The row limit of a store of four objects of 3 bytes: `2^22 + floor(12 / 4) + 4 × 4`. (Four
/// times an object's `4 + floor(3 / 4)` would be 3 less.)
const SMALL_OBJECTS_ROWS: u64 = (1 << 22) + 3 + 16;

/// A store of four objects of 3 bytes with `records` records: its root, its objects, and empty
/// structs under the first of them.
fn small_objects(records: u64) -> Res<Ir> {
    let mut ir = Ir::new_store();
    let keys: Vec<(String, u64)> = ["a", "b", "c", "d"].iter().map(|k| (k.to_string(), 3)).collect();
    let o = ir.listing(&keys)?;
    for k in ir.len() as u64..records {
        ir.struct_(o[0], "s/", k, 0, None)?;
    }
    Ok(ir)
}

#[test]
fn a_store_of_as_many_records_as_its_row_limit_parses() {
    let ir = small_objects(SMALL_OBJECTS_ROWS).unwrap();
    assert_eq!(ir.len() as u64, refkv_ir::mirror::store_row_limit(ir.size, ir.objects));
}

#[test]
fn a_store_of_more_records_than_its_row_limit_is_refused() {
    let e = small_objects(SMALL_OBJECTS_ROWS + 1).err().unwrap();
    assert_eq!(e, Error::budget("more than 4194323 IR records"));
}

#[test]
fn a_key_listed_twice_is_refused() {
    let e = Ir::new_store().listing(&[("a".into(), 1), ("a".into(), 2)]).unwrap_err();
    assert_eq!(e, Error::rejected("key \"a\" is listed twice"));
}

#[test]
fn a_key_with_an_empty_segment_is_refused() {
    let e = Ir::new_store().listing(&[("a//b".into(), 1)]).unwrap_err();
    assert_eq!(e, Error::rejected("key \"a//b\" has an empty, . or .. segment"));
}

#[test]
fn an_object_in_a_file_ir_is_refused() {
    let mut ir = Ir::new(10);
    assert_eq!(ir.object(0, "a", NO_INDEX, 1).unwrap_err(), Error::rejected("an object in a file IR"));
    ir.add(OBJECT, 0, "a", NO_INDEX, "", 0, Some((0, 10))).unwrap();
    assert!(check(&ir).unwrap_err().to_string().contains("is an object, in a file IR"));
}

#[test]
fn an_extent_outside_every_object_is_refused() {
    let (mut ir, _) = store();
    ir.value(0, "stray", NO_INDEX, "u1", 0, (0, 1), None).unwrap();
    assert!(finish(&mut ir).unwrap_err().to_string().contains("is outside every object of the store, and is not a directory"));
}

#[test]
fn an_object_inside_an_object_is_refused() {
    let (mut ir, objs) = store();
    ir.object(objs[0], "inner", NO_INDEX, 3).unwrap();
    assert!(finish(&mut ir).unwrap_err().to_string().contains("is inside object \".zgroup\""));
}

#[test]
fn an_object_that_does_not_span_its_source_is_refused() {
    let (mut ir, objs) = store();
    ir.start[objs[2] as usize] = 5;
    assert!(check(&ir).unwrap_err().to_string().contains("does not span its source"));
}

#[test]
fn a_leaf_past_its_object_is_refused() {
    let (mut ir, objs) = store();
    ir.value(objs[1], "over", NO_INDEX, "ascii[8]", 0, (36, 8), None).unwrap();
    assert!(finish(&mut ir).unwrap_err().to_string().contains("claims bytes past the end of object \"a/.zarray\" (40 bytes)"));
}

#[test]
fn a_run_past_its_object_is_refused() {
    let (mut ir, objs) = store();
    let r = ir.value(objs[1], "w/", 0, "u1", 0, (0, 1), None).unwrap();
    ir.runs.push((r, 41, 1));
    assert!(finish(&mut ir).unwrap_err().to_string().contains("claims bytes past the end of object \"a/.zarray\""));
}

#[test]
fn a_length_in_an_empty_object_is_refused() {
    let (mut ir, objs) = store();
    ir.value(objs[6], "x", NO_INDEX, "u1", 0, (0, 1), None).unwrap();
    assert!(finish(&mut ir).unwrap_err().to_string().contains("has a length in the empty object \"a/empty\""));
}

#[test]
fn bytes_of_an_object_claimed_twice_are_refused() {
    let (mut ir, objs) = store();
    finish(&mut ir).unwrap();
    ir.finished = false;
    ir.value(objs[1], "dup", NO_INDEX, "u1", 0, (12, 1), None).unwrap();
    ir.build_children();
    let e = check(&ir).unwrap_err();
    assert!(e.to_string().starts_with("bytes [12, 13) of object \"a/.zarray\" are claimed by"), "{e}");
}

#[test]
fn bytes_of_an_object_claimed_by_no_element_are_refused() {
    let (mut ir, _) = store();
    let tail = ir.len() - 1;
    ir.len[tail] = 20; // "tail" ends at 30: bytes 30..40 of a/.zarray unclaimed
    ir.build_children();
    let e = check(&ir).unwrap_err();
    assert!(e.to_string().starts_with("bytes [30, 40) of object \"a/.zarray\" are claimed by no element"), "{e}");
}

#[test]
fn a_count_of_objects_other_than_the_description_s_is_refused() {
    let (mut ir, _) = store();
    ir.objects += 1;
    assert_eq!(check(&ir).unwrap_err(), Error::rejected("the store IR has 8 objects, its description 9"));
}

#[test]
fn a_store_root_with_an_extent_is_refused() {
    let (mut ir, _) = store();
    ir.len[0] = 3;
    assert_eq!(check(&ir).unwrap_err(), Error::rejected("the root's extent is (0, 3): a store's root has none"));
}

#[test]
fn held_objects_take_their_bytes_once_in_any_order() {
    let (mut ir, objs) = store();
    ir.hold_all([(objs[1], &[1u8; 40][..])]).unwrap();
    let before = ir.vbytes.len();
    ir.hold_all([(objs[7], &[7u8; 5][..]), (objs[0], &[0u8; 24][..]), (objs[7], &[7u8; 5][..]), (objs[1], &[1u8; 40][..])]).unwrap();
    assert_eq!(ir.vbytes.len(), before + 29);
    assert_eq!(ir.value_bytes(objs[1]), Some(&[1u8; 40][..]));
    assert_eq!(ir.value_bytes(objs[0]), Some(&[0u8; 24][..]));
    assert_eq!(ir.value_bytes(objs[7]), Some(&[7u8; 5][..]));
}

#[test]
fn held_bytes_of_another_size_are_refused() {
    let (mut ir, objs) = store();
    assert!(ir.hold_all([(objs[0], &[0u8; 3][..])]).unwrap_err().to_string().contains("which is no object of that size"));
}

#[test]
fn held_bytes_of_an_element_that_is_no_object_are_refused() {
    let (mut ir, _) = store();
    let v = (ir.len() - 1) as u32;
    assert!(ir.hold_all([(v, &[0u8; 30][..])]).unwrap_err().to_string().contains("which is no object of that size"));
    assert!(ir.hold_all([(10_000, &[0u8; 1][..])]).unwrap_err().to_string().contains("which is no object of that size"));
}

#[test]
fn an_object_held_twice_with_other_bytes_is_refused() {
    let (mut ir, objs) = store();
    let e = ir.hold_all([(objs[0], &[0u8; 24][..]), (objs[0], &[1u8; 24][..])]).unwrap_err();
    assert_eq!(e, Error::internal(format!("element {} held twice, with other bytes", objs[0])));
}

#[test]
fn bytes_other_than_an_object_holds_are_refused() {
    let (mut ir, objs) = store();
    ir.hold_all([(objs[0], &[0u8; 24][..])]).unwrap();
    let e = ir.hold_all([(objs[0], &[1u8; 24][..])]).unwrap_err();
    assert_eq!(e, Error::internal(format!("element {} held twice, with other bytes", objs[0])));
}
