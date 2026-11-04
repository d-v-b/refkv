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

/// The (offset, size) of each sample of a track, in decode order: the `data` elements under
/// `samples/<T>`.
fn samples_of(ir: &refkv_ir::ir::Ir, facts: &serde_json::Value, index: usize) -> Vec<(u64, u64)> {
    let track = &facts["tracks"][index];
    let first = track["first"].as_u64().unwrap() as usize;
    (0..track["count"].as_u64().unwrap() as usize).map(|j| (ir.start[first + j], ir.len[first + j])).collect()
}

/// A trun box's samples start where the run before ended when it has no data offset (ISO/IEC
/// 14496-12 §8.8.8.3), which ffmpeg's demuxer does not read, so that write_fixtures.py cannot
/// have it checked against ffmpeg: mp4_frag_offsets.mp4's first traf box has two runs, the
/// second's data offset the first's bytes. Without it, its table moved up and 4 bytes left as the
/// box's rest, the samples are the same.
#[test]
fn a_run_without_a_data_offset_follows_the_run_before() {
    let plain = std::fs::read(common::fixtures().join("mp4/mp4_frag_offsets.mp4")).unwrap();
    let find = |from: usize| from + plain[from..].windows(4).position(|w| w == b"trun").unwrap();
    let (first, second) = (find(0), find(find(0) + 4));
    let (flags, table) = (second + 4, second + 16);
    assert_eq!(plain[flags + 3] & 1, 1);
    let size = u32::from_be_bytes(plain[second - 4..second].try_into().unwrap()) as usize;
    assert!(second > first);
    let mut moved = plain.clone();
    moved[flags + 3] &= !1;
    moved.copy_within(table..second - 4 + size, table - 4);
    let (ir, facts) = refkv_ir::parse_bytes::<Mp4>(&plain).unwrap();
    let (moved_ir, moved_facts) = refkv_ir::parse_bytes::<Mp4>(&moved).unwrap();
    assert!(facts["tracks"][0]["reason"].is_null());
    assert_eq!(samples_of(&moved_ir, &moved_facts, 0), samples_of(&ir, &facts, 0));
    assert!(common::rebuilt(&moved_ir, &moved) == moved);
}

/// A run of a PCM track's samples of one size is one element, however many samples it has, and
/// the elements are the track's bytes: 8192 frames of 4 bytes, in fragments.
#[test]
fn a_pcm_run_is_one_element() {
    let file = std::fs::read(common::fixtures().join("mp4/mov_frag_pcm_s32be.mov")).unwrap();
    let runs = file.windows(4).filter(|w| w == b"trun").count();
    let (ir, facts) = refkv_ir::parse_bytes::<Mp4>(&file).unwrap();
    assert_eq!(facts["tracks"][0]["format"], "pcm");
    assert_eq!(facts["tracks"][0]["frames"], 8192);
    let samples = samples_of(&ir, &facts, 0);
    assert_eq!(samples.len(), runs);
    assert_eq!(samples.iter().map(|s| s.1).sum::<u64>(), 8192 * 4);
    assert!(common::rebuilt(&ir, &file) == file);
}

/// The parser reads the moof boxes and little else: with each mdat box grown by 256 KiB, past the
/// first read and any read ahead, a fragment takes one read.
#[test]
fn a_fragment_takes_one_read() {
    let plain = std::fs::read(common::fixtures().join("mp4/mp4_frag_hevc_moof_base.mp4")).unwrap();
    let (mut data, mut p, mut moofs) = (Vec::new(), 0, 0u64);
    while p < plain.len() {
        let n = u32::from_be_bytes(plain[p..p + 4].try_into().unwrap()) as usize;
        match &plain[p + 4..p + 8] {
            b"mdat" => {
                data.extend_from_slice(&((n + (1 << 18)) as u32).to_be_bytes());
                data.extend_from_slice(&plain[p + 4..p + n]);
                data.resize(data.len() + (1 << 18), 0);
            }
            typ => {
                moofs += (typ == b"moof") as u64;
                data.extend_from_slice(&plain[p..p + n]);
            }
        }
        p += n;
    }
    let mut parser = Mp4::new(data.len() as u64);
    let mut reads = 0;
    while let refkv_ir::nd2::Step::Read(ranges) = parser.step().unwrap() {
        for (o, n) in ranges {
            reads += 1;
            parser.feed(o, data[o as usize..(o + n) as usize].to_vec());
        }
    }
    let (ir, facts) = parser.finish().unwrap();
    assert!(moofs > 1);
    assert!(reads <= moofs + 2, "{reads} reads of {moofs} moof boxes");
    assert!(facts["tracks"][0]["reason"].is_null());
    assert!(common::rebuilt(&ir, &data) == data);
}

/// The moof boxes together are at most 64 MiB, checked before the one past it is read.
#[test]
fn moof_boxes_past_the_limit_fail() {
    let size = 1u64 << 28;
    let head = [&((1u32 << 26) + 1).to_be_bytes()[..], b"moof", &[0; 8]].concat();
    let mut parser = Mp4::new(size);
    parser.step().unwrap();
    parser.feed(0, head);
    let m = parser.step().err().unwrap();
    assert_eq!(m, "the moof boxes are 67108865 bytes in all, more than 64 MiB");
}
