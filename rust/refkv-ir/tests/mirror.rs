//! The mirror's table (src/mirror.rs): written from an IR, read back with its column
//! runs expanded, it is the canonical IR, its invariants hold and its leaves rebuild
//! the source; rebuilt from what it loads, it is the same table entry for entry (the
//! canonical form is a fixed point); one test per way a table can be invalid, and one
//! per way a valid table can be non-canonical, which the validator flags.

use std::path::Path;
use refkv_ir::check::{check, finish, leaves};
use refkv_ir::ir::{Ir, GAP, NO_INDEX, STRUCT, VALUE};
use refkv_ir::mirror::{text_json, canonical, canonical_problem, load, mirror, ordered, pieces, shown, table, table_from_out, view_problem, write, Table};
use refkv_ir::out::Out;
use refkv_ir::run::{run_bytes, Format};

/// Every file profile's fixtures (the registry's, spec/zarrify/profiles/: fixtures/<p>,
/// the files of its suffixes), with its format.
fn fixtures() -> Vec<(Format, std::path::PathBuf)> {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../fixtures");
    let mut v = Vec::new();
    for e in refkv_ir::out::registry().iter().filter(|e| e["kind"] == "file") {
        let name = e["name"].as_str().unwrap();
        let f = Format::from_name(name).unwrap_or_else(|| panic!("{name}: no parser"));
        if f.store_run() {
            continue; // a file input read as a store run (VSI): tests/vsi.rs checks its mirrors
        }
        let suffixes: Vec<&str> = e["suffixes"].as_array().unwrap().iter().map(|s| s.as_str().unwrap()).collect();
        let mut ps: Vec<_> = std::fs::read_dir(root.join(name)).unwrap().map(|e| e.unwrap().path())
            .filter(|p| p.is_file() && suffixes.iter().any(|s| p.to_string_lossy().ends_with(s))).collect();
        ps.sort();
        v.extend(ps.into_iter().map(|p| (f, p)));
    }
    v
}

fn entry(out: &Out) -> impl Fn(&str) -> Option<Vec<u8>> + '_ {
    |k: &str| out.entries.iter().find(|e| e.0 == k).map(|e| e.1.clone())
}

#[test]
fn the_table_is_the_canonical_ir_rebuilds_the_source_and_is_a_fixed_point() {
    let mut folded_somewhere = false;
    let mut read_arrays = false;
    for (f, p) in fixtures() {
        let data = std::fs::read(&p).unwrap();
        let Ok((ir, _, _)) = run_bytes(f, &data) else { continue };
        let mut out = Out::new("u");
        let folded = mirror(&ir, &mut out, f.name()).unwrap();
        folded_somewhere |= folded.cruns > 0;
        read_arrays |= folded.element_columns > 0;
        let t = table_from_out(&out).unwrap();
        assert!(t.runs.is_empty(), "{p:?}");
        let mut read = |o: u64, n: u64| Ok(data[o as usize..(o + n) as usize].to_vec());
        let back = load(&t, &mut read).unwrap_or_else(|e| panic!("{p:?}: {e}"));
        assert_eq!(ordered(&back), ordered(&ir), "{p:?}");
        check(&back).unwrap();
        // the root: named "", no index, a struct in the source spanning it (§8.1
        // <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#81-elements>)
        assert_eq!((back.kind[0], back.name_of(0), back.nidx[0], back.space[0]), (STRUCT, String::new(), NO_INDEX, 0), "{p:?}");
        assert_eq!((back.start[0], back.len[0]), (0, data.len() as u64), "{p:?}");
        let mut rebuilt = Vec::new();
        for (o, n) in leaves(&back).unwrap() {
            rebuilt.extend_from_slice(&data[o as usize..(o + n) as usize]);
        }
        assert!(rebuilt == data, "{p:?}");
        assert_eq!(folded.rows, back.len() as u64);
        // the fixed point: the table rebuilt from what it loads is the same, byte for byte
        let y = canonical(&back).unwrap();
        let (again, _) = table(&y, f.name(), &|e| {
            let e = e as usize;
            Some(data[y.start[e] as usize..(y.start[e] + y.len[e]) as usize].to_vec())
        });
        let mut out2 = Out::new("u");
        write(&again, &mut out2, f.name(), false);
        for (k, v) in &out2.entries {
            assert!(entry(&out)(k).as_ref() == Some(v), "{p:?}: {k}");
        }
        assert_eq!(canonical_problem(&entry(&out), &mut read), None, "{p:?}");
        // the view the table and the source give is the stored one
        let keys: Vec<String> = out.entries.iter().map(|e| e.0.clone()).collect();
        assert_eq!(view_problem(&keys, &entry(&out), &mut read), None, "{p:?}");
    }
    assert!(folded_somewhere && read_arrays);
}

/// Every parser keeps every value the view shows: the run's fill batch, which reads
/// those it did not, is empty on every fixture; and the IR holds them all.
#[test]
fn every_value_the_view_shows_is_held() {
    for (f, p) in fixtures() {
        let data = std::fs::read(&p).unwrap();
        let Ok((ir, _, stats)) = run_bytes(f, &data) else { continue };
        assert_eq!(stats["fill"], serde_json::json!({"ranges": 0, "requests": 0, "bytes": 0}), "{p:?}");
        let y = canonical(&ir).unwrap();
        for i in 0..y.len() as u32 {
            if y.kind[i as usize] == VALUE && shown(&y, f.name(), i) {
                assert!(y.value_bytes(i).is_some(), "{p:?}: {} ({})", y.path(i), y.types.get(y.ty[i as usize]));
            }
        }
    }
}

/// A source of 30 bytes: the root and three gaps of 10 bytes, `gaps/0`, `gaps/10` and `gaps/20`.
fn three_ir() -> Ir {
    let mut ir = Ir::new(30);
    ir.budget = None;
    for k in 0..3u64 {
        ir.gap(0, "gaps/", (10 * k, 10)).unwrap();
        ir.nidx[k as usize + 1] = 10 * k;
    }
    ir.finished = true;
    ir.build_children();
    ir
}

/// The canonical mirror of `three_ir`, with `edit` applied to its table, as entries.
fn edited(edit: impl Fn(&mut Table), deflate: bool) -> Out {
    let y = canonical(&three_ir()).unwrap();
    let (mut t, _) = table(&y, "tiff", &|_| None);
    edit(&mut t);
    let mut out = Out::new("u");
    write(&t, &mut out, "tiff", deflate);
    out
}

fn problem(out: &Out) -> String {
    let mut read = |_: u64, n: u64| Ok(vec![0u8; n as usize]);
    canonical_problem(&entry(out), &mut read).expect("a problem")
}

#[test]
fn the_canonical_mirror_of_three_gaps_is_one_column_run() {
    let mut out = Out::new("u");
    mirror(&three_ir(), &mut out, "tiff").unwrap();
    let t = table_from_out(&out).unwrap();
    assert_eq!(t.kind.len(), 2);
    assert_eq!(t.cruns, vec![[1, 3, 1]]);
    let mut read = |_: u64, n: u64| Ok(vec![0u8; n as usize]);
    assert_eq!(canonical_problem(&entry(&out), &mut read), None);
}

