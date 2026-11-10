//! The NIfTI parser (src/nifti.rs; spec/zarrify/nifti.md): one test per rejected fixture.

mod common;

use refkv_ir::nd2::Step;
use refkv_ir::nifti::Nifti;
use refkv_ir::out::Part;
use refkv_ir::run::Format;
use refkv_ir::store::{StoreFormat, StoreRun};

// one test per case of conformance/zarrify/rejects/nifti.json (build.rs), each the message its check gives:
// a file's by its parser, a header-and-image pair's by a store run, the file beside it found in the directory
macro_rules! reject {
    ($name:ident, $input:literal, $msg:literal) => {
        #[test]
        fn $name() {
            let m = rejection($input);
            assert!(m.starts_with($msg), "{m}");
        }
    };
}

fn fixture(key: &str) -> Option<Vec<u8>> {
    std::fs::read(common::fixtures().join("nifti").join(key)).ok()
}

/// The rejection of the input `key` of fixtures/nifti.
fn rejection(key: &str) -> String {
    let data = fixture(key).unwrap();
    if refkv_ir::nifti::detect_pair(&data).is_some() {
        return run_pair(key, &data, fixture).err().expect("the pair was accepted");
    }
    refkv_ir::parse_bytes::<Nifti>(&data).err().expect("the file was accepted")
}

/// A store run of the pair whose header file is `key`, the image file found by `beside`.
fn run_pair(key: &str, data: &[u8], beside: impl Fn(&str) -> Option<Vec<u8>>) -> Result<refkv_ir::out::Out, String> {
    let (ir, facts, stats) = parse_pair(key, data, beside)?;
    refkv_ir::store::store_output(StoreFormat::Nifti, &ir, &facts, stats, "https://example.org/nifti/", true, false).map_err(String::from)
}

/// The store IR, the facts and the run's counts of the pair whose header file is `key`, the image file found by `beside`.
fn parse_pair(key: &str, data: &[u8], beside: impl Fn(&str) -> Option<Vec<u8>>) -> Result<(refkv_ir::ir::Ir, serde_json::Value, serde_json::Value), String> {
    let mut run = StoreRun::new(StoreFormat::Nifti, vec![(key.to_string(), data.len() as u64)], 4)?;
    while let Some(reqs) = run.poll()? {
        if reqs.is_empty() {
            for k in run.probes().to_vec() {
                run.found(beside(&k).map(|d| d.len() as u64))?;
            }
            continue;
        }
        for (k, q) in reqs.iter().enumerate() {
            let object = if q.key == key { data.to_vec() } else { beside(&q.key).unwrap() };
            run.complete(k, q.spans.iter().map(|&(a, b)| object[a as usize..b as usize].to_vec()).collect())?;
        }
    }
    run.finish()
}

include!(concat!(env!("OUT_DIR"), "/rejects_nifti.rs"));

/// A file with the pair's magic names a sibling: its file run ends there, and a store run reads it.
#[test]
fn a_file_with_the_pairs_magic_starts_over_as_a_store() {
    let data = fixture("nifti_pair_n1_le_int16.hdr").unwrap();
    assert_eq!(Format::sniff(&data).map(|f| f.name()), Some("nifti"));
    let mut parser = Nifti::new(data.len() as u64);
    while let Step::Read(ranges) = parser.step().unwrap() {
        for (o, n) in ranges {
            parser.feed(o, data[o as usize..(o + n) as usize].to_vec());
        }
    }
    assert!(matches!(parser.step().unwrap(), Step::Beside));
}

/// Every pair the fixtures hold is accepted, as many arrays as a single file's: one.
#[test]
fn accepted_pairs_have_one_array() {
    let mut accepted = 0;
    for entry in std::fs::read_dir(common::fixtures().join("nifti")).unwrap() {
        let key = entry.unwrap().file_name().into_string().unwrap();
        if key.starts_with("nifti_pair_") && key.to_lowercase().ends_with(".hdr") {
            let out = run_pair(&key, &fixture(&key).unwrap(), fixture).unwrap();
            assert!(out.entries.iter().any(|e| e.0 == "0/zarr.json"), "{key}");
            accepted += 1;
        }
    }
    assert_eq!(accepted, 9);
}

/// A pair's references are ranges of its image file; its image file's bytes before the voxels pass over.
#[test]
fn a_pairs_chunks_are_ranges_of_its_image_file() {
    let key = "nifti_pair_n1_le_offset.hdr";
    let data = fixture(key).unwrap();
    let out = run_pair(key, &data, fixture).unwrap();
    assert_eq!(out.refs.len(), 1);
    assert_eq!(out.refs[0].1, vec![Part::Obj("nifti_pair_n1_le_offset.img".into(), 16, 24)]);
}

