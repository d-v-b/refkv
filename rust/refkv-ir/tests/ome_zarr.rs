//! The OME layer of the Zarr v2 store format (src/ome_zarr.rs): every OME-Zarr fixture store it
//! accepts is OME-Zarr's, with a mirror that is canonical and rebuilds each held document byte
//! for byte; a Zarr v2 store that does not declare OME-NGFF 0.4 stays Zarr v2's; one test per
//! rejected fixture (each the role of `schema/ome-zarr.json`, or the Zarr v2 rule, that
//! rejects it); and the hostile probes.

use std::path::{Path, PathBuf};
use vzip_ir::store::{run_store, store_output, StoreFormat};

fn fixtures() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../fixtures")
}

fn walk(root: &Path, rel: &str, out: &mut Vec<(String, u64)>) {
    for e in std::fs::read_dir(root.join(rel)).unwrap() {
        let e = e.unwrap();
        let name = e.file_name().into_string().unwrap();
        let r = if rel.is_empty() { name } else { format!("{rel}/{name}") };
        if e.file_type().unwrap().is_dir() {
            walk(root, &r, out);
        } else {
            out.push((r, e.metadata().unwrap().len()));
        }
    }
}

/// The output of the store in `dir`, mirrored, or its rejection.
fn output(dir: &Path) -> Result<vzip_ir::out::Out, String> {
    let mut keys = Vec::new();
    walk(dir, "", &mut keys);
    keys.sort_by(|a, b| a.0.as_bytes().cmp(b.0.as_bytes()));
    let k2 = keys.clone();
    let (ir, facts, stats) = run_store(StoreFormat::Zarr2, keys, &|k| std::fs::read(dir.join(&k2[k].0)).unwrap())?;
    store_output(StoreFormat::Zarr2, &ir, &facts, stats, "https://example.org/s/", true)
}

fn stores(format: &str) -> Vec<PathBuf> {
    let mut v: Vec<PathBuf> = std::fs::read_dir(fixtures().join(format)).unwrap().map(|e| e.unwrap().path()).filter(|p| p.is_dir()).collect();
    v.sort();
    v
}

#[test]
fn accepted_stores_mirror_and_rebuild_their_documents() {
    let mut accepted = 0;
    for dir in stores("ome-zarr") {
        let name = dir.file_name().unwrap().to_str().unwrap().to_string();
        match output(&dir) {
            Err(m) => assert!(name.contains("reject"), "{name}: {m}"),
            Ok(out) => {
                assert!(!name.contains("reject"), "{name} was accepted");
                assert_eq!(out.summary["format"], "ome-zarr", "{name}");
                let entries: std::collections::HashMap<&str, &Vec<u8>> = out.entries.iter().map(|(k, v)| (k.as_str(), v)).collect();
                let get = |k: &str| entries.get(k).map(|v| v.to_vec());
                for (key, n, b) in vzip_ir::mirror::rebuild_store(&get).unwrap() {
                    if let Some(b) = b {
                        assert_eq!(b, std::fs::read(dir.join(&key)).unwrap(), "{name}: {key}");
                        assert_eq!(b.len() as u64, n);
                    }
                }
                let mut never = |_: u64, _: u64| -> Result<Vec<u8>, String> { panic!("a store's mirror reads no source") };
                assert_eq!(vzip_ir::mirror::canonical_problem(&get, &mut never), None, "{name}");
                let keys: Vec<String> = out.entries.iter().map(|e| e.0.clone()).collect();
                assert_eq!(vzip_ir::mirror::view_problem(&keys, &get, &mut never), None, "{name}");
                accepted += 1;
            }
        }
    }
    assert_eq!(accepted, 13);
}

#[test]
fn zarr2_stores_stay_zarr2() {
    // Every accepted Zarr v2 fixture is Zarr v2's: the test of OME-NGFF 0.4 finds none.
    let mut n = 0;
    for dir in stores("zarr2") {
        let name = dir.file_name().unwrap().to_str().unwrap().to_string();
        if let Ok(out) = output(&dir) {
            assert_eq!(out.summary["format"], "zarr2", "{name}");
            n += 1;
        }
    }
    assert!(n > 0);
}

