//! The VSI parser (src/vsi/; spec/virtualize/vsi.md): every slide fixture it accepts is a valid
//! store IR whose objects (the `.vsi` file and its tile files, described in place) rebuild from
//! their leaves, whose mirror is canonical and whose view is the one it rebuilds; its images
//! are the fixtures' (the generator's expected arrays are checked by python/tests/ir/test_vsi.py);
//! and one test per rejected fixture (each the role the schema names), the run driven as a host
//! drives it, its probes answered from the fixture directory.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use refkv_ir::check;
use refkv_ir::ir::{Ir, OBJECT};
use refkv_ir::mirror::{canonical_problem, load, table_from_out, view_problem};
use refkv_ir::out::{Out, Part};
use refkv_ir::run::Format;
use refkv_ir::store::{StoreFormat, StoreRun};

fn fixtures() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../fixtures/vsi")
}

/// Runs the VSI store format on the `.vsi` at `path`, probing for its tile files beside it:
/// (the output, the IR, the files read by key), or the rejection.
fn run(path: &Path) -> Result<(Out, Ir, BTreeMap<String, Vec<u8>>), String> {
    let dir = path.parent().unwrap();
    let key = path.file_name().unwrap().to_str().unwrap().to_string();
    let size = std::fs::metadata(path).unwrap().len();
    let mut files: BTreeMap<String, Vec<u8>> = BTreeMap::new();
    files.insert(key.clone(), std::fs::read(path).unwrap());
    let mut run = StoreRun::new(StoreFormat::Vsi, vec![(key, size)], 4)?;
    let mut keys = vec![path.file_name().unwrap().to_str().unwrap().to_string()];
    loop {
        let Some(reqs) = run.poll()? else { break };
        if reqs.is_empty() {
            for k in run.probes().to_vec() {
                let f = dir.join(&k);
                let data = std::fs::read(&f).ok();
                run.found(data.as_ref().map(|d| d.len() as u64))?;
                if let Some(d) = data {
                    keys.push(k.clone());
                    files.insert(k, d);
                }
            }
            continue;
        }
        for (k, q) in reqs.iter().enumerate() {
            let data = &files[&q.key];
            let parts = q.spans.iter().map(|&(a, b)| data[a as usize..b as usize].to_vec()).collect();
            run.complete(k, parts)?;
        }
    }
    let (ir, facts, stats) = run.finish()?;
    let out = refkv_ir::store::store_output(StoreFormat::Vsi, &ir, &facts, stats, "https://example.org/slides/", true)?;
    // the files of the input: those the run made objects (a second tile file name found beside
    // the first is neither read nor an object)
    let objects: std::collections::HashSet<String> = ir.sources().objects.iter().map(|&o| ir.key(o)).collect();
    files.retain(|k, _| objects.contains(k));
    Ok((out, ir, files))
}

fn entry(out: &Out) -> impl Fn(&str) -> Option<Vec<u8>> + '_ {
    |k: &str| out.entries.iter().find(|e| e.0 == k).map(|e| e.1.clone())
}