/// Canonical form names every gap `gaps/<offset>` (conventions §8.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#81-elements>), however the
/// producer named it. refkv's producer fills the holes of a run's members with a run of gaps,
/// `gaps/6/0` to `gaps/6/2`; canonical form names them `gaps/6`, `gaps/16` and `gaps/26`, and
/// they fold with the gap after the run, `gaps/36`, into one column run whose name index is its
/// start (encoding 3).
#[test]
fn canonical_form_names_every_gap_by_its_offset() {
    let gap_paths = |ir: &Ir| -> Vec<String> {
        (0..ir.len() as u32).filter(|&i| ir.kind[i as usize] == GAP).map(|i| ir.path(i)).collect()
    };
    let mut ir = Ir::new(40);
    ir.budget = None;
    let d = ir.value(0, "d/", 0, "bytes[6]", 0, (0, 6), None).unwrap();
    ir.runs.push((d, 4, 10));
    finish(&mut ir).unwrap();
    assert_eq!(gap_paths(&ir), ["gaps/6/0", "gaps/36"]);
    assert_eq!(gap_paths(&canonical(&ir).unwrap()), ["gaps/6", "gaps/16", "gaps/26", "gaps/36"]);
    let mut out = Out::new("u");
    mirror(&ir, &mut out, "tiff").unwrap();
    let t = table_from_out(&out).unwrap();
    // stored: the root, d/0 and gaps/6 (expanded ids 0, 1 and 5), each of the last two member 0
    // of a column run
    assert_eq!(t.kind.len(), 3);
    assert_eq!(t.cruns, vec![[1, 4, 1], [5, 4, 1]]);
    assert!(t.columns.contains(&[1, 0, 0, 3, 0, 0]));
    let mut read = |_: u64, n: u64| Ok(vec![0u8; n as usize]);
    assert_eq!(canonical_problem(&entry(&out), &mut read), None);
    // a gap in each member of a run, named `pad` in member 0: `s/0/gaps/6` and `s/1/gaps/16`
    let mut ir = Ir::new(20);
    ir.budget = None;
    let s = ir.struct_(0, "s/", 0, 0, Some((0, 10))).unwrap();
    ir.value(s, "v", NO_INDEX, "bytes[6]", 0, (0, 6), None).unwrap();
    ir.gap(s, "pad", (6, 4)).unwrap();
    ir.runs.push((s, 2, 10));
    ir.build_children();
    assert_eq!(gap_paths(&canonical(&ir).unwrap()), ["s/0/gaps/6", "s/1/gaps/16"]);
    // a gap in a derived space, by its offset in the derived bytes: `z/gaps/4`
    let mut ir = Ir::new(10);
    ir.budget = None;
    let z = ir.derived(0, "z", 0, (0, 10), r#"{"size":8,"transform":"zlib"}"#).unwrap();
    ir.value(z, "a", NO_INDEX, "bytes[4]", z, (0, 4), None).unwrap();
    ir.add(GAP, z, "pad", NO_INDEX, "", z, Some((4, 4))).unwrap();
    ir.build_children();
    assert_eq!(gap_paths(&canonical(&ir).unwrap()), ["z/gaps/4"]);
}

/// The names hold of the gaps as canonical form names them (conventions §8.8
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#88-canonical-form>, step 1): a
/// gap `pad` at offset 10, beside a value `gaps/10`, has no canonical form.
#[test]
fn an_ir_whose_gaps_renamed_break_the_names_has_no_canonical_form() {
    let mut ir = Ir::new(20);
    ir.budget = None;
    ir.value(0, "gaps/", 10, "bytes[10]", 0, (0, 10), None).unwrap();
    ir.gap(0, "pad", (10, 10)).unwrap();
    ir.build_children();
    check(&ir).unwrap();
    assert_eq!(
        canonical(&ir).err().unwrap(),
        "element 1 (value \"gaps/10\") and element 2 (gap \"gaps/10\") have one path, \"gaps/10\""
    );
}

#[test]
fn flags_a_run() {
    let out = edited(|t| t.runs.push([1, 1, 0]), false);
    assert!(problem(&out).starts_with("a run (table code 0)"));
}

#[test]
fn flags_compressed_chunks() {
    assert!(problem(&edited(|_| {}, true)).starts_with("ir/rows: compressed chunks"));
}

#[test]
fn flags_unsorted_names() {
    let out = edited(|t| {
        t.names.reverse();
        for x in t.name.iter_mut() {
            *x = 1 - *x;
        }
    }, false);
    assert!(problem(&out).starts_with("the names are not sorted"));
}

#[test]
fn flags_siblings_out_of_order() {
    // gaps/0 and gaps/1 stored in the other order, unfolded
    let out = edited(|t| {
        t.cruns.clear();
        t.columns.clear();
        t.column_values.clear();
        t.kind = vec![0, 5, 5, 5];
        t.up = vec![0, 1, 2, 3];
        t.name = vec![0, 1, 1, 1];
        t.nidx = vec![u64::MAX, 1, 0, 2];
        t.ty = vec![0; 4];
        t.space = vec![0; 4];
        t.start = vec![0, -20, -20, 10];
        t.length = vec![30, 10, 10, 10];
    }, false);
    assert_eq!(problem(&out), "row 1 is out of canonical order");
}

#[test]
fn flags_a_foldable_run_left_unfolded() {
    let out = edited(|t| {
        t.cruns.clear();
        t.columns.clear();
        t.column_values.clear();
        t.kind = vec![0, 5, 5, 5];
        t.up = vec![0, 1, 2, 3];
        t.name = vec![0, 1, 1, 1];
        t.nidx = vec![u64::MAX, 0, 1, 2];
        t.ty = vec![0; 4];
        t.space = vec![0; 4];
        t.start = vec![0, -30, 0, 0];
        t.length = vec![30, 10, 10, 10];
    }, false);
    assert!(problem(&out).starts_with("stored row 2 is"));
}

#[test]
fn flags_a_stored_column_where_arithmetic_holds() {
    let out = edited(|t| {
        let c = t.columns.iter_mut().find(|c| c[3] == 0 && c[2] == 2).expect("an arithmetic start column");
        c[3] = 1;
        c[4] = t.column_values.len() as i64;
        c[5] = 0;
        t.column_values.extend([0, 10, 10]);
    }, false);
    assert!(problem(&out).starts_with("table row"));
}

/// A table of the root and three gaps of 10 bytes each, folded as one column run.
fn three() -> Table {
    Table {
        size: 30,
        kind: vec![0, 5],
        up: vec![0, 1],
        name: vec![0, 1],
        nidx: vec![u64::MAX, 0],
        ty: vec![0, 0],
        space: vec![0, 0],
        start: vec![0, -30],
        length: vec![30, 10],
        cruns: vec![[1, 3, 1]],
        columns: vec![[0, 0, 2, 0, 0, 10], [0, 0, 0, 0, 0, 1]],
        names: vec!["".into(), "gaps/".into()],
        types: vec!["".into()],
        ..Default::default()
    }
}

fn no_read(_: u64, _: u64) -> Result<Vec<u8>, String> {
    Err("no reads".into())
}

/// A table of a 10-byte source: the root and a derived element of its bytes, 8 bytes derived,
/// with the rows `extra` (kind, up, name, space, coded start, length) after them.
fn derived_table(extra: &[(u8, u32, i32, u32, i64, u64)]) -> Table {
    let mut t = Table {
        size: 10,
        kind: vec![0, 3],
        up: vec![0, 1],
        name: vec![0, 1],
        nidx: vec![u64::MAX, u64::MAX],
        ty: vec![0, 0],
        space: vec![0, 0],
        start: vec![0, -10],
        length: vec![10, 10],
        forms: vec![[1, 0]],
        form_text: vec![r#"{"size":8,"transform":"zlib"}"#.into()],
        names: vec!["".into(), "z".into(), "a".into(), "b".into()],
        types: vec!["".into()],
        ..Default::default()
    };
    for &(kind, up, name, space, start, length) in extra {
        t.kind.push(kind);
        t.up.push(up);
        t.name.push(name);
        t.nidx.push(u64::MAX);
        t.ty.push(0);
        t.space.push(space);
        t.start.push(start);
        t.length.push(length);
    }
    t
}

/// What a table may leave out (conventions §8.8
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#88-canonical-form>, steps 3 and 7):
/// a derived element's descendants, and an alias's target, directly or at the end of a chain.
#[test]
fn a_table_may_leave_out_derived_spaces_and_alias_targets() {
    let alias = (4, 2, 2, 0, 0, 0);
    let chained = (4, 3, 3, 0, 0, 0);
    for (extra, targets) in [
        (vec![], vec![]),
        (vec![alias], vec![[2, -1]]),
        (vec![alias, chained], vec![[2, -1], [3, 2]]),
    ] {
        let mut t = derived_table(&extra);
        t.targets = targets;
        let ir = load(&t, &mut no_read).unwrap();
        check(&ir).unwrap();
        assert_eq!(leaves(&ir).unwrap(), [(0, 10)]);
    }
}

#[test]
fn a_table_whose_derived_space_has_a_hole_is_invalid() {
    let ir = load(&derived_table(&[(1, 1, 2, 1, -10, 4)]), &mut no_read).unwrap();
    assert!(check(&ir).unwrap_err().starts_with("derived bytes [4, 8) of element 1"));
}

/// Step 3 leaves all of a derived element's descendants out or none: one with only empty
/// descendants left none of them out, and they do not cover it.
#[test]
fn a_table_whose_derived_space_has_only_empty_descendants_is_invalid() {
    let ir = load(&derived_table(&[(0, 1, 2, 1, 0, 0)]), &mut no_read).unwrap();
    assert!(check(&ir).unwrap_err().starts_with("derived bytes [0, 8) of element 1"));
}

#[test]
fn a_table_whose_alias_target_is_past_its_rows_is_invalid() {
    let mut t = derived_table(&[(4, 2, 2, 0, 0, 0)]);
    t.targets = vec![[2, 99]];
    assert_eq!(err(&t), "bad alias [2, 99]");
}

/// An IR a parser built has every descendant: only a table read back has left any out.
#[test]
fn an_ir_whose_derived_element_has_no_descendants_is_invalid() {
    let mut ir = Ir::new(10);
    ir.derived(0, "z", 0, (0, 10), r#"{"size":8,"transform":"zlib"}"#).unwrap();
    assert!(check(&ir).unwrap_err().starts_with("derived bytes [0, 8) of element 1"));
}

// ---- the tables a reader rejects
//
// Each sentence of conventions §8.2
// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#82-the-table> (the table) and §8.3
// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#83-column-runs> (column runs) that
// makes a table invalid, with the tests of its rejection: a sentence with `none` has no test yet.
// Reading (`table_from`) rejects what the description and the arrays say, loading (`load`) what
// the rows and the tables say, and the checker (`check`) what the elements they stand for break.
//
// | sentence | tests |
// |---|---|
// | the table: `version` is required and is `2`; a reader rejects another | `rejects_another_version`, `rejects_a_description_of_no_version` |
// | the table: `size` is required | none |
// | the table: `objects` is an integer | none |
// | the table: `elements` is the elements, every column run expanded | `rejects_a_description_whose_elements_are_not_the_expanded_rows`, `rejects_a_description_of_no_elements` |
// | the table: `rows` is the rows the table stores | `rejects_a_description_whose_rows_are_not_the_stored_rows`, `rejects_a_description_of_no_rows` |
// | the table: `names`, `types` and `forms` are required arrays of strings | none |
// | the table: `rows` has the shape `[8, m]`, `tables` the shape `[k, 7]` | `rejects_rows_of_another_shape`, `rejects_tables_of_another_shape` |
// | the table: the two arrays' data type, chunk shape and codecs | none |
// | the table: a row's space is 0 or the expanded id of a derived element | `rejects_a_space_that_is_no_row`, `rejects_a_space_past_32_bits`; none for an element that is not derived |
// | the table: a run's `count` is at least 1 | `rejects_a_run_of_no_members` |
// | the table: an alias's target is a row, or `-1` | `rejects_an_alias_whose_target_is_no_row`, `a_table_whose_alias_target_is_past_its_rows_is_invalid`, `rejects_an_alias_column_whose_target_is_no_row` |
// | the table: the alias rows name stored rows | `rejects_an_alias_row_of_a_row_that_is_not_stored`, `rejects_an_alias_row_of_no_row` |
// | the table: the form rows name stored rows | `rejects_a_form_row_of_a_row_that_is_not_stored` |
// | the table: the run rows name stored rows | `rejects_a_run_row_of_a_row_that_is_not_stored` |
// | the table: the column run rows name stored rows | `rejects_a_column_run_at_a_row_that_is_not_stored` |
// | the table: a row of another code makes the table invalid | `rejects_a_table_row_of_another_code` |
// | the table: a column's column run is one of the rows of code 3 | `rejects_a_column_of_no_column_run` |
// | column runs: a column's row offset is a row of member 0, its field one of 0 to 6 | `rejects_a_column_of_a_row_past_the_member` |
// | column runs: the encodings are 0 to 4 | `rejects_a_column_of_another_encoding` |
// | column runs: encoding 4's pieces are each of a member at least, and are the column run's members | `rejects_a_piece_of_no_member`, `rejects_pieces_short_of_the_members`, `rejects_pieces_past_the_members` |
// | column runs: encoding 2's row is a value in space 0, with a length | `rejects_a_column_of_a_row_that_is_no_value`, `rejects_a_column_of_no_row` |
// | column runs: encoding 2's row is of a type that is an array of unsigned integers | `rejects_a_column_of_a_row_that_is_no_array` |
// | column runs: encoding 2's row is not in a column run that has a column of encoding 2 | `rejects_a_column_of_a_row_in_a_run_that_reads_columns` |
// | column runs: encoding 3 is of the name index only | `rejects_encoding_3_of_a_field_other_than_the_name_index` |
// | limits: the expanded table has at most 2^22 + floor(Z / 4) rows | `rejects_stored_rows_past_the_budget`, `rejects_a_column_run_past_the_budget` |
// | limits: columns' lengths differ | `rejects_columns_of_different_lengths` |
// | limits: a column refers to values past `column_values` | `rejects_a_column_past_its_values`, `rejects_pieces_past_their_values` |
// | limits: a column refers to entries past its array | `rejects_a_column_past_its_array` |
// | limits: a column run's member 0 is not `size` rows of sibling subtrees | `rejects_a_column_run_that_is_not_a_subtree` |
// | limits: an up of 0 for a row other than the root, or larger than the row's id | `rejects_a_bad_parent` |
// | limits: kinds out of range | `rejects_a_bad_kind`, `rejects_a_kind_past_8_bits` |
// | limits: names out of range | `rejects_a_name_past_the_names`, `rejects_a_name_where_the_table_has_none` |
// | limits: types out of range | `rejects_a_type_past_the_types`, `rejects_a_type_column_past_the_types` |
// | limits: forms out of range | `rejects_a_form_row_of_no_form`, `rejects_a_form_column_that_is_no_form` |
// | limits: the expanded rows break the rules of the root or of the names | `flags_a_table_whose_root_is_named`, and each rule in tests/core.rs |
// | limits: the expanded table, every run expanded too, has more than `N` rows | `rejects_runs_past_the_budget` (and `an_ir_past_the_budget_only_by_its_left_out_derived_rows_is_valid`, a parser's IR) |
//
// Beside them, the runs of §8.1 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#81-elements>:
//
// | sentence | tests |
// |---|---|
// | runs do not nest | `rejects_a_run_inside_a_run` |
// | a run holds no derived element | `rejects_a_run_holding_a_derived_element`, `an_ir_whose_run_holds_a_derived_element_is_invalid` |
// | a run holds no alias | `rejects_a_run_holding_an_alias`, `an_ir_whose_run_holds_an_alias_is_invalid` |
// | a run holds no element in a derived space | `rejects_a_run_holding_an_element_in_a_derived_space`, `an_ir_whose_run_holds_an_element_in_a_derived_space_is_invalid` |
// | canonical form names every gap by its offset, then the names hold | `canonical_form_names_every_gap_by_its_offset`, `an_ir_whose_gaps_renamed_break_the_names_has_no_canonical_form` |
//
// And a store's limits (§8.9 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#89-stores>),
// in tests/store_mirror.rs.

fn err(t: &Table) -> String {
    match load(t, &mut no_read) {
        Ok(_) => panic!("loaded"),
        Err(e) => e,
    }
}

#[test]
fn a_column_run_expands() {
    let ir = load(&three(), &mut no_read).unwrap();
    assert_eq!(ir.len(), 4);
    assert_eq!(ir.start, vec![0, 0, 10, 20]);
    assert_eq!(ir.nidx[1..], [0, 1, 2]);
    check(&ir).unwrap();
}

/// A column's pieces, each as long as it can be from the first value on (conventions §8.8
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#88-canonical-form>, step 6).
#[test]
fn a_column_is_cut_into_its_longest_arithmetic_pieces() {
    for (v, want) in [
        (vec![5], vec![(5, 0, 1)]),
        (vec![5, 7], vec![(5, 2, 2)]),
        (vec![0, 10, 20, 100, 110, 120, 7], vec![(0, 10, 3), (100, 10, 3), (7, 0, 1)]),
        (vec![0, 10, 20, 30, 0, 0, 0], vec![(0, 10, 4), (0, 0, 3)]),
        (vec![i64::MAX, i64::MIN, i64::MIN + 1], vec![(i64::MAX, 1, 3)]),
    ] {
        assert_eq!(pieces(&v), want, "{v:?}");
    }
}

/// Units in runs of one stride, gaps between: their starts take encoding 4 where the pieces are
/// fewer than a third of the members, else encoding 1; either loads back to the same IR, and the
/// table is canonical.
#[test]
fn a_column_in_runs_of_one_stride_is_stored_as_pieces() {
    let run = |n: u64, at: u64| (0..n).map(move |j| at + 10 * j);
    let cases: [(Vec<u64>, Option<Vec<i64>>); 3] = [
        (run(20, 0).chain(run(20, 300)).collect(), Some(vec![0, 10, 20, 300, 10, 20])),
        (run(4, 0).chain(run(3, 100)).collect(), Some(vec![0, 10, 4, 100, 10, 3])),
        (run(3, 0).chain(run(3, 100)).collect(), None),
    ];
    for (starts, pieces) in cases {
        let mut ir = Ir::new(500);
        ir.budget = None;
        for (j, &at) in starts.iter().enumerate() {
            ir.data(0, "u/", j as u64, 0, (at, 10), r#"{"recipe":[["src",0,null]]}"#).unwrap();
        }
        finish(&mut ir).unwrap();
        let y = canonical(&ir).unwrap();
        let (t, _) = table(&y, "flac", &|_| None);
        let at = format!("{starts:?}: {:?}", t.columns);
        let start = t.columns.iter().find(|c| c[2] == 2).expect(&at);
        match &pieces {
            Some(want) => {
                assert_eq!(start[3], 4, "{at}");
                let (x, k) = (start[4] as usize, start[5] as usize);
                assert_eq!(&t.column_values[x..x + 3 * k], &want[..], "{at}");
            }
            None => assert_eq!(start[3], 1, "{at}"),
        }
        let back = load(&t, &mut no_read).unwrap();
        assert_eq!(ordered(&back), ordered(&y), "{at}");
        let mut out = Out::new("u");
        write(&t, &mut out, "flac", false);
        let data = vec![0u8; 500];
        assert_eq!(canonical_problem(&entry(&out), &mut |o, n| Ok(data[o as usize..(o + n) as usize].to_vec())), None, "{at}");
    }
}

#[test]
fn rejects_a_piece_of_no_member() {
    let mut t = three();
    t.columns = vec![[0, 0, 2, 4, 0, 2]];
    t.column_values = vec![0, 10, 3, 30, 0, 0];
    assert_eq!(err(&t), "column run 0: pieces that are not its members");
}

#[test]
fn rejects_pieces_short_of_the_members() {
    let mut t = three();
    t.columns = vec![[0, 0, 2, 4, 0, 1]];
    t.column_values = vec![0, 10, 2];
    assert_eq!(err(&t), "column run 0: pieces that are not its members");
}

#[test]
fn rejects_pieces_past_the_members() {
    let mut t = three();
    t.columns = vec![[0, 0, 2, 4, 0, 2]];
    t.column_values = vec![0, 10, 2, 20, 10, 2];
    assert_eq!(err(&t), "column run 0: pieces that are not its members");
}

/// Encoding 4's pieces, three values each, past `column_values`: too few values for the pieces it
/// names, and a negative count of pieces.
#[test]
fn rejects_pieces_past_their_values() {
    for (column, values) in [([0, 0, 2, 4, 0, 2], vec![0, 10, 3]), ([0, 0, 2, 4, 0, -1], vec![0, 10, 3]), ([0, 0, 2, 4, -1, 1], vec![0, 10, 3])] {
        let mut t = three();
        (t.columns, t.column_values) = (vec![column], values);
        assert_eq!(err(&t), "a column past its values", "{column:?}");
    }
}

#[test]
fn rejects_columns_of_different_lengths() {
    let mut t = three();
    t.up.pop();
    assert_eq!(err(&t), "columns of different lengths");
}

#[test]
fn rejects_a_column_run_past_the_budget() {
    let mut t = three();
    t.cruns = vec![[1, 1 << 40, 1]];
    assert!(err(&t).starts_with("budget: more than"));
}

#[test]
fn rejects_a_column_past_its_values() {
    let mut t = three();
    t.columns = vec![[0, 0, 2, 1, 0, 0]];
    t.column_values = vec![0, 10];
    assert_eq!(err(&t), "a column past its values");
}

#[test]
fn rejects_a_column_of_a_row_that_is_no_array() {
    let mut t = three();
    t.columns = vec![[0, 0, 2, 2, 0, 0]];
    assert_eq!(err(&t), "a column of a row that is no unsigned array");
}

#[test]
fn rejects_a_column_of_a_row_in_a_run_that_reads_columns() {
    let mut t = three();
    t.columns = vec![[0, 0, 2, 2, 1, 0]];
    assert_eq!(err(&t), "a column of a row in a column run that reads columns");
}

#[test]
fn rejects_a_column_of_no_column_run() {
    let mut t = three();
    t.columns = vec![[5, 0, 2, 0, 0, 10]];
    assert_eq!(err(&t), "a column of no column run");
}

#[test]
fn rejects_a_bad_parent() {
    let mut t = three();
    t.up[1] = 2;
    assert_eq!(err(&t), "row 1: parent -1");
}

#[test]
fn rejects_a_bad_kind() {
    let mut t = three();
    t.kind[1] = 9;
    assert_eq!(err(&t), "row 1: kind 9");
}

#[test]
fn rejects_a_column_run_that_is_not_a_subtree() {
    let mut t = three();
    t.cruns = vec![[1, 3, 2]];
    assert_eq!(err(&t), "column run 0: member 0 is not 2 rows of sibling subtrees");
}

/// A table of `m` stored rows and no column run, of a source of no bytes: the root and
/// `m - 1` empty structs `s/0`, `s/1`, ….
fn flat(m: usize) -> Table {
    let mut t = Table {
        kind: vec![0; m],
        up: (0..m as u32).collect(),
        name: vec![1; m],
        nidx: (0..m as u64).map(|k| k.wrapping_sub(1)).collect(),
        ty: vec![0; m],
        space: vec![0; m],
        start: vec![0; m],
        length: vec![0; m],
        names: vec!["".into(), "s/".into()],
        types: vec!["".into()],
        ..Default::default()
    };
    t.name[0] = 0;
    t
}

/// The limit holds for the stored rows too: a table of no column run is not past it unseen.
#[test]
fn rejects_stored_rows_past_the_budget() {
    assert_eq!(err(&flat((1 << 22) + 1)), "budget: more than 4194304 rows");
}

/// `three()` as a mirror's entries, `edit` applied to its description (`I`, conventions §8.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#82-the-table>).
fn described(edit: impl Fn(&mut serde_json::Value)) -> Out {
    let mut out = Out::new("u");
    write(&three(), &mut out, "tiff", false);
    let e = out.entries.iter_mut().find(|e| e.0 == "refkv_source/zarr.json").unwrap();
    let mut doc: serde_json::Value = serde_json::from_slice(&e.1).unwrap();
    edit(&mut doc["attributes"]["refkv_zarrified"]["tiff"]["ir"]);
    e.1 = serde_json::to_vec(&doc).unwrap();
    out
}

/// `three()` as a mirror's entries, with `value` in column `column` of its stored row 1 (`rows`
/// is `[8, 2]`, a column after another).
fn with_cell(column: usize, value: i64) -> Out {
    let mut out = Out::new("u");
    write(&three(), &mut out, "tiff", false);
    let e = out.entries.iter_mut().find(|e| e.0 == "refkv_source/ir/rows/c/0/0").unwrap();
    assert_eq!(e.1.len(), 8 * 8 * 2);
    let at = 8 * (2 * column + 1);
    e.1[at..at + 8].copy_from_slice(&value.to_le_bytes());
    out
}

/// What reading the entries' table says: `table_from`'s error.
fn unread(out: &Out) -> String {
    match table_from_out(out) {
        Ok(_) => panic!("read"),
        Err(e) => e,
    }
}

/// A name of 2^32 + 1 is not name 1: the column is checked as it is stored, 64 bits wide.
#[test]
fn rejects_a_name_past_the_names() {
    assert_eq!(unread(&with_cell(2, (1 << 32) + 1)), "a row's name out of range");
}

#[test]
fn rejects_a_type_past_the_types() {
    assert_eq!(unread(&with_cell(4, 1 << 32)), "a row's type out of range");
}

#[test]
fn rejects_a_space_past_32_bits() {
    assert_eq!(unread(&with_cell(5, 1 << 32)), "a row's space out of range");
}

#[test]
fn rejects_another_version() {
    assert_eq!(unread(&described(|d| d["version"] = serde_json::json!(3))), "version 3, not 2");
}

#[test]
fn rejects_a_description_of_no_version() {
    assert_eq!(unread(&described(|d| { d.as_object_mut().unwrap().remove("version"); })), "no version");
}

#[test]
fn rejects_a_description_whose_rows_are_not_the_stored_rows() {
    assert_eq!(unread(&described(|d| d["rows"] = serde_json::json!(3))), "the description has 3 rows, ir/rows 2");
}

#[test]
fn rejects_a_description_of_no_rows() {
    assert_eq!(unread(&described(|d| { d.as_object_mut().unwrap().remove("rows"); })), "no rows");
}

#[test]
fn rejects_a_description_whose_elements_are_not_the_expanded_rows() {
    assert_eq!(unread(&described(|d| d["elements"] = serde_json::json!(2))), "the description has 2 elements, the table expands to 4");
}

#[test]
fn rejects_a_description_of_no_elements() {
    assert_eq!(unread(&described(|d| { d.as_object_mut().unwrap().remove("elements"); })), "no elements");
}

/// `three()` and a third stored row, an alias `x` of the root: element 4, after the column
/// run's three members (elements 1 to 3, of which only element 1 is stored).
fn three_and_an_alias() -> Table {
    let mut t = three();
    t.kind.push(4);
    t.up.push(4);
    t.name.push(2);
    t.nidx.push(u64::MAX);
    t.ty.push(0);
    t.space.push(0);
    t.start.push(0);
    t.length.push(0);
    t.names.push("x".into());
    t.targets = vec![[4, 0]];
    t
}

/// An alias row names a stored row: element 3 is member 2 of the column run, which is not stored.
#[test]
fn rejects_an_alias_row_of_a_row_that_is_not_stored() {
    let mut t = three_and_an_alias();
    t.targets = vec![[3, 0]];
    assert_eq!(err(&t), "bad alias [3, 0]");
}

#[test]
fn rejects_an_alias_row_of_no_row() {
    let mut t = three_and_an_alias();
    t.targets = vec![[(1 << 32) + 4, 0]];
    assert_eq!(err(&t), "bad alias [4294967300, 0]");
}

#[test]
fn rejects_an_alias_whose_target_is_no_row() {
    let mut t = three_and_an_alias();
    t.targets = vec![[4, 5]];
    assert_eq!(err(&t), "bad alias [4, 5]");
    t.targets = vec![[4, -2]];
    assert_eq!(err(&t), "bad alias [4, -2]");
}

/// A target column holds a row or -1, for none: 4 is past the rows, and -2 is neither.
#[test]
fn rejects_an_alias_column_whose_target_is_no_row() {
    for (x, y) in [(3, 1), (-1, -1)] {
        let mut t = three();
        t.columns.push([0, 0, 5, 0, x, y]); // targets 3, 4, 5, or -1, -2, -3, for the three members
        assert_eq!(err(&t), "a target column out of range");
    }
}

#[test]
fn rejects_a_form_row_of_a_row_that_is_not_stored() {
    let mut t = three();
    t.form_text = vec!["{}".into()];
    t.forms = vec![[2, 0]];
    assert_eq!(err(&t), "bad form [2, 0]");
}

#[test]
fn rejects_a_run_row_of_a_row_that_is_not_stored() {
    let mut t = three();
    t.runs = vec![[2, 1, 0]];
    assert_eq!(err(&t), "bad run [2, 1, 0]");
}

/// A column run of one member adds no rows, so the count of rows does not give it away.
#[test]
fn rejects_a_column_run_at_a_row_that_is_not_stored() {
    let mut t = three();
    t.cruns.push([2, 1, 1]);
    assert_eq!(err(&t), "column run 1 at row 2");
}

/// A table of a 40-byte source: the root, a struct `s/0` and in it a gap `gaps/0` of 10 bytes,
/// with the runs `runs` (root, count, stride).
fn two_deep(runs: &[[i64; 3]]) -> Table {
    Table {
        size: 40,
        kind: vec![0, 0, 5],
        up: vec![0, 1, 1],
        name: vec![0, 2, 1],
        nidx: vec![u64::MAX, 0, 0],
        ty: vec![0, 0, 0],
        space: vec![0, 0, 0],
        start: vec![0, 0, -40],
        length: vec![40, 0, 10],
        runs: runs.to_vec(),
        names: vec!["".into(), "gaps/".into(), "s/".into()],
        types: vec!["".into()],
        ..Default::default()
    }
}

/// Runs do not nest (conventions §8.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#81-elements>).
#[test]
fn rejects_a_run_inside_a_run() {
    assert_eq!(err(&two_deep(&[[1, 2, 20], [2, 2, 10]])), "the run at row 2 is inside the run at row 1");
}

/// `derived_table(extra)` (the root, a derived element `z` and the rows `extra`) whose element
/// `root` is a run of two members, `stride` bytes apart, with the alias rows `targets`.
fn run_in_derived_table(extra: &[(u8, u32, i32, u32, i64, u64)], root: usize, stride: i64, targets: Vec<[i64; 2]>) -> Table {
    let mut t = derived_table(extra);
    t.nidx[root] = 0;
    t.runs = vec![[root as i64, 2, stride]];
    t.targets = targets;
    t
}

/// A run's members are copies of member 0 (conventions §8.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#81-elements>), so it holds no
/// derived element, no alias and no element in a derived space.
#[test]
fn rejects_a_run_holding_a_derived_element() {
    assert_eq!(err(&run_in_derived_table(&[], 1, 10, vec![])), "the run at row 1 holds a derived element (row 1)");
}

#[test]
fn rejects_a_run_holding_an_alias() {
    let t = run_in_derived_table(&[(4, 2, 2, 0, 0, 0)], 2, 0, vec![[2, 0]]);
    assert_eq!(err(&t), "the run at row 2 holds an alias (row 2)");
}

#[test]
fn rejects_a_run_holding_an_element_in_a_derived_space() {
    let t = run_in_derived_table(&[(1, 1, 2, 1, -10, 4)], 2, 4, vec![]);
    assert_eq!(err(&t), "the run at row 2 holds an element in a derived space (row 2)");
}

/// A 20-byte source whose struct `s/0`, at `[0, 10)`, is a run of two members 10 bytes apart,
/// with what `fill` adds.
fn run_of(fill: impl Fn(&mut Ir, u32)) -> Ir {
    let mut ir = Ir::new(20);
    ir.budget = None;
    let s = ir.struct_(0, "s/", 0, 0, Some((0, 10))).unwrap();
    ir.runs.push((s, 2, 10));
    fill(&mut ir, s);
    ir.build_children();
    ir
}

#[test]
fn an_ir_whose_run_holds_a_derived_element_is_invalid() {
    let ir = run_of(|ir, s| {
        ir.derived(s, "z", 0, (0, 10), r#"{"transform":"zlib"}"#).unwrap();
    });
    assert_eq!(check(&ir).unwrap_err(), "the run at row 1 holds a derived element (row 2)");
    assert_eq!(canonical(&ir).err().unwrap(), "the run at row 1 holds a derived element (row 2)");
}

#[test]
fn an_ir_whose_run_holds_an_alias_is_invalid() {
    let ir = run_of(|ir, s| {
        ir.alias(s, "a", NO_INDEX, 0).unwrap();
    });
    assert_eq!(check(&ir).unwrap_err(), "the run at row 1 holds an alias (row 2)");
    assert_eq!(canonical(&ir).err().unwrap(), "the run at row 1 holds an alias (row 2)");
}

#[test]
fn an_ir_whose_run_holds_an_element_in_a_derived_space_is_invalid() {
    let mut ir = Ir::new(20);
    ir.budget = None;
    let z = ir.derived(0, "z", 0, (0, 20), r#"{"size":8,"transform":"zlib"}"#).unwrap();
    let v = ir.value(z, "v/", 0, "u4", z, (0, 4), None).unwrap();
    ir.runs.push((v, 2, 4));
    ir.build_children();
    assert_eq!(check(&ir).unwrap_err(), "the run at row 2 holds an element in a derived space (row 2)");
    assert_eq!(canonical(&ir).err().unwrap(), "the run at row 2 holds an element in a derived space (row 2)");
}

/// A parser's IR counts its rows outside derived spaces, as the producer does before canonical form
/// leaves derived descendants out (conventions §8.8
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#88-canonical-form>, step 3): an IR
/// of `2^20 + 1` structs in a derived space and a run that brings the rows outside it to `N = 2^22`
/// passes `check`, and the producer mirrors it in `N` rows.
#[test]
fn an_ir_past_the_budget_only_by_its_left_out_derived_rows_is_valid() {
    let mut ir = Ir::new(0);
    ir.budget = None;
    let z = ir.add(refkv_ir::ir::DERIVED, 0, "z", NO_INDEX, "", 0, None).unwrap();
    for k in 0..(1u64 << 20) + 1 {
        ir.struct_(z, "d/", k, z, None).unwrap();
    }
    let s = ir.struct_(0, "s/", 0, 0, None).unwrap();
    ir.runs.push((s, (1 << 22) - 2, 0));
    ir.build_children();
    check(&ir).unwrap();
    let mut out = Out::new("u");
    assert_eq!(mirror(&ir, &mut out, "tiff").unwrap().rows, 1 << 22);
    ir.runs[0].1 += 1;
    assert_eq!(check(&ir).unwrap_err(), "budget: more than 4194304 rows");
}

/// The limit of conventions §8.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#83-column-runs> counts the rows
/// of every run expanded: a run of 2^40 empty structs, and one of 2^21 whose members are two rows
/// each (a struct and an empty struct in it), so that neither its members nor its 3 stored rows are
/// past `N = 2^22`, but the `3 + (2^21 - 1) × 2 = N + 1` rows it expands to are (one member fewer:
/// `N - 1`).
#[test]
fn rejects_runs_past_the_budget() {
    let run = |count: i64, child: bool| Table {
        size: 0,
        kind: if child { vec![0, 0, 0] } else { vec![0, 0] },
        up: if child { vec![0, 1, 1] } else { vec![0, 1] },
        name: if child { vec![0, 1, 1] } else { vec![0, 1] },
        nidx: if child { vec![u64::MAX, 0, 0] } else { vec![u64::MAX, 0] },
        ty: if child { vec![0; 3] } else { vec![0; 2] },
        space: if child { vec![0; 3] } else { vec![0; 2] },
        start: if child { vec![0; 3] } else { vec![0; 2] },
        length: if child { vec![0; 3] } else { vec![0; 2] },
        runs: vec![[1, count, 0]],
        names: vec!["".into(), "s/".into()],
        types: vec!["".into()],
        ..Default::default()
    };
    assert_eq!(err(&run(1 << 40, false)), "budget: more than 4194304 rows");
    assert_eq!(err(&run(1 << 21, true)), "budget: more than 4194304 rows");
    load(&run((1 << 21) - 1, true), &mut no_read).unwrap();
}

#[test]
fn rejects_a_run_of_no_members() {
    let mut t = three();
    t.runs = vec![[1, 0, 0]];
    assert_eq!(err(&t), "bad run [1, 0, 0]");
}

#[test]
fn rejects_a_form_row_of_no_form() {
    let mut t = three();
    t.forms = vec![[1, 0]];
    assert_eq!(err(&t), "bad form [1, 0]");
}

#[test]
fn rejects_a_name_where_the_table_has_none() {
    let mut t = three();
    t.names.clear();
    assert_eq!(err(&t), "row 0: name or type out of range");
}

#[test]
fn rejects_a_type_column_past_the_types() {
    let mut t = three();
    t.columns.push([0, 0, 1, 0, 0, 1]); // types 0, 1, 2 for the three members
    assert_eq!(err(&t), "a type column out of range");
}

/// A form column holds a form or -1, for none: 1 is past the forms, and -2 is neither.
#[test]
fn rejects_a_form_column_that_is_no_form() {
    for (x, y) in [(0, 1), (-1, -1)] {
        let mut t = three();
        t.form_text = vec!["{}".into()];
        t.columns.push([0, 0, 4, 0, x, y]); // forms 0, 1, 2, or -1, -2, -3, for the three members
        assert_eq!(err(&t), "a form column out of range");
    }
}

#[test]
fn rejects_a_column_of_a_row_past_the_member() {
    let mut t = three();
    t.columns = vec![[0, 1, 2, 0, 0, 10]];
    assert_eq!(err(&t), "column run 0: bad column [0, 1, 2, 0, 0, 10]");
}

#[test]
fn rejects_a_column_of_another_encoding() {
    let mut t = three();
    t.columns = vec![[0, 0, 2, 7, 0, 10]];
    assert_eq!(err(&t), "column run 0: encoding 7");
}

#[test]
fn rejects_encoding_3_of_a_field_other_than_the_name_index() {
    let mut t = three();
    t.columns = vec![[0, 0, 2, 3, 0, 0]];
    assert_eq!(err(&t), "column run 0: encoding 3 of field 2");
}

#[test]
fn rejects_a_kind_past_8_bits() {
    assert_eq!(unread(&with_cell(0, 256)), "a row's kind or parent out of range");
}

/// A space is 0 or an element's id.
#[test]
fn rejects_a_space_that_is_no_row() {
    let mut t = three();
    t.space[1] = 4;
    assert_eq!(err(&t), "row 1: space 4");
}

/// `three()` as a mirror's entries, the document of its array `ir/<name>` edited.
fn with_array(name: &str, edit: impl Fn(&mut serde_json::Value)) -> Out {
    let mut out = Out::new("u");
    write(&three(), &mut out, "tiff", false);
    let key = format!("refkv_source/ir/{name}/zarr.json");
    let e = out.entries.iter_mut().find(|e| e.0 == key).unwrap();
    let mut doc: serde_json::Value = serde_json::from_slice(&e.1).unwrap();
    edit(&mut doc);
    e.1 = serde_json::to_vec(&doc).unwrap();
    out
}

/// `rows` is `[8, m]`: the 16 values of `three()` read as `[4, 4]` are no table.
#[test]
fn rejects_rows_of_another_shape() {
    let out = with_array("rows", |d| {
        d["shape"] = serde_json::json!([4, 4]);
        d["chunk_grid"]["configuration"]["chunk_shape"] = serde_json::json!([4, 4]);
    });
    assert_eq!(unread(&out), "ir/rows: shape [4, 4], not [8, m]");
}

/// `tables` is `[k, 7]`: the 21 values of `three()` read as `[7, 3]` are no table rows.
#[test]
fn rejects_tables_of_another_shape() {
    let out = with_array("tables", |d| {
        d["shape"] = serde_json::json!([7, 3]);
        d["chunk_grid"]["configuration"]["chunk_shape"] = serde_json::json!([7, 3]);
    });
    assert_eq!(unread(&out), "ir/tables: shape [7, 3], not [k, 7]");
}

#[test]
fn rejects_a_table_row_of_another_code() {
    let mut out = Out::new("u");
    write(&three(), &mut out, "tiff", false);
    let e = out.entries.iter_mut().find(|e| e.0 == "refkv_source/ir/tables/c/0/0").unwrap();
    e.1[..8].copy_from_slice(&9i64.to_le_bytes());
    assert_eq!(unread(&out), "a table row of kind 9");
}

/// A table of a 10-byte source: the root, `counts` (a value of the type `<u2[2]`, 4 bytes) and
/// three gaps of 2 bytes folded as one column run, whose lengths are a column read from row `x`.
fn arrayed(x: i64) -> Table {
    Table {
        size: 10,
        kind: vec![0, 1, 5],
        up: vec![0, 1, 2],
        name: vec![0, 1, 2],
        nidx: vec![u64::MAX, u64::MAX, 0],
        ty: vec![0, 1, 0],
        space: vec![0; 3],
        start: vec![0, -10, 0],
        length: vec![10, 4, 2],
        cruns: vec![[2, 3, 1]],
        columns: vec![[0, 0, 3, 2, x, 0]],
        names: vec!["".into(), "counts".into(), "gaps/".into()],
        types: vec!["".into(), "<u2[2]".into()],
        ..Default::default()
    }
}

/// What loading `t` says, the source being all zeros.
fn err_of_zeros(t: &Table) -> String {
    match load(t, &mut |_, n| Ok(vec![0u8; n as usize])) {
        Ok(_) => panic!("loaded"),
        Err(e) => e,
    }
}

/// Three members, and an array of two entries.
#[test]
fn rejects_a_column_past_its_array() {
    assert_eq!(err_of_zeros(&arrayed(1)), "a column past its array");
}

#[test]
fn rejects_a_column_of_a_row_that_is_no_value() {
    let mut t = arrayed(1);
    t.kind[1] = 5;
    assert_eq!(err_of_zeros(&t), "a column of a row that is no value in the source");
}

#[test]
fn rejects_a_column_of_no_row() {
    assert_eq!(err_of_zeros(&arrayed(99)), "a column of no row");
}

/// The tables the checks above leave alone: a run and a form at a stored row, sibling runs,
/// an alias whose target is a row that is not stored or is left out, a name the description
/// has twice, and a description that says what its table holds.
#[test]
fn valid_tables_load() {
    // one run of four gaps; and a run of two structs, each of one gap
    for (runs, size) in [([[2, 4, 10]], 40), ([[1, 2, 10]], 20)] {
        let mut t = two_deep(&runs);
        t.size = size;
        t.length[0] = size;
        t.start[2] = -(size as i64);
        let ir = load(&t, &mut no_read).unwrap();
        assert_eq!(ir.runs.len(), 1);
        check(&ir).unwrap();
    }
    // sibling runs: `gaps/0` and `gaps/1` of 10 bytes, then `tail/0` and `tail/1` of 5
    let t = Table {
        size: 30,
        kind: vec![0, 5, 5],
        up: vec![0, 1, 2],
        name: vec![0, 1, 2],
        nidx: vec![u64::MAX, 0, 0],
        ty: vec![0; 3],
        space: vec![0; 3],
        start: vec![0, -30, 10],
        length: vec![30, 10, 5],
        runs: vec![[1, 2, 10], [2, 2, 5]],
        names: vec!["".into(), "gaps/".into(), "tail/".into()],
        types: vec!["".into()],
        ..Default::default()
    };
    let ir = load(&t, &mut no_read).unwrap();
    assert_eq!(leaves(&ir).unwrap(), [(0, 30)]);
    check(&ir).unwrap();
    // an alias row names a stored row; its target is any row, or none
    for target in [0, 3, 4, -1] {
        let mut t = three_and_an_alias();
        t.targets = vec![[4, target]];
        let ir = load(&t, &mut no_read).unwrap();
        assert_eq!(ir.target(4) as i64, if target < 0 { u32::MAX as i64 } else { target });
    }
    // a form row at a stored row, in member 0 of a column run
    let mut t = three();
    t.form_text = vec!["{}".into()];
    t.forms = vec![[1, 0]];
    let ir = load(&t, &mut no_read).unwrap();
    assert_eq!((1..4).map(|i| ir.form(i)).collect::<Vec<_>>(), [Some("{}"); 3]);
    // a form column and a target column of -1 for a member: none
    let mut t = three();
    t.form_text = vec!["{}".into()];
    t.forms = vec![[1, 0]];
    t.targets = vec![[1, 0]];
    t.columns.push([0, 0, 4, 1, 0, 0]); // forms 0, -1, -1, stored as differences
    t.columns.push([0, 0, 5, 1, 3, 0]); // targets 0, -1, -1
    t.column_values = vec![0, -1, 0, 0, -1, 0];
    let ir = load(&t, &mut no_read).unwrap();
    assert_eq!((1..4).map(|i| ir.form(i)).collect::<Vec<_>>(), [Some("{}"), None, None]);
    assert_eq!((1..4).map(|i| ir.target(i)).collect::<Vec<_>>(), [0, u32::MAX, u32::MAX]);
    // a name the description has twice is one name, whichever place a row names it by
    let mut t = three();
    t.names = vec!["".into(), "".into(), "gaps/".into(), "gaps/".into()];
    for name in [2, 3] {
        t.name[1] = name;
        let ir = load(&t, &mut no_read).unwrap();
        assert_eq!((1..4).map(|i| ir.name_of(i)).collect::<Vec<_>>(), ["gaps/0", "gaps/1", "gaps/2"]);
        check(&ir).unwrap();
    }
    // a description that says what the table holds, and cells in range
    for out in [described(|_| {}), with_cell(2, 1), with_cell(4, 0), with_cell(5, 0)] {
        let ir = load(&table_from_out(&out).unwrap(), &mut no_read).unwrap();
        assert_eq!(ir.len(), 4);
        check(&ir).unwrap();
    }
}

/// A source of 4 bytes whose IR stands for 4,200,000 elements (a run of empty
/// structs): its mirror would have more rows than 2^22 + 4 / 4, so it is rejected,
/// before the run is expanded.
#[test]
fn rejects_a_mirror_past_the_row_budget() {
    let mut ir = Ir::new(4);
    ir.budget = None;
    ir.gap(0, "gaps/", (0, 4)).unwrap();
    let s = ir.struct_(0, "s", 0, 0, None).unwrap();
    ir.runs.push((s, 4_200_000, 0));
    ir.finished = true;
    ir.build_children();
    let mut out = Out::new("u");
    assert_eq!(mirror(&ir, &mut out, "tiff").unwrap_err(), "budget: the mirror would have more than 4194305 rows");
}

#[test]
fn the_mirror_of_an_ir_whose_names_break_the_rules_is_a_rejection() {
    // two empty structs `s`: one path for two elements
    let mut ir = Ir::new(10);
    ir.budget = None;
    ir.gap(0, "gaps/", (0, 10)).unwrap();
    ir.struct_(0, "s", u64::MAX, 0, None).unwrap();
    ir.struct_(0, "s", u64::MAX, 0, None).unwrap();
    ir.finished = true;
    ir.build_children();
    let mut out = Out::new("u");
    assert_eq!(
        mirror(&ir, &mut out, "tiff").unwrap_err(),
        "element 2 (struct \"s\") and element 3 (struct \"s\") have one path, \"s\""
    );
}

#[test]
fn flags_a_table_whose_root_is_named() {
    // `names[0]`, the root's name, is "!" (still sorted first)
    let out = edited(|t| t.names[0] = "!".into(), false);
    assert_eq!(problem(&out), "invalid: the root (element 0) is named \"!\", not \"\"");
    let t = table_from_out(&out).unwrap();
    let back = load(&t, &mut no_read).unwrap();
    assert_eq!(check(&back).unwrap_err(), "the root (element 0) is named \"!\", not \"\"");
}

/// A fixture's output, its view document at `group` edited by `edit`, and what the
/// validator's view check says of it.
fn view_edited(fixture: &str, group: &str, edit: impl Fn(&mut serde_json::Value)) -> String {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../fixtures/tiff").join(fixture);
    let data = std::fs::read(path).unwrap();
    let (ir, _, _) = run_bytes(Format::Tiff, &data).unwrap();
    let mut out = Out::new("u");
    mirror(&ir, &mut out, "tiff").unwrap();
    let key = format!("refkv_source/{group}/zarr.json");
    let e = out.entries.iter_mut().find(|e| e.0 == key).unwrap();
    let mut doc: serde_json::Value = serde_json::from_slice(&e.1).unwrap();
    edit(&mut doc["attributes"]["refkv_zarrified"]["tiff"]);
    e.1 = serde_json::to_vec(&doc).unwrap();
    let keys: Vec<String> = out.entries.iter().map(|e| e.0.clone()).collect();
    let mut read = |o: u64, n: u64| Ok(data[o as usize..(o + n) as usize].to_vec());
    view_problem(&keys, &entry(&out), &mut read).expect("a problem")
}

#[test]
fn flags_a_view_missing_a_value() {
    let p = view_edited("jpeg_gray.tif", "tree", |d| {
        d["header"].as_object_mut().unwrap().remove("magic");
    });
    assert_eq!(p, r#"view tree: member ["header"]["magic"] is missing"#);
}

#[test]
fn flags_a_view_with_an_extra_value() {
    let p = view_edited("jpeg_gray.tif", "tree", |d| d["header"]["extra"] = serde_json::json!(1));
    assert_eq!(p, r#"view tree: member ["header"]["extra"] is not in the rebuilt view"#);
}

#[test]
fn flags_a_wrong_partial() {
    let p = view_edited("jpeg_gray.tif", "tree", |d| d["$partial"] = serde_json::json!(true));
    assert_eq!(p, r#"view tree: member ["$partial"] is not in the rebuilt view"#);
}

#[test]
fn flags_a_value_shown_above_the_size_limit() {
    // a source of one value of 2000 bytes, past the 1024 the view shows: a reference to its row
    let data = vec![7u8; 2000];
    let mut ir = Ir::new(2000);
    ir.value(0, "big", u64::MAX, "u1[2000]", 0, (0, 2000), None).unwrap();
    ir.finished = true;
    ir.build_children();
    let mut out = Out::new("u");
    mirror(&ir, &mut out, "tiff").unwrap();
    let e = out.entries.iter_mut().find(|e| e.0 == "refkv_source/tree/zarr.json").unwrap();
    let mut doc: serde_json::Value = serde_json::from_slice(&e.1).unwrap();
    assert_eq!(doc["attributes"]["refkv_zarrified"]["tiff"]["big"], serde_json::json!({"$vz": "element", "id": 1}));
    doc["attributes"]["refkv_zarrified"]["tiff"]["big"] = serde_json::json!(vec![7; 2000]);
    e.1 = serde_json::to_vec(&doc).unwrap();
    let keys: Vec<String> = out.entries.iter().map(|e| e.0.clone()).collect();
    let mut read = |o: u64, n: u64| Ok(data[o as usize..(o + n) as usize].to_vec());
    let p = view_problem(&keys, &entry(&out), &mut read).unwrap();
    assert!(p.starts_with(r#"view tree: member ["big"] is [7,7,"#) && p.ends_with(r#"where the rebuilt view has {"$vz":"element","id":1}"#), "{p}");
}

/// Integers in the view (conventions §8.7
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#87-the-view>): exact JSON
/// integers whatever their magnitude, as in §6
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#6-source-metadata-as-json>;
/// and the validator's rebuild agrees.
#[test]
fn integers_show_exactly_whatever_their_magnitude() {
    let mut data = Vec::new();
    for v in [u64::MAX, i64::MIN as u64, (1 << 53) + 1, 7] {
        data.extend(v.to_le_bytes());
    }
    let mut ir = Ir::new(data.len() as u64);
    for (k, (name, ty)) in [("u", "u8"), ("i", "i8"), ("past", "u8"), ("small", "i8")].into_iter().enumerate() {
        ir.value(0, name, u64::MAX, ty, 0, (8 * k as u64, 8), Some(&data[8 * k..8 * k + 8])).unwrap();
    }
    ir.finished = true;
    ir.build_children();
    let mut out = Out::new("u");
    mirror(&ir, &mut out, "tiff").unwrap();
    let text = String::from_utf8(entry(&out)("refkv_source/tree/zarr.json").unwrap()).unwrap();
    assert!(text.contains(r#""tiff":{"u":18446744073709551615,"i":-9223372036854775808,"past":9007199254740993,"small":7}"#), "{text}");
    let keys: Vec<String> = out.entries.iter().map(|e| e.0.clone()).collect();
    let mut read = |o: u64, n: u64| Ok(data[o as usize..(o + n) as usize].to_vec());
    assert_eq!(view_problem(&keys, &entry(&out), &mut read), None);
}

/// The validator compares a view's numbers by value (an integer exactly, a float by its binary64
/// value): a stored float written another way (`1.50`, `15e-1`) is the rebuilt one; another value
/// is not.
#[test]
fn the_view_s_floats_compare_by_value() {
    let data = 1.5f64.to_le_bytes().to_vec();
    let mut ir = Ir::new(8);
    ir.value(0, "x", u64::MAX, "f8", 0, (0, 8), Some(&data)).unwrap();
    ir.finished = true;
    ir.build_children();
    let mut out = Out::new("u");
    mirror(&ir, &mut out, "tiff").unwrap();
    let keys: Vec<String> = out.entries.iter().map(|e| e.0.clone()).collect();
    for (literal, same) in [("1.50", true), ("15e-1", true), ("1.25", false)] {
        let mut changed = Out::new("u");
        mirror(&ir, &mut changed, "tiff").unwrap();
        let e = changed.entries.iter_mut().find(|e| e.0 == "refkv_source/tree/zarr.json").unwrap();
        let text = String::from_utf8(e.1.clone()).unwrap();
        assert!(text.contains(r#""x":1.5"#), "{text}");
        e.1 = text.replace(r#""x":1.5"#, &format!(r#""x":{literal}"#)).into_bytes();
        let mut read = |o: u64, n: u64| Ok(data[o as usize..(o + n) as usize].to_vec());
        assert_eq!(view_problem(&keys, &entry(&changed), &mut read).is_none(), same, "{literal}");
    }
}

/// Text values in the view (conventions §8.7
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#87-the-view>): §6
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#6-source-metadata-as-json>'s text,
/// and `{text, after}` when bytes after the NUL are not all zero.
#[test]
fn text_values_show_their_text_and_what_follows_its_nul() {
    use serde_json::json;
    let cases: Vec<(&str, &[u8], serde_json::Value)> = vec![
        ("ascii", b"II", json!("II")),
        ("ascii", b"abc\0\0\0", json!("abc")),
        ("cstr", b"\xe9t\xe9\0", json!({"latin1": "\u{e9}t\u{e9}"})),
        ("cstr", b"ab\0xyz\0\0", json!({"text": "ab", "after": {"$vz": "bytes", "b64": "AHh5egAA"}})),
        ("ascii", b"\0\x01", json!({"text": "", "after": {"$vz": "bytes", "b64": "AAE="}})),
        ("utf16", b"h\0i\0\0\0", json!("hi")),
        ("utf16", b"h\0\0\0x\0", json!({"text": "h", "after": {"$vz": "bytes", "b64": "AAB4AA=="}})),
    ];
    for (kind, raw, want) in cases {
        assert_eq!(text_json(kind, raw), want, "{kind} {raw:?}");
    }
    // in a mirror: shown so, and the validator's rebuild agrees
    let data = b"Label\0hidden".to_vec();
    let mut ir = Ir::new(data.len() as u64);
    ir.value(0, "name", u64::MAX, "cstr[12]", 0, (0, 12), Some(&data)).unwrap();
    ir.finished = true;
    ir.build_children();
    let mut out = Out::new("u");
    mirror(&ir, &mut out, "czi").unwrap();
    let doc: serde_json::Value = serde_json::from_slice(&entry(&out)("refkv_source/tree/zarr.json").unwrap()).unwrap();
    assert_eq!(doc["attributes"]["refkv_zarrified"]["czi"]["name"], json!({"text": "Label", "after": {"$vz": "bytes", "b64": "AGhpZGRlbg=="}}));
    let keys: Vec<String> = out.entries.iter().map(|e| e.0.clone()).collect();
    let mut read = |o: u64, n: u64| Ok(data[o as usize..(o + n) as usize].to_vec());
    assert_eq!(view_problem(&keys, &entry(&out), &mut read), None);
}

/// Every derived transform a parser can emit (`Transform::ALL`, which `index` keeps
/// complete) either may hold shown values and a validator can derive it again, or may
/// hold none (conventions §8.8
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#88-canonical-form>); and on every
/// fixture, each derived element names a
/// known transform and no space that may hold none holds a shown value.
#[test]
fn every_derived_transform_holding_shown_values_can_be_derived_again() {
    use refkv_ir::ir::DERIVED;
    use refkv_ir::transform::Transform;
    for (k, t) in Transform::ALL.into_iter().enumerate() {
        assert_eq!(t.index(), k, "{t:?} is not at its index in ALL");
        let derivable = t.rederive(&[], &serde_json::Value::Null).is_some();
        assert!(!t.may_hold_shown() || derivable, "{} may hold shown values, but a validator cannot derive it", t.name());
    }
    let mut seen = std::collections::HashSet::new();
    for (f, p) in fixtures() {
        let data = std::fs::read(&p).unwrap();
        let Ok((ir, _, _)) = run_bytes(f, &data) else { continue };
        let y = canonical(&ir).unwrap();
        for i in 0..y.len() {
            if y.kind[i] == DERIVED {
                let t = Transform::of_form(y.form(i as u32).unwrap_or("")).unwrap_or_else(|| panic!("{p:?}: {:?}", y.form(i as u32)));
                seen.insert(t);
            }
            let d = y.space[i];
            if d != 0 && shown(&y, f.name(), i as u32) {
                assert!(Transform::of_form(y.form(d).unwrap()).unwrap().may_hold_shown(), "{p:?}: {}", y.path(i as u32));
            }
        }
    }
    assert!(seen.len() >= 2, "{seen:?}");
}

/// The view's 65536 bytes are what the members of a document's children spend (conventions §8.7
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#87-the-view>): `$vz` and `$form`
/// spend none, so a derived element of a long form has a document that holds more, and is not
/// partial.
#[test]
fn a_view_document_may_hold_more_than_its_children_spend() {
    let form = format!(r#"{{"pad":"{}","size":0,"transform":"zlib"}}"#, "x".repeat(70_000));
    let mut ir = Ir::new(10);
    ir.derived(0, "z", 0, (0, 10), &form).unwrap();
    ir.finished = true;
    ir.build_children();
    let mut out = Out::new("u");
    mirror(&ir, &mut out, "tiff").unwrap();
    let doc: serde_json::Value = serde_json::from_slice(&entry(&out)("refkv_source/tree/z/zarr.json").unwrap()).unwrap();
    let view = &doc["attributes"]["refkv_zarrified"]["tiff"];
    assert_eq!(view, &serde_json::json!({"$vz": "derived", "$form": form}));
    assert!(serde_json::to_vec(view).unwrap().len() > 65536);
    let keys: Vec<String> = out.entries.iter().map(|e| e.0.clone()).collect();
    assert_eq!(view_problem(&keys, &entry(&out), &mut no_read), None);
}