macro_rules! reject {
    ($name:ident, $msg:expr) => {
        #[test]
        fn $name() {
            match output(&fixtures().join("ome-zarr").join(stringify!($name))) {
                Err(m) => assert!(m.contains($msg), "{m}"),
                Ok(_) => panic!("{} was accepted", stringify!($name)),
            }
        }
    };
}

reject!(ome_zarr_reject_axes_count, "axes is not 2 to 5 axis objects");
reject!(ome_zarr_reject_axes_duplicate, "axis names [x, x] are not distinct");
reject!(ome_zarr_reject_axes_four_space, "axis types [space, space, space, space] are not");
reject!(ome_zarr_reject_axes_name, "an axis name is not a string");
reject!(ome_zarr_reject_axes_order, "axis types [space, channel, space] are not");
reject!(ome_zarr_reject_axes_two_others, "axis types [channel, phase, space, space] are not");
reject!(ome_zarr_reject_axes_type, "axis type 3 is not a string");
reject!(ome_zarr_reject_bioformats2raw_layout, "bioformats2raw.layout is not 3");
reject!(ome_zarr_reject_bioformats2raw_numbering, "the images are not numbered consecutively from 0");
reject!(ome_zarr_reject_bioformats2raw_series, "OME: series is not an array of image paths");
reject!(ome_zarr_reject_datasets_empty, "datasets is not a nonempty array of objects");
reject!(ome_zarr_reject_datasets_missing, "datasets[0]: 9 is not an array");
reject!(ome_zarr_reject_datasets_path, "path ../1 is not a relative path");
reject!(ome_zarr_reject_datasets_rank, "r has 3 dimensions, not 2");
reject!(ome_zarr_reject_has_ome, "the group already has an `ome` attribute");
reject!(ome_zarr_reject_image_label_color_duplicate, "colors label-values are not unique");
reject!(ome_zarr_reject_image_label_dtype, "label data type float32 is not an integer type");
reject!(ome_zarr_reject_image_label_levels, "the label image has 1 levels, fewer than its image /'s 2");
reject!(ome_zarr_reject_image_label_no_multiscales, "image-label: the group has no multiscales");
reject!(ome_zarr_reject_image_label_rgba, "rgba [1,2,3,300] is not four integers from 0 to 255");
reject!(ome_zarr_reject_image_label_source, "source image ../../elsewhere is not an image");
reject!(ome_zarr_reject_image_label_value, "properties is not an array of objects with an integer label-value");
reject!(ome_zarr_reject_image_label_version, "image-label: version 0.3 is not 0.4");
reject!(ome_zarr_reject_labels_not_image, "label missing is not the path of an image");
reject!(ome_zarr_reject_level_axes_conflict, "p/q/lvl is a level of images with axes [y, x] and [a, b]");
reject!(ome_zarr_reject_multiscale_transforms, "multiscales[0]: the scale is not 2 numbers");
reject!(ome_zarr_reject_multiscales_empty, "g: multiscales is not a nonempty array of objects");
reject!(ome_zarr_reject_omero_channels, "omero: channels is not an array of objects");
reject!(ome_zarr_reject_omero_color, "channel color red is not 6 hexadecimal digits");
reject!(ome_zarr_reject_omero_window, "a channel window does not have numbers min, max, start and end");
reject!(ome_zarr_reject_plate_acquisition_id, "acquisition id -1 is not an integer from 0");
reject!(ome_zarr_reject_plate_column_name, "a column name is not alphanumeric");
reject!(ome_zarr_reject_plate_field_count, "field_count 0 is not a positive integer");
reject!(ome_zarr_reject_plate_row_duplicate, "row names are not unique");
reject!(ome_zarr_reject_plate_version, "plate: version 0.3 is not 0.4");
reject!(ome_zarr_reject_plate_well_index, "well rowIndex or columnIndex is not an index of rows or columns");
reject!(ome_zarr_reject_plate_well_missing, "plate: A/2 is not a well");
reject!(ome_zarr_reject_plate_well_path, "well path B/2 is not row A/column 2");
reject!(ome_zarr_reject_scale_length, "datasets[0]: the scale is not 2 numbers");
reject!(ome_zarr_reject_scale_order, "the scale [0.5, 2] is smaller than the previous level's [1, 1]");
reject!(ome_zarr_reject_transforms_identity, "the first transformation is not a scale");
reject!(ome_zarr_reject_transforms_missing, "coordinateTransformations is not one or two transformation objects");
reject!(ome_zarr_reject_transforms_order, "the first transformation is not a scale");
reject!(ome_zarr_reject_transforms_path, "the scale is not 2 numbers");
reject!(ome_zarr_reject_translation_length, "the translation is not 2 numbers");
reject!(ome_zarr_reject_version, "old: multiscales[0]: version 0.3 is not 0.4");
reject!(ome_zarr_reject_well_acquisition, "A/1: acquisition 5 is not one of the plate's");
reject!(ome_zarr_reject_well_acquisition_missing, "an image has no acquisition, and the plate has several");
reject!(ome_zarr_reject_well_image_duplicate, "well: image paths are not unique");
reject!(ome_zarr_reject_well_image_missing, "well: A/1/7 is not an image");
reject!(ome_zarr_reject_well_image_path, "image path f-0 is not alphanumeric");