/// Every object is described in place, its leaves rebuild it from its own bytes, the mirror is
/// canonical and its view is the one the table and the files give.
fn check_slide(path: &Path, files: &BTreeMap<String, Vec<u8>>, out: &Out, ir: &Ir) {
    check::check(ir).unwrap_or_else(|m| panic!("{path:?}: {m}"));
    let src = ir.sources();
    assert_eq!(src.objects.len(), files.len(), "{path:?}");
    for &o in &src.objects {
        assert!(ir.kind[o as usize] == OBJECT && ir.in_place(o), "{path:?}: {} is not described in place", ir.key(o));
    }
    // the leaves of each object, read at virtual offsets (adjacent ones merged, across objects
    // too), rebuild the files end to end
    let mut rebuilt: BTreeMap<String, Vec<u8>> = BTreeMap::new();
    for (mut v, mut n) in check::leaves(ir).unwrap() {
        while n > 0 {
            let (o, base) = src.at(ir, v).unwrap();
            let key = ir.key(o);
            let x = (v - base) as usize;
            let take = (n as usize).min(ir.len[o as usize] as usize - x);
            rebuilt.entry(key.clone()).or_default().extend_from_slice(&files[&key][x..x + take]);
            v += take as u64;
            n -= take as u64;
        }
    }
    for (k, b) in files {
        assert!(rebuilt.get(k).is_some_and(|r| r == b), "{path:?}: {k} is not rebuilt");
    }
    // the mirror: loaded back, it is the IR; its table is a fixed point; the view is the stored one
    let get = entry(out);
    let back = load(&table_from_out(out).unwrap(), &mut |_, _| Err("no source".into())).unwrap_or_else(|e| panic!("{path:?}: {e}"));
    assert_eq!(back.len(), ir.len(), "{path:?}");
    let bsrc = back.sources();
    let mut read = |v: u64, n: u64| -> Result<Vec<u8>, String> {
        let (o, base) = bsrc.at(&back, v).ok_or("no object at that virtual offset")?;
        let x = (v - base) as usize;
        let b = &files[&back.key(o)];
        b.get(x..x + n as usize).map(|s| s.to_vec()).ok_or_else(|| format!("[{x}, {}) runs past {:?}", x + n as usize, back.key(o)))
    };
    assert_eq!(canonical_problem(&get, &mut read), None, "{path:?}");
    let keys: Vec<String> = out.entries.iter().map(|e| e.0.clone()).collect();
    assert_eq!(view_problem(&keys, &get, &mut read), None, "{path:?}");
    // every reference names a range of a file, within it
    for (k, parts) in &out.refs {
        for p in parts {
            if let Part::Obj(key, o, n) = p {
                let size = files.get(key).map(|b| b.len() as u64).unwrap_or_else(|| panic!("{path:?}: {k} references {key}, not a file of the input"));
                assert!(o + n <= size, "{path:?}: {k} references [{o}, {}) of the {size}-byte {key}", o + n);
            }
        }
    }
}

#[test]
fn accepted_slides_are_valid_store_irs_whose_mirrors_rebuild_their_files() {
    let mut n = 0;
    for e in std::fs::read_dir(fixtures()).unwrap() {
        let path = e.unwrap().path();
        if path.extension().is_none_or(|x| x != "vsi") || path.file_name().unwrap().to_str().unwrap().starts_with("vsi_reject") {
            continue;
        }
        let (out, ir, files) = run(&path).unwrap_or_else(|m| panic!("{path:?}: {m}"));
        check_slide(&path, &files, &out, &ir);
        assert_eq!(out.summary["format"], "vsi");
        n += 1;
    }
    assert_eq!(n, 8);
}

