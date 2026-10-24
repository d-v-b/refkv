//! The TIFF parser (src/tiff.rs; spec/virtualize/tiff.md): one test per rejected fixture; NDPI
//! found as NDPI; and the round-3 probes built here (IFD loops, huge counts, too many IFDs).

mod common;

use common::{fixtures, rebuilt};
use refkv_ir::check;
use refkv_ir::parse_bytes;
use refkv_ir::tiff::Tiff;

// one test per rejected fixture, each the message its check gives
// one test per case of conformance/virtualize/rejects/tiff.json (build.rs), each the message its check gives
macro_rules! reject {
    ($name:ident, $input:literal, $msg:literal) => {
        common::reject!($name, Tiff, concat!("tiff/", $input), $msg);
    };
}
include!(concat!(env!("OUT_DIR"), "/rejects_tiff.rs"));

/// The TIFF parser reads an NDPI as NDPI (spec/virtualize/ndpi.md §7
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/ndpi.md#7-ndpi-profile>): each
/// accepted NDPI fixture's facts name the profile, and a TIFF's do not.
#[test]
fn the_tiff_parser_finds_ndpi() {
    for (file, ndpi) in [("ndpi/ndpi_levels.ndpi", true), ("ndpi/ndpi_pointers.ndpi", true), ("tiff/jpeg_gray.tif", false)] {
        let data = std::fs::read(fixtures().join(file)).unwrap();
        let (_, facts) = parse_bytes::<Tiff>(&data).unwrap();
        assert_eq!(facts["profile"] == "ndpi", ndpi, "{file}");
    }
}

// ---- the round-3 TIFF probes, built here

/// A little-endian classic TIFF: `blobs` after the header, then the IFDs (sorted
/// entries (tag, type, count, value)), each IFD's next given by `next` (an index).
fn tiff(ifds: &[Vec<(u16, u16, u32, u32)>], blobs: &[u8], next: &[Option<usize>]) -> Vec<u8> {
    let start = 8 + blobs.len();
    let mut offs = Vec::new();
    let mut at = start;
    for e in ifds {
        offs.push(at as u32);
        at += 2 + 12 * e.len() + 4;
    }
    let mut out = b"II*\0".to_vec();
    out.extend_from_slice(&offs[0].to_le_bytes());
    out.extend_from_slice(blobs);
    for (k, e) in ifds.iter().enumerate() {
        let mut e = e.clone();
        e.sort();
        out.extend_from_slice(&(e.len() as u16).to_le_bytes());
        for (t, ty, c, v) in e {
            out.extend_from_slice(&t.to_le_bytes());
            out.extend_from_slice(&ty.to_le_bytes());
            out.extend_from_slice(&c.to_le_bytes());
            out.extend_from_slice(&v.to_le_bytes());
        }
        out.extend_from_slice(&next[k].map(|j| offs[j]).unwrap_or(0).to_le_bytes());
    }
    out
}

/// A tiled 32 x 32 8-bit image's entries and its four tiles and tables.
fn image() -> (Vec<(u16, u16, u32, u32)>, Vec<u8>) {
    let mut blobs = vec![0u8; 1024];
    let at = 8 + blobs.len() as u32;
    for k in 0..4u32 {
        blobs.extend_from_slice(&(8 + 256 * k).to_le_bytes());
    }
    for _ in 0..4 {
        blobs.extend_from_slice(&256u32.to_le_bytes());
    }
    let e = vec![
        (256, 4, 1, 32), (257, 4, 1, 32), (258, 3, 1, 8), (259, 3, 1, 1), (262, 3, 1, 1), (277, 3, 1, 1),
        (322, 3, 1, 16), (323, 3, 1, 16), (324, 4, 4, at), (325, 4, 4, at + 16),
    ];
    (e, blobs)
}

fn rejects(data: &[u8], msg: &str) {
    match parse_bytes::<Tiff>(data) {
        Err(m) => assert!(m.contains(msg), "{m}"),
        Ok(_) => panic!("accepted"),
    }
}

#[test]
fn tiff_probes_parse() {
    let (e, blobs) = image();
    // one IFD, then IFD 0 with an EXIF pointer to a second IFD that points back to both: aliases
    let first = 8 + blobs.len() as u32;
    let second = first + 2 + 12 * 11 + 4;
    let mut with_exif = e.clone();
    with_exif.push((34665, 4, 1, second));
    let cases = [
        tiff(&[e.clone()], &blobs, &[None]),
        tiff(&[with_exif, vec![(34665, 4, 1, first), (34853, 4, 1, second)]], &blobs, &[None, None]),
        // a tag the profile does not read, with a count whose value would be 16 GiB: not read
        tiff(&[[e.clone(), vec![(65000, 7, u32::MAX, 64)]].concat()], &blobs, &[None]),
    ];
    for data in cases {
        let (ir, facts) = parse_bytes::<Tiff>(&data).unwrap();
        check::check(&ir).unwrap();
        assert_eq!(rebuilt(&ir, &data), data);
        assert_eq!(facts["levels"][0]["w"], 32);
        assert!(facts["requested"].as_u64().unwrap() < 4 * data.len() as u64);
    }
}

#[test]
fn a_chain_that_loops_is_rejected() {
    let (e, blobs) = image();
    rejects(&tiff(&[e.clone(), e], &blobs, &[Some(1), Some(0)]), "read twice");
}

#[test]
fn an_ifd_that_is_its_own_next_is_rejected() {
    let (e, blobs) = image();
    rejects(&tiff(&[e], &blobs, &[Some(0)]), "read twice");
}

#[test]
fn a_subifd_that_is_its_parent_is_rejected() {
    let (mut e, blobs) = image();
    e.push((330, 4, 1, 8 + blobs.len() as u32));
    rejects(&tiff(&[e], &blobs, &[None]), "read twice");
}

#[test]
fn a_bigtiff_entry_count_past_the_file_is_rejected() {
    let mut data = b"II+\0".to_vec();
    data.extend_from_slice(&8u16.to_le_bytes());
    data.extend_from_slice(&0u16.to_le_bytes());
    data.extend_from_slice(&16u64.to_le_bytes());
    data.extend_from_slice(&(1u64 << 40).to_le_bytes());
    data.extend_from_slice(&[0; 64]);
    rejects(&data, "not within the file");
}

#[test]
fn a_tile_table_count_past_the_file_is_rejected() {
    let (mut e, blobs) = image();
    e[8] = (324, 4, u32::MAX, 64);
    rejects(&tiff(&[e], &blobs, &[None]), "the value of tag 324 is outside the file");
}

#[test]
fn more_than_100000_ifds_are_rejected() {
    let ifds = vec![vec![(256u16, 4u16, 1u32, 1u32)]; 100001];
    let next: Vec<Option<usize>> = (0..ifds.len()).map(|k| (k + 1 < ifds.len()).then_some(k + 1)).collect();
    rejects(&tiff(&ifds, &[], &next), "too many IFDs");
}
