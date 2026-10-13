//! The mirror of a store IR (conventions §8.9
//! <https://github.com/d-v-b/vzip/blob/main/spec/conventions.md#89-stores>), on the Zarr v2
//! store format (src/zarr2.rs) over the Zarr v2 and OME-Zarr fixture stores it accepts and
//! large synthetic stores: the table loads back to the canonical IR, keeps the invariants,
//! rebuilds every held object byte for byte and the listing, is canonical and a fixed point,
//! its view is the one it rebuilds, and the chunk objects of an array fold into column runs.
//! One test per way a store's table can be invalid.

use std::collections::BTreeMap;
use std::path::Path;
use std::time::Instant;
use vzip_ir::check::check;
use vzip_ir::ir::*;
use vzip_ir::mirror::{canonical_problem, load, mirror, ordered, rebuild_store, table_from_out, view_problem, write, Table};
use vzip_ir::out::Out;
use vzip_ir::store::{run_store, StoreFormat};

/// A directory's regular files as a listing (key, size) and their bytes, keys in byte order.
fn walk(root: &Path) -> BTreeMap<String, Vec<u8>> {
    let mut out = BTreeMap::new();
    let mut stack = vec![root.to_path_buf()];
    while let Some(d) = stack.pop() {
        for e in std::fs::read_dir(&d).unwrap() {
            let e = e.unwrap();
            let t = e.file_type().unwrap();
            if t.is_dir() {
                stack.push(e.path());
            } else if t.is_file() {
                let k = e.path().strip_prefix(root).unwrap().to_str().unwrap().replace('\\', "/");
                out.insert(k, std::fs::read(e.path()).unwrap());
            }
        }
    }
    out
}

fn stores() -> Vec<std::path::PathBuf> {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../fixtures");
    let mut v: Vec<_> = ["zarr2", "ome-zarr"].iter()
        .flat_map(|f| std::fs::read_dir(root.join(f)).unwrap().map(|e| e.unwrap().path()))
        .filter(|p| p.is_dir()).collect();
    v.sort();
    v
}

fn entry(out: &Out) -> impl Fn(&str) -> Option<Vec<u8>> + '_ {
    |k: &str| out.entries.iter().find(|e| e.0 == k).map(|e| e.1.clone())
}

fn no_source(_: u64, _: u64) -> Result<Vec<u8>, String> {
    Err("a store's mirror reads no source".into())
}

/// The mirror of a store held in memory, its IR and the documents the format read; or the
/// rejection.
fn try_mirrored(files: &BTreeMap<String, Vec<u8>>) -> Result<(Out, Ir, Vec<String>), String> {
    let keys: Vec<(String, u64)> = files.iter().map(|(k, v)| (k.clone(), v.len() as u64)).collect();
    let bytes: Vec<&Vec<u8>> = files.values().collect();
    let (ir, facts, _) = run_store(StoreFormat::Zarr2, keys, &|k| bytes[k].clone())?;
    let mut out = Out::new("https://example.org/s/");
    mirror(&ir, &mut out, "zarr2").unwrap();
    let docs = facts["documents"].as_array().unwrap().iter().map(|d| d.as_str().unwrap().to_string()).collect();
    Ok((out, ir, docs))
}

fn mirrored(files: &BTreeMap<String, Vec<u8>>) -> (Out, Ir) {
    let (out, ir, _) = try_mirrored(files).unwrap();
    (out, ir)
}

/// The mirror's table and description bytes (what the archive stores of it, uncompressed).
fn table_bytes(out: &Out) -> usize {
    out.entries.iter().filter(|(k, _)| k.starts_with("vzip_source/ir/") || k == "vzip_source/zarr.json").map(|(_, v)| v.len()).sum()
}

#[test]
fn a_store_s_mirror_rebuilds_its_documents_and_listing_and_is_canonical() {
    let (mut held, mut accepted) = (0, 0);
    for p in stores() {
        let files = walk(&p);
        let Ok((out, ir, docs)) = try_mirrored(&files) else { continue };
        accepted += 1;
        let t = table_from_out(&out).unwrap();
        assert_eq!(t.objects, Some(files.len() as u64), "{p:?}");
        let back = load(&t, &mut no_source).unwrap_or_else(|e| panic!("{p:?}: {e}"));
        assert_eq!(ordered(&back), ordered(&ir), "{p:?}");
        check(&back).unwrap();
        // the rebuild: the listing, and every held object byte for byte
        let rebuilt = rebuild_store(&entry(&out)).unwrap();
        let listing: Vec<(String, u64)> = rebuilt.iter().map(|r| (r.0.clone(), r.1)).collect();
        let mut sorted = listing.clone();
        sorted.sort();
        assert_eq!(sorted, files.iter().map(|(k, v)| (k.clone(), v.len() as u64)).collect::<Vec<_>>(), "{p:?}");
        for (k, _, b) in &rebuilt {
            assert_eq!(b.is_some(), docs.contains(k), "{p:?} {k}");
            if let Some(b) = b {
                assert!(b == &files[k], "{p:?} {k}");
                held += 1;
            }
        }
        // canonical, a fixed point, and its view the one its table gives
        assert_eq!(canonical_problem(&entry(&out), &mut no_source), None, "{p:?}");
        let keys: Vec<String> = out.entries.iter().map(|e| e.0.clone()).collect();
        assert_eq!(view_problem(&keys, &entry(&out), &mut no_source), None, "{p:?}");
    }
    assert!(accepted > 20 && held > 50, "{accepted} stores, {held} documents");
}