/// The brightfield-like fixture: its images (two JPEG pyramids, a focus map from an IFD, the
/// macro image), their levels as Bio-Formats sizes them, the missing tile's chunk absent, and
/// the macro's chunk the strip with its shared JPEG prefix.
#[test]
fn the_rgb_jpeg_slide_is_a_bioformats2raw_layout_of_its_stacks_and_the_macro_image() {
    let (out, _, _) = run(&fixtures().join("vsi_rgb_jpeg.vsi")).unwrap();
    let get = entry(&out);
    let doc = |k: &str| -> serde_json::Value { serde_json::from_slice(&get(k).unwrap_or_else(|| panic!("no {k}"))).unwrap() };
    let root = doc("zarr.json");
    assert_eq!(root["attributes"]["ome"], serde_json::json!({"version": "0.5", "bioformats2raw.layout": 3}));
    assert_eq!(root["attributes"]["refkv_virtualized"]["source"]["url"], "https://example.org/slides/vsi_rgb_jpeg.vsi");
    assert_eq!(doc("OME/zarr.json")["attributes"]["ome"]["series"], serde_json::json!(["0", "1", "2", "3"]));
    let names: Vec<String> = (0..4).map(|k| doc(&format!("{k}/zarr.json"))["attributes"]["ome"]["multiscales"][0]["name"].as_str().unwrap().to_string()).collect();
    assert_eq!(names, ["Overview", "20x_BF_01", "20x FocusMap", "macro image"]);
    // the 20x stack: 300 × 200 in tiles of 64, three levels (150 × 100, 75 × 50), RGB JPEG
    let lv: Vec<serde_json::Value> = (0..3).map(|l| doc(&format!("1/{l}/zarr.json"))).collect();
    assert_eq!(lv[0]["shape"], serde_json::json!([3, 200, 300]));
    assert_eq!(lv[1]["shape"], serde_json::json!([3, 100, 150]));
    assert_eq!(lv[2]["shape"], serde_json::json!([3, 50, 75]));
    assert_eq!(lv[0]["chunk_grid"]["configuration"]["chunk_shape"], serde_json::json!([3, 64, 64]));
    assert_eq!(lv[0]["codecs"], serde_json::json!([{"name": "transpose", "configuration": {"order": [1, 2, 0]}}, {"name": "imagecodecs_jpeg"}]));
    assert_eq!(lv[0]["fill_value"], 238);
    let refs: BTreeMap<&str, &Vec<Part>> = out.refs.iter().map(|r| (r.0.as_str(), &r.1)).collect();
    assert!(refs.contains_key("1/0/c/0/0/0") && !refs.contains_key("1/0/c/0/1/2"), "the missing tile has no chunk");
    assert!(matches!(refs["1/0/c/0/0/0"].as_slice(), [Part::Obj(k, _, _)] if k == "_vsi_rgb_jpeg_/stack10002/frame_t.ets"));
    // the focus map: a 6 × 5 uint16 IFD, one chunk of its strip
    assert_eq!(doc("2/0/zarr.json")["shape"], serde_json::json!([5, 6]));
    assert!(matches!(refs["2/0/c/0/0"].as_slice(), [Part::Obj(k, _, 60)] if k == "vsi_rgb_jpeg.vsi"));
    // the macro: the JPEG strip's SOI, the shared prefix, the rest of the strip
    assert_eq!(doc("3/0/zarr.json")["shape"], serde_json::json!([3, 30, 80]));
    assert!(matches!(refs["3/0/c/0/0/0"].as_slice(), [Part::Obj(_, _, 2), Part::Data(1, 0, _), Part::Obj(_, _, _)]));
    let ms = &doc("1/zarr.json")["attributes"]["ome"]["multiscales"][0];
    assert_eq!(ms["axes"][1]["unit"], "micrometer");
    assert_eq!(ms["datasets"][1]["coordinateTransformations"][0]["scale"], serde_json::json!([1, 0.27 * 2.0, 0.27 * 2.0]));
    assert_eq!(ms["datasets"][0]["coordinateTransformations"][1]["translation"], serde_json::json!([0, 4600.0, 37000.0]));
    let omero = &doc("1/zarr.json")["attributes"]["ome"]["omero"]["channels"];
    assert_eq!(omero[0]["label"], "R");
    assert_eq!(omero[2]["color"], "0000FF");
}

/// The multidimensional fixture: t, c and z axes from the tile coordinates, the channel names
/// and colors from the tree, the z step, and the 12-bit background as the fill value.
#[test]
fn a_stack_with_t_z_and_c_dimensions_has_the_axes_and_channels_of_its_tree() {
    let (out, _, _) = run(&fixtures().join("vsi_raw_tzc.vsi")).unwrap();
    let get = entry(&out);
    let doc = |k: &str| -> serde_json::Value { serde_json::from_slice(&get(k).unwrap()).unwrap() };
    let a = doc("0/0/zarr.json");
    assert_eq!(a["shape"], serde_json::json!([2, 2, 3, 30, 40]));
    assert_eq!(a["dimension_names"], serde_json::json!(["t", "c", "z", "y", "x"]));
    assert_eq!(a["fill_value"], 4095);
    assert_eq!(a["codecs"], serde_json::json!([{"name": "bytes", "configuration": {"endian": "little"}}]));
    let g = doc("0/zarr.json");
    let ms = &g["attributes"]["ome"]["multiscales"][0];
    assert_eq!(ms["datasets"][0]["coordinateTransformations"][0]["scale"], serde_json::json!([1, 1, 0.2, 0.065, 0.065]));
    let ch = &g["attributes"]["ome"]["omero"]["channels"];
    assert_eq!((ch[0]["label"].as_str(), ch[0]["color"].as_str()), (Some("Cy5"), Some("FF0000")));
    assert_eq!(ch[1]["window"], serde_json::json!({"min": 0.0, "max": 65535.0, "start": 86.0, "end": 170.0}));
    let own = &g["attributes"]["refkv_virtualized"]["vsi"];
    assert_eq!(own["tile_file"], "_vsi_raw_tzc_/stack1/frame_t_0.ets");
    assert_eq!(own["dimensions"], serde_json::json!([{"coordinate": 2, "meaning": "t"}, {"coordinate": 3, "meaning": "z"}, {"coordinate": 4, "meaning": "c"}]));
    assert_eq!(out.refs.len(), 13);
    assert!(out.refs.iter().any(|r| r.0 == "0/0/c/1/1/2/0/0"));
}