#[test]
fn every_rejection_fixture_has_a_test() {
    let here = std::fs::read_to_string(file!()).unwrap();
    for dir in stores("ome-zarr") {
        let name = dir.file_name().unwrap().to_str().unwrap().to_string();
        if name.contains("_reject_") {
            assert!(here.contains(&format!("reject!({name},")), "{name} has no test");
        }
    }
}

// ---- hostile probes (in the style of python/tests/ir/gen_probes.py): each store is refused
// or accepted, in bounded time and with a bounded mirror

/// The output of a store held in memory (key -> bytes), mirrored, or its rejection, and the time.
fn output_of(objects: &[(String, Vec<u8>)]) -> (Result<vzip_ir::out::Out, String>, std::time::Duration) {
    let mut keys: Vec<(String, u64)> = objects.iter().map(|(k, b)| (k.clone(), b.len() as u64)).collect();
    keys.sort_by(|a, b| a.0.as_bytes().cmp(b.0.as_bytes()));
    let by: std::collections::HashMap<&str, &Vec<u8>> = objects.iter().map(|(k, b)| (k.as_str(), b)).collect();
    let k2 = keys.clone();
    let t = std::time::Instant::now();
    let r = run_store(StoreFormat::Zarr2, keys, &|k| by[k2[k].0.as_str()].clone())
        .and_then(|(ir, facts, stats)| store_output(StoreFormat::Zarr2, &ir, &facts, stats, "https://example.org/s/", true));
    (r, t.elapsed())
}

fn doc(k: &str, v: serde_json::Value) -> (String, Vec<u8>) {
    (k.to_string(), v.to_string().into_bytes())
}

fn image(path: &str, levels: &[&str]) -> Vec<(String, Vec<u8>)> {
    let p = |k: &str| if path.is_empty() { k.to_string() } else { format!("{path}/{k}") };
    let mut v = vec![doc(&p(".zgroup"), serde_json::json!({"zarr_format": 2})), doc(&p(".zattrs"), serde_json::json!({"multiscales": [{
        "version": "0.4", "axes": [{"name": "y", "type": "space"}, {"name": "x", "type": "space"}],
        "datasets": levels.iter().map(|l| serde_json::json!({"path": l, "coordinateTransformations": [{"type": "scale", "scale": [1, 1]}]})).collect::<Vec<_>>(),
    }]}))];
    for l in levels {
        v.push(doc(&p(&format!("{l}/.zarray")), serde_json::json!({"zarr_format": 2, "shape": [4, 4], "chunks": [2, 2], "dtype": "|u1",
            "order": "C", "compressor": null, "fill_value": 0, "filters": null})));
    }
    v
}