/// A Zarr v2 store of one array whose chunks are `rows × cols` objects, keys `i.j` (`sep`
/// ".") or `i/j` ("/"), of varying sizes.
fn synthetic(rows: u64, cols: u64, sep: &str) -> BTreeMap<String, Vec<u8>> {
    let mut f = BTreeMap::new();
    f.insert(".zgroup".to_string(), b"{\"zarr_format\": 2}".to_vec());
    let zarray = format!("{{\"chunks\": [1, 1], \"compressor\": null, \"dtype\": \"|u1\", \"fill_value\": 0, \"filters\": null, \
                          \"order\": \"C\", \"shape\": [{rows}, {cols}], \"zarr_format\": 2, \"dimension_separator\": \"{sep}\"}}");
    f.insert("a/.zarray".to_string(), zarray.into_bytes());
    for i in 0..rows {
        for j in 0..cols {
            f.insert(format!("a/{i}{sep}{j}"), vec![0; 1 + ((i * 7 + j * 13) % 97) as usize]);
        }
    }
    f
}

#[test]
fn the_chunk_objects_of_an_array_fold_into_column_runs() {
    for (rows, cols, sep, cruns) in [(4u64, 25u64, "/", 1u64), (4, 25, ".", 4), (100, 1000, "/", 1), (100, 1000, ".", 100)] {
        let files = synthetic(rows, cols, sep);
        let t0 = Instant::now();
        let (out, _) = mirrored(&files);
        let dt = t0.elapsed();
        let summary = &out.summary;
        let t = table_from_out(&out).unwrap();
        // and one of the .zarray's gaps between members
        assert_eq!(t.cruns.len() as u64, cruns + 1, "{rows}x{cols} {sep} {summary}");
        // member 0 of each run stored (a row of chunks for "/", one chunk for "."), not every
        // object; and the two documents, a value per member and the gaps between them
        assert!((t.kind.len() as u64) < 48 + if sep == "/" { cols + 1 } else { 2 * rows }, "{rows}x{cols} {sep}: {} rows stored", t.kind.len());
        let n = rows * cols;
        let bytes = table_bytes(&out);
        // about 8 bytes an object: its size, a column value (the listing's data)
        assert!((bytes as u64) < 8192 + 12 * n + 1024 * cruns, "{rows}x{cols} {sep}: {bytes} bytes");
        let t1 = Instant::now();
        let rebuilt = rebuild_store(&entry(&out)).unwrap();
        assert_eq!(rebuilt.len() as u64, n + 2);
        assert_eq!(canonical_problem(&entry(&out), &mut no_source), None);
        eprintln!("{rows}x{cols} sep {sep:?}: {n} chunk objects, {} rows stored, {} column runs, {} bytes of table; \
                   mirror {:.0} ms, rebuild and canonical check {:.0} ms",
                  t.kind.len(), t.cruns.len(), bytes, dt.as_secs_f64() * 1e3, t1.elapsed().as_secs_f64() * 1e3);
    }
}

fn small() -> Out {
    mirrored(&synthetic(1, 5, ".")).0
}

fn invalid(change: impl Fn(&mut Table)) -> String {
    let mut t = table_from_out(&small()).unwrap();
    change(&mut t);
    let mut out = Out::new("");
    write(&t, &mut out, "zarr2", false);
    let t = table_from_out(&out).unwrap();
    match load(&t, &mut no_source) {
        Err(e) => e,
        Ok(ir) => check(&ir).unwrap_err(),
    }
}

#[test]
fn a_store_table_with_held_objects_other_than_ir_held_s_is_refused() {
    assert!(invalid(|t| { t.held.pop(); }).starts_with("the table has "));
}

#[test]
fn a_store_table_whose_held_bytes_are_not_the_object_s_size_is_refused() {
    assert!(invalid(|t| t.held[0].push(b' ')).contains(" bytes, its size "));
}

#[test]
fn an_object_in_a_file_table_is_refused() {
    assert!(invalid(|t| { t.objects = None; t.held.clear(); }).contains(": kind 6"));
}

#[test]
fn a_store_table_of_too_many_objects_is_refused() {
    assert_eq!(invalid(|t| t.objects = Some(MAX_OBJECTS + 1)), format!("budget: more than {MAX_OBJECTS} objects"));
}

#[test]
fn a_store_table_whose_objects_are_not_its_description_s_is_refused() {
    assert!(invalid(|t| *t.objects.as_mut().unwrap() -= 1).contains("objects, its description"));
}

#[test]
fn a_store_table_of_too_many_name_bytes_is_refused() {
    let e = invalid(|t| t.names.push("x".repeat(1 << 24)));
    assert_eq!(e, "budget: the mirror's names total more than 16777216 bytes");
}

#[test]
fn a_store_column_read_from_an_array_is_refused() {
    let e = invalid(|t| {
        let c = (0..t.columns.len()).find(|&k| t.columns[k][2] == 0).expect("a name index column");
        t.columns[c][3] = 2;
    });
    assert_eq!(e, "a column read from an array, in a store IR");
}
