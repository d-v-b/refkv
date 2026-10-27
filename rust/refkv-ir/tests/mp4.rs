//! The MP4 parser (src/mp4.rs; spec/zarrify/mp4.md): one test per rejected fixture.

mod common;

use refkv_ir::mp4::Mp4;

// one test per rejected fixture, each the message its check gives
// one test per case of conformance/zarrify/rejects/mp4.json (build.rs), each the message its check gives
macro_rules! reject {
    ($name:ident, $input:literal, $msg:literal) => {
        common::reject!($name, Mp4, concat!("mp4/", $input), $msg);
    };
}
include!(concat!(env!("OUT_DIR"), "/rejects_mp4.rs"));

/// The ftyp box read gives the brands, with a 64-bit size too; a second ftyp box, larger than
/// the first, is opaque.
#[test]
fn ftyp_boxes_are_read() {
    let plain = std::fs::read(common::fixtures().join("mp4/mp4_h264_baseline.mp4")).unwrap();
    let brand = serde_json::json!({"major": "isom", "minor": 512, "compatible": ["isom", "iso2", "avc1", "mp41"]});
    // the 32-byte ftyp box and the 8-byte free box after it, as one 40-byte ftyp box with a
    // 16-byte header
    assert_eq!(&plain[32..40], b"\0\0\0\x08free");
    let mut large = [&1u32.to_be_bytes()[..], b"ftyp", &40u64.to_be_bytes()].concat();
    large.extend_from_slice(&plain[8..32]);
    large.extend_from_slice(&plain[40..]);
    let mut second = plain.clone();
    second.extend_from_slice(&[&48u32.to_be_bytes()[..], b"ftyp", b"mp42", &[0; 4], &[b'm'; 32]].concat());
    for data in [plain, large, second] {
        let (ir, facts) = refkv_ir::parse_bytes::<Mp4>(&data).unwrap();
        assert_eq!(facts["brand"], brand);
        assert!(common::rebuilt(&ir, &data) == data);
    }
}

/// An ftyp box too short for its major brand and minor version, with either header: in place
/// of a file's 32-byte ftyp box and the 8-byte free box after it, a 4-byte payload, and a free
/// box for the rest of the 40 bytes.
#[test]
fn an_ftyp_box_too_short_for_its_fields_fails() {
    let plain = std::fs::read(common::fixtures().join("mp4/mp4_h264_baseline.mp4")).unwrap();
    let short = [&12u32.to_be_bytes()[..], b"ftypisom", &28u32.to_be_bytes(), b"free", &[0; 20]].concat();
    let large = [&1u32.to_be_bytes()[..], b"ftyp", &20u64.to_be_bytes(), b"isom", &20u32.to_be_bytes(), b"free", &[0; 12]].concat();
    for head in [short, large] {
        let data = [&head[..], &plain[40..]].concat();
        let m = refkv_ir::parse_bytes::<Mp4>(&data).err().unwrap();
        assert_eq!(m, "the ftyp box at 0 has 4 bytes, too few for its 8 bytes of fields");
    }
}