#[test]
fn the_root_holds_the_tree_as_json_and_the_stacks() {
    let (out, _, _) = run(&fixtures().join("vsi_metadata_edges.vsi")).unwrap();
    let get = entry(&out);
    let root: serde_json::Value = serde_json::from_slice(&get("zarr.json").unwrap()).unwrap();
    let own = &root["attributes"]["refkv_virtualized"]["vsi"];
    assert_eq!(own["file"]["key"], "vsi_metadata_edges.vsi");
    assert_eq!(own["stacks"][0]["name"], "µm édge");
    let fields = own["tree"]["fields"].as_array().unwrap();
    assert_eq!(fields[0]["tag"], 2);
    let coll = &fields[1];
    assert_eq!((coll["tag"].as_u64(), coll["tag2"].as_u64(), coll["name"].as_str()), (Some(2000), Some(0), Some("Collection volume")));
    let stack = coll["fields"].as_array().unwrap().iter().find(|f| f["tag"] == 2001).unwrap();
    let props = stack["fields"].as_array().unwrap().iter().find(|f| f["tag"] == 2005).unwrap();
    let blob = props["fields"].as_array().unwrap().iter().find(|f| f["tag"] == 2024).unwrap();
    assert_eq!(blob["value"], serde_json::json!({"bytes": 1280}));
    let unit = props["fields"].as_array().unwrap().iter().find(|f| f["tag"] == 2020).unwrap();
    assert_eq!(unit["value"], "10^-3m^1");
    // millimeters, no origin: a scale in millimeters and no translation
    let g: serde_json::Value = serde_json::from_slice(&get("0/zarr.json").unwrap()).unwrap();
    let ms = &g["attributes"]["ome"]["multiscales"][0];
    assert_eq!(ms["axes"][0]["unit"], "millimeter");
    assert_eq!(ms["datasets"][0]["coordinateTransformations"].as_array().unwrap().len(), 1);
}

#[test]
fn sniff_finds_a_vsi_file_before_a_tiff() {
    let head = std::fs::read(fixtures().join("vsi_raw_tzc.vsi")).unwrap();
    assert_eq!(Format::sniff(&head[..552]), Some(Format::Vsi));
    assert_eq!(Format::sniff(b"II*\0\x08\0\0\0\0\0\0\0"), Some(Format::Tiff));
}

#[test]
fn a_vsi_file_run_is_refused() {
    let data = std::fs::read(fixtures().join("vsi_raw_tzc.vsi")).unwrap();
    assert_eq!(refkv_ir::run::run_bytes(Format::Vsi, &data).err().as_deref(), Some("a VSI file is virtualized as a store run, with its tile files"));
}

#[test]
fn a_listing_of_more_than_the_vsi_file_is_refused() {
    let r = StoreRun::new(StoreFormat::Vsi, vec![("a.vsi".into(), 100), ("b.ets".into(), 100)], 1);
    assert!(r.err().unwrap().starts_with("a VSI run starts from the .vsi file alone"));
    let r = StoreRun::new(StoreFormat::Vsi, vec![("a.tif".into(), 100)], 1);
    assert!(r.err().unwrap().contains("is not the name of a .vsi file"));
}

macro_rules! reject {
    ($name:ident, $file:expr, $msg:expr) => {
        #[test]
        fn $name() {
            match run(&fixtures().join($file)) {
                Err(m) => assert!(m.starts_with($msg), "{m}"),
                Ok(_) => panic!("{} was accepted", $file),
            }
        }
    };
}

