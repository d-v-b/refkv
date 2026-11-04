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
    let (ir, facts, stats) = run.finish()?;
    refkv_ir::store::store_output(StoreFormat::Nifti, &ir, &facts, stats, "https://example.org/nifti/", true)
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
    assert_eq!(accepted, 8);
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