#[test]
fn probe_many_multiscales_naming_one_array_is_accepted_quickly() {
    // amplification: 20000 multiscales of one image, each naming the same level
    let ms: Vec<serde_json::Value> = (0..20000).map(|_| serde_json::json!({"version": "0.4",
        "axes": [{"name": "y", "type": "space"}, {"name": "x", "type": "space"}],
        "datasets": [{"path": "0", "coordinateTransformations": [{"type": "scale", "scale": [1, 1]}]}]})).collect();
    let mut objects = image("", &["0"]);
    objects[1] = doc(".zattrs", serde_json::json!({"multiscales": ms}));
    let (r, t) = output_of(&objects);
    assert!(r.is_ok(), "{:?}", r.err());
    assert!(t.as_secs() < 10, "{t:?}");
}

#[test]
fn probe_many_levels_are_accepted_quickly() {
    // huge counts: one multiscale of 20000 datasets, each its own array (each level's first
    // multiscale is found by a map, not by a scan of every level)
    let n = 20000;
    let levels: Vec<String> = (0..n).map(|i| format!("l{i}")).collect();
    let refs: Vec<&str> = levels.iter().map(|l| l.as_str()).collect();
    let (r, t) = output_of(&image("", &refs));
    assert_eq!(r.unwrap().summary["arrays"], n);
    assert!(t.as_secs() < 30, "{t:?}");
}

#[test]
fn probe_a_plate_listing_one_well_many_times_is_checked_quickly() {
    // huge counts: 20000 plate wells naming one well of 2000 fields, 2000 acquisitions
    let n = 2000;
    let mut objects = vec![doc(".zgroup", serde_json::json!({"zarr_format": 2})), doc(".zattrs", serde_json::json!({"plate": {
        "version": "0.4", "rows": [{"name": "A"}], "columns": [{"name": "1"}],
        "acquisitions": (0..n).map(|i| serde_json::json!({"id": i})).collect::<Vec<_>>(),
        "wells": (0..20000).map(|_| serde_json::json!({"path": "A/1", "rowIndex": 0, "columnIndex": 0})).collect::<Vec<_>>()}}))];
    objects.push(doc("A/.zgroup", serde_json::json!({"zarr_format": 2})));
    objects.push(doc("A/1/.zgroup", serde_json::json!({"zarr_format": 2})));
    objects.push(doc("A/1/.zattrs", serde_json::json!({"well": {"version": "0.4",
        "images": (0..n).map(|i| serde_json::json!({"path": format!("f{i}"), "acquisition": i})).collect::<Vec<_>>()}})));
    for i in 0..n {
        objects.extend(image(&format!("A/1/f{i}"), &["0"]));
    }
    let (r, t) = output_of(&objects);
    assert!(r.is_ok(), "{:?}", r.err());
    assert!(t.as_secs() < 30, "{t:?}");
}

#[test]
fn probe_documents_nested_past_the_limit_are_refused() {
    let deep = |d: usize| format!("{}{}", "[".repeat(d), "]".repeat(d));
    let mut objects = image("", &["0"]);
    objects.push(("labels/.zgroup".into(), b"{\"zarr_format\": 2}".to_vec()));
    objects.push(("labels/.zattrs".into(), format!("{{\"x\": {}}}", deep(255)).into_bytes()));
    assert!(output_of(&objects).0.is_ok());
    objects.pop();
    objects.push(("labels/.zattrs".into(), format!("{{\"x\": {}}}", deep(256)).into_bytes()));
    assert!(output_of(&objects).0.err().unwrap().contains("nests more than 256"));
}