reject!(reject_short, "vsi_reject_short.vsi", "the .vsi file of 20 bytes is too short");
reject!(reject_first_ifd_outside, "vsi_reject_first_ifd_outside.vsi", "the first IFD at");
reject!(reject_container_header_size, "vsi_reject_container_header_size.vsi", "the bytes at 48 are not a container (header size 23");
reject!(reject_container_version, "vsi_reject_container_version.vsi", "the bytes at 48 are not a container (header size 24, version 21322)");
reject!(reject_container_count, "vsi_reject_container_count.vsi", "the container at 8 declares 1048577 fields, more than 1048576");
reject!(reject_container_fields_fit, "vsi_reject_container_fields_fit.vsi", "the container at 8 declares 100000 fields, which do not fit");
reject!(reject_container_data_offset, "vsi_reject_container_data_offset.vsi", "the container at 8 has the data offset 32, not 24");
reject!(reject_container_depth, "vsi_reject_container_depth.vsi", "containers nested more than 64 deep");
reject!(reject_field_header, "vsi_reject_field_header.vsi", "field 0 of the container at");
reject!(reject_field_next_zero, "vsi_reject_field_next_zero.vsi", "field 1 (tag 99) of the container at 8 has next 0, but it is not the last of 3 fields");
reject!(reject_field_last_next, "vsi_reject_field_last_next.vsi", "field 1 (tag 2000) of the container at 8 has next 24, but it is the last of 2 fields");
reject!(reject_field_next_outside, "vsi_reject_field_next_outside.vsi", "field 1 (tag 99) of the container at 8 has next 1073741824, outside its container");
reject!(reject_field_data_outside, "vsi_reject_field_data_outside.vsi", "field 1 (tag 99, type 5) of the container at 8: its data (1048576 bytes) runs past its region");
reject!(reject_field_extended_type, "vsi_reject_field_extended_type.vsi", "field 1 (tag 99) of the container at 8 is an extended field of type 5, which is unknown");
reject!(reject_field_pset_size, "vsi_reject_field_pset_size.vsi", "field 1 (tag 99) of the container at 8 is a property set of size 4");
reject!(reject_ifd_outside, "vsi_reject_ifd_outside.vsi", "the IFD at");
reject!(reject_ifd_loop, "vsi_reject_ifd_loop.vsi", "the IFD chain of the .vsi file returns to the IFD at");
reject!(reject_ifd_count, "vsi_reject_ifd_count.vsi", "more than 65536 IFDs");
reject!(reject_stack_no_size, "vsi_reject_stack_no_size.vsi", "stack 10002 (s) has a tile file but no image size");
reject!(reject_missing_tile_file, "vsi_reject_missing_tile_file.vsi", "stack 10002 (s) has no tile file: neither _vsi_reject_missing_tile_file_/stack10002/frame_t.ets nor");
reject!(reject_sis_magic, "vsi_reject_sis_magic.vsi", "_vsi_reject_sis_magic_/stack10002/frame_t.ets is not an ETS file (magic SOS)");
reject!(reject_sis_header_size, "vsi_reject_sis_header_size.vsi", "_vsi_reject_sis_header_size_/stack10002/frame_t.ets: SIS header size 48, not 64");
reject!(reject_sis_coordinates, "vsi_reject_sis_coordinates.vsi", "_vsi_reject_sis_coordinates_/stack10002/frame_t.ets: 1 coordinates per tile");
reject!(reject_sis_ets_outside, "vsi_reject_sis_ets_outside.vsi", "_vsi_reject_sis_ets_outside_/stack10002/frame_t.ets: the ETS header (228 bytes at 1073741824) is outside");
reject!(reject_sis_tiles_negative, "vsi_reject_sis_tiles_negative.vsi", "_vsi_reject_sis_tiles_negative_/stack10002/frame_t.ets: -1 tiles");
reject!(reject_sis_directory_outside, "vsi_reject_sis_directory_outside.vsi", "_vsi_reject_sis_directory_outside_/stack10002/frame_t.ets: the tile directory (8 tiles at 1073741824) is outside");
reject!(reject_ets_short, "vsi_reject_ets_short.vsi", "_vsi_reject_ets_short_/stack10002/frame_t.ets (24 bytes) is too short for an ETS file");
reject!(reject_ets_magic, "vsi_reject_ets_magic.vsi", "_vsi_reject_ets_magic_/stack10002/frame_t.ets: the ETS header's magic is ETX");
reject!(reject_ets_pixel_type, "vsi_reject_ets_pixel_type.vsi", "_vsi_reject_ets_pixel_type_/stack10002/frame_t.ets: unsupported pixel type 11");
reject!(reject_ets_samples, "vsi_reject_ets_samples.vsi", "_vsi_reject_ets_samples_/stack10002/frame_t.ets: 2 samples per pixel");
reject!(reject_ets_compression_png, "vsi_reject_ets_compression_png.vsi", "_vsi_reject_ets_compression_png_/stack10002/frame_t.ets: unsupported ETS compression 8 (PNG)");
reject!(reject_ets_compression_unknown, "vsi_reject_ets_compression_unknown.vsi", "_vsi_reject_ets_compression_unknown_/stack10002/frame_t.ets: unknown ETS compression 7");
reject!(reject_ets_tile_size, "vsi_reject_ets_tile_size.vsi", "_vsi_reject_ets_tile_size_/stack10002/frame_t.ets: tiles of 0 × 8 pixels");
reject!(reject_ets_tile_depth, "vsi_reject_ets_tile_depth.vsi", "_vsi_reject_ets_tile_depth_/stack10002/frame_t.ets: tiles of depth 2");
reject!(reject_ets_dimensions, "vsi_reject_ets_dimensions.vsi", "_vsi_reject_ets_dimensions_/stack10002/frame_t.ets: 1 dimensions for 3 coordinates per tile");
reject!(reject_dimension_size, "vsi_reject_dimension_size.vsi", "stack 10002: coordinate 2 has the size 0");
reject!(reject_dimension_unknown, "vsi_reject_dimension_unknown.vsi", "stack 10002: coordinate 2 (phase) has 2 values, which the layout cannot place");
reject!(reject_samples_channels, "vsi_reject_samples_channels.vsi", "stack 10002: 3 samples per pixel and 2 channels");
reject!(reject_tile_outside, "vsi_reject_tile_outside.vsi", "_vsi_reject_tile_outside_/stack10002/frame_t.ets: tile 3 (64 bytes at 1073741824) is outside");
reject!(reject_tile_coordinate, "vsi_reject_tile_coordinate.vsi", "_vsi_reject_tile_coordinate_/stack10002/frame_t.ets: tile 6 has a coordinate outside the dimension sizes");
reject!(reject_tile_duplicate, "vsi_reject_tile_duplicate.vsi", "_vsi_reject_tile_duplicate_/stack10002/frame_t.ets: tile 8 repeats the coordinates of an earlier tile");
reject!(reject_tile_raw_size, "vsi_reject_tile_raw_size.vsi", "_vsi_reject_tile_raw_size_/stack10002/frame_t.ets: raw tile 2 has 63 bytes, not 64");
reject!(reject_ifd_image_size, "vsi_reject_ifd_image_size.vsi", "IFD 0 (macro image) is 0 × 8");
reject!(reject_ifd_image_compression, "vsi_reject_ifd_image_compression.vsi", "IFD 0 (macro image) has compression 5");
reject!(reject_ifd_image_samples, "vsi_reject_ifd_image_samples.vsi", "IFD 0 (macro image) has 2 samples per pixel");
reject!(reject_ifd_image_bits, "vsi_reject_ifd_image_bits.vsi", "IFD 0 (macro image) has 16 bits per sample");
reject!(reject_ifd_image_planar, "vsi_reject_ifd_image_planar.vsi", "IFD 0 (macro image) has PlanarConfiguration 2");
reject!(reject_ifd_image_format, "vsi_reject_ifd_image_format.vsi", "IFD 0 (macro image) has SampleFormat 3");
reject!(reject_ifd_image_strips, "vsi_reject_ifd_image_strips.vsi", "IFD 0 (macro image) has 2 strips");
reject!(reject_ifd_strip_outside, "vsi_reject_ifd_strip_outside.vsi", "IFD 0 (macro image): a strip");

/// Every `vsi_reject_*` fixture has a test above.
#[test]
fn every_reject_fixture_has_a_test() {
    let src = include_str!("vsi.rs");
    for e in std::fs::read_dir(fixtures()).unwrap() {
        let path = e.unwrap().path();
        let name = path.file_name().unwrap().to_str().unwrap().to_string();
        if name.starts_with("vsi_reject") && name.ends_with(".vsi") {
            assert!(src.contains(&format!("\"{name}\"")), "{name} has no test");
        }
    }
}