/// A header file past the limit of what the mirror holds is rejected; no fixture is that long.
#[test]
fn a_header_file_past_the_limit_is_rejected() {
    let mut data = fixture("nifti_pair_n1_le_int16.hdr").unwrap();
    data.resize((1 << 24) + 1, 0);
    let m = run_pair("long.hdr", &data, fixture).err().unwrap();
    assert!(m.starts_with("the header file \"long.hdr\" of 16777217 bytes"), "{m}");
}

/// A gzip stream that holds a pair's header file matches no signature, as it has no image file to read.
#[test]
fn a_gzipped_header_file_is_of_no_format() {
    let data = refkv_ir::out::gzip(&fixture("nifti_pair_n1_le_int16.hdr").unwrap());
    assert_eq!(Format::sniff(&data).map(|f| f.name()), None);
}

/// A file gzipped whole is chosen by what its first bytes inflate to, and read at its start and
/// its trailer alone, however long its stream (spec/zarrify/nifti.md §7.4
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nifti.md#74-gzipped-files>).
#[test]
fn a_gzipped_file_is_read_at_its_start_and_its_trailer() {
    let mut plain = vec![0u8; 352];
    plain[0..4].copy_from_slice(&348i32.to_le_bytes());
    for (at, value) in [(40, 2i16), (42, 1024), (44, 1024), (70, 2), (72, 8)] {
        plain[at..at + 2].copy_from_slice(&value.to_le_bytes());
    }
    plain[108..112].copy_from_slice(&352f32.to_le_bytes());
    plain[344..348].copy_from_slice(b"n+1\0");
    // voxels that do not compress, so that the stream is far longer than a read
    let mut state = 88172645463325252u64;
    plain.extend((0..1 << 20).map(|_| {
        state ^= state << 13;
        state ^= state >> 7;
        state ^= state << 17;
        state as u8
    }));
    let file = refkv_ir::out::gzip(&plain);
    let size = file.len() as u64;
    assert!(size > 1 << 19);
    assert_eq!(Format::sniff(&file[..552]).map(|f| f.name()), Some("nifti"));

    let mut parser = Nifti::new(size);
    let mut reads = Vec::new();
    while let Step::Read(ranges) = parser.step().unwrap() {
        for (o, n) in ranges {
            reads.push((o, n));
            parser.feed(o, file[o as usize..(o + n) as usize].to_vec());
        }
    }
    assert!(reads.iter().all(|&(o, n)| o + n <= 1 << 16 || (o, n) == (size - 4, 4)), "{reads:?}");
    let (_, facts) = parser.finish().unwrap();
    assert_eq!(facts["gzip"]["skip"], 352);
}

/// Every accepted fixture's facts name its voxels' description by its key (spec/stored-array.md §7
/// <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): `declared_by` is
/// the id of the `header` element of a single file, or of a pair's header file, and of a gzipped
/// file's one element, its stream. The IR's one description is keyed by it, with no index, and its
/// one row's element is the facts' `voxels`.
#[test]
fn every_accepted_fixtures_facts_name_its_description() {
    let (mut single, mut gzipped, mut pairs) = (0, 0, 0);
    for entry in std::fs::read_dir(common::fixtures().join("nifti")).unwrap() {
        let key = entry.unwrap().file_name().into_string().unwrap();
        let lower = key.to_lowercase();
        if key.contains("reject") || lower.ends_with(".img") {
            continue;
        }
        let data = fixture(&key).unwrap();
        let (ir, facts, declaring) = if lower.ends_with(".hdr") {
            pairs += 1;
            let (ir, facts, _) = parse_pair(&key, &data, fixture).unwrap();
            (ir, facts, format!("{key}/header"))
        } else {
            let (ir, facts) = refkv_ir::parse_bytes::<Nifti>(&data).unwrap();
            let stream = lower.ends_with(".gz");
            assert_eq!(facts["gzip"].is_object(), stream, "{key}");
            gzipped += stream as u32;
            single += !stream as u32;
            (ir, facts, if stream { "voxels" } else { "header" }.to_string())
        };
        let declared = facts["declared_by"].as_u64().unwrap_or_else(|| panic!("{key}: no declared_by")) as u32;
        assert_eq!(ir.path(declared), declaring, "{key}");
        let s = &ir.stored;
        assert_eq!(s.arrays.len(), 1, "{key}");
        assert_eq!((s.arrays[0].element, s.arrays[0].index), (declared, None), "{key}");
        assert!(s.arrays[0].grid.is_some(), "{key}");
        assert_eq!(s.map.len(), 1, "{key}");
        assert_eq!(s.map.element[0].map(u64::from), facts["voxels"].as_u64(), "{key}");
    }
    assert_eq!((single, gzipped, pairs), (70, 5, 9));
}