#[test]
fn probe_a_label_image_that_is_its_own_source_is_accepted() {
    // a loop: the label image's source.image is itself
    let mut objects = image("", &["0"]);
    objects.extend(image("labels/a", &["0", "1"]));
    objects.retain(|(k, _)| k != "labels/a/.zattrs");
    let ms = serde_json::json!([{"version": "0.4", "axes": [{"name": "y", "type": "space"}, {"name": "x", "type": "space"}],
        "datasets": [{"path": "0", "coordinateTransformations": [{"type": "scale", "scale": [1, 1]}]},
                     {"path": "1", "coordinateTransformations": [{"type": "scale", "scale": [2, 2]}]}]}]);
    objects.push(doc("labels/a/.zattrs", serde_json::json!({"multiscales": ms, "image-label": {"version": "0.4", "source": {"image": "."}}})));
    objects.push(doc("labels/.zgroup", serde_json::json!({"zarr_format": 2})));
    objects.push(doc("labels/.zattrs", serde_json::json!({"labels": ["a"]})));
    let (r, _) = output_of(&objects);
    assert_eq!(r.unwrap().summary["droppedLabelLevels"], 0);
}

#[test]
fn probe_many_chunk_objects_fold_into_few_rows() {
    // huge counts: 200 x 500 chunk objects of one array, flat keys
    let mut objects = image("", &["0"]);
    objects[2] = doc("0/.zarray", serde_json::json!({"zarr_format": 2, "shape": [400, 1000], "chunks": [2, 2], "dtype": "|u1",
        "order": "C", "compressor": null, "fill_value": 0, "filters": null}));
    for i in 0..200 {
        for j in 0..500 {
            objects.push((format!("0/{i}.{j}"), vec![7u8; 4]));
        }
    }
    let (r, t) = output_of(&objects);
    let out = r.unwrap();
    assert_eq!(out.summary["chunks"], 100_000);
    let rows = out.summary["folded"]["stored"].as_u64().unwrap_or(u64::MAX);
    assert!(rows < 1000, "{}", out.summary["folded"]);
    assert!(t.as_secs() < 30, "{t:?}");
}

#[test]
fn probe_an_object_beside_a_directory_of_its_name_is_accepted() {
    // overlapping names: the object `0` and the directory `0/` (the level)
    let mut objects = image("", &["0"]);
    objects.push(("0".into(), b"not a chunk".to_vec()));
    let out = output_of(&objects).0.unwrap();
    assert!(out.refs.iter().any(|(k, _)| k == "vzip_source/objects/0"));
}

#[test]
fn ignored_keys_are_vzip_source_metadata() {
    // the recorded ignored keys sit beside the mirror's description, sorted by their bytes
    let objects = image("", &["0"]);
    let keys: Vec<(String, u64)> = objects.iter().map(|(k, b)| (k.clone(), b.len() as u64)).collect();
    let mut r = vzip_ir::store::StoreRun::new(StoreFormat::Zarr2, keys, 4).unwrap();
    r.ignore(vec!["z/./a".into(), "donn\u{e9}es/./x".into()], Vec::new()).unwrap();
    while let Some(reqs) = r.poll().unwrap() {
        for (k, q) in reqs.iter().enumerate() {
            let b = &objects[q.object].1;
            r.complete(k, q.spans.iter().map(|&(a, e)| b[a as usize..e as usize].to_vec()).collect()).unwrap();
        }
    }
    let out = r.into_output("https://example.org/s/", true).unwrap();
    assert!(!out.entries.iter().any(|e| e.0 == "vzip_source/ignored.json"));
    let doc: serde_json::Value = serde_json::from_slice(&out.entries.iter().find(|e| e.0 == "vzip_source/zarr.json").unwrap().1).unwrap();
    let own = &doc["attributes"]["vzip_virtualized"]["ome-zarr"];
    assert_eq!(own["ignored"], serde_json::json!(["donn\u{e9}es/./x", "z/./a"]));
    assert!(own["ir"].is_object());
    // the mirror is canonical with its ignored keys beside it
    let get = |k: &str| out.entries.iter().find(|e| e.0 == k).map(|e| e.1.clone());
    let mut never = |_: u64, _: u64| -> Result<Vec<u8>, String> { panic!("a store's mirror reads no source") };
    assert_eq!(vzip_ir::mirror::canonical_problem(&get, &mut never), None);
}

#[test]
fn probe_a_document_the_test_reads_outside_the_batch() {
    // the plate's first well lies inside an array (`A`), so its `.zattrs` is no node document:
    // the test of OME-NGFF 0.4 reads it in a round of its own
    let base = |well: &[u8]| -> Vec<(String, Vec<u8>)> {
        let mut o = vec![doc(".zgroup", serde_json::json!({"zarr_format": 2})), doc(".zattrs", serde_json::json!({
            "bioformats2raw.layout": 3, "plate": {"wells": [{"path": "A/1"}]}}))];
        o.push(doc("A/.zarray", serde_json::json!({"zarr_format": 2, "shape": [4], "chunks": [2], "dtype": "|u1",
            "order": "C", "compressor": null, "fill_value": 0, "filters": null})));
        o.push(doc("A/1/.zgroup", serde_json::json!({"zarr_format": 2})));
        o.push(("A/1/.zattrs".into(), well.to_vec()));
        o
    };
    let keys = |o: &[(String, Vec<u8>)]| -> Vec<(String, u64)> {
        // in the order of their bytes, as output_of lists them; read by listing index, found in o by key
        let mut k: Vec<(String, u64)> = o.iter().map(|(k, b)| (k.clone(), b.len() as u64)).collect();
        k.sort_by(|a, b| a.0.as_bytes().cmp(b.0.as_bytes()));
        k
    };
    // a well with no images: not OME-NGFF 0.4, Zarr v2's, after two rounds
    let o = base(br#"{"well": {"images": []}}"#);
    let listing = keys(&o);
    let bytes = |k: usize| o.iter().find(|x| x.0 == listing[k].0).unwrap().1.clone();
    let (_, _, stats) = run_store(StoreFormat::Zarr2, listing.clone(), &bytes).unwrap();
    assert_eq!(stats["batches"], 2);
    assert_eq!(output_of(&o).0.unwrap().summary["format"], "zarr2");
    // a document the test cannot parse rejects the store
    let o = base(b"{not json");
    assert!(output_of(&o).0.err().unwrap().starts_with("A/1/.zattrs: invalid JSON"));
}

#[test]
fn a_run_names_the_profile_it_decided() {
    // before its batch a Zarr v2 run is zarr2's; after it, OME-Zarr's when the root declares 0.4
    let objects = image("", &["0"]);
    let keys: Vec<(String, u64)> = objects.iter().map(|(k, b)| (k.clone(), b.len() as u64)).collect();
    let mut r = vzip_ir::store::StoreRun::new(StoreFormat::Zarr2, keys, 4).unwrap();
    assert_eq!(r.profile(), "zarr2");
    while let Some(reqs) = r.poll().unwrap() {
        for (k, q) in reqs.iter().enumerate() {
            let b = &objects[q.object].1;
            r.complete(k, q.spans.iter().map(|&(a, e)| b[a as usize..e as usize].to_vec()).collect()).unwrap();
        }
    }
    assert_eq!(r.profile(), "ome-zarr");
}

#[test]
fn a_layout_that_is_not_an_integer_is_not_3() {
    // B1: a layout that is not the integer 3 rejects as 2 does, never passing as an absent value
    for layout in ["\"3\"", "null", "3.5", "true"] {
        let dir = std::env::temp_dir().join(format!("vzip-ome-layout-{}-{}", std::process::id(), layout.replace('"', "q")));
        let _ = std::fs::remove_dir_all(&dir);
        let src = fixtures().join("ome-zarr/ome_zarr_empty_xml");
        let mut keys = Vec::new();
        walk(&src, "", &mut keys);
        for (k, _) in &keys {
            std::fs::create_dir_all(dir.join(k).parent().unwrap()).unwrap();
            std::fs::copy(src.join(k), dir.join(k)).unwrap();
        }
        std::fs::write(dir.join(".zattrs"), format!("{{\"bioformats2raw.layout\": {layout}}}")).unwrap();
        let r = output(&dir);
        std::fs::remove_dir_all(&dir).unwrap();
        match r {
            Err(m) => assert!(m.contains("bioformats2raw.layout is not 3"), "{m}"),
            Ok(_) => panic!("layout {layout} was accepted"),
        }
    }
}
