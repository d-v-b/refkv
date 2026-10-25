//! The FLAC parser (src/flac.rs; spec/zarrify/flac.md): one test per rejected fixture.

mod common;

use refkv_ir::flac::Flac;

// one test per case of conformance/zarrify/rejects/flac.json (build.rs), each the message its check gives
macro_rules! reject {
    ($name:ident, $input:literal, $msg:literal) => {
        common::reject!($name, Flac, concat!("flac/", $input), $msg);
    };
}
include!(concat!(env!("OUT_DIR"), "/rejects_flac.rs"));

/// The ranges a parse of `data` asks for, in order, and its IR and facts.
fn reads(data: &[u8]) -> (Vec<(u64, u64)>, refkv_ir::ir::Ir) {
    let mut p = Flac::new(data.len() as u64);
    let mut asked = Vec::new();
    while let refkv_ir::nd2::Step::Read(r) = p.step().unwrap() {
        for (o, n) in r {
            asked.push((o, n));
            p.feed(o, data[o as usize..(o + n) as usize].to_vec());
        }
    }
    (asked, p.finish().unwrap().0)
}

/// A file whose 1 MiB picture lies between STREAMINFO and a padding block before the frames
/// (flac_mono_16.flac's): the profile reads the first 64 KiB, the padding block's header with the
/// 64 KiB after it, and the frames past that, but not the picture's data past the first window
/// (spec/zarrify/flac.md §8.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/flac.md#81-the-stream>);
/// the file is rebuilt from the IR.
#[test]
fn a_pictures_data_is_not_read() {
    let src = std::fs::read(common::fixtures().join("flac/flac_mono_16.flac")).unwrap();
    let (info, frames) = (&src[8..42], &src[42..]);
    let mut pic = vec![0, 0, 0, 3, 0, 0, 0, 9];
    pic.extend_from_slice(b"image/png");
    pic.extend_from_slice(&[0u8; 20]);
    pic.extend_from_slice(&(1u32 << 20).to_be_bytes());
    pic.extend((0..1u32 << 20).map(|i| (i % 251) as u8));
    let mut data = b"fLaC\x00\x00\x00\x22".to_vec();
    data.extend_from_slice(info);
    data.push(6);
    data.extend_from_slice(&(pic.len() as u32).to_be_bytes()[1..]);
    data.extend_from_slice(&pic);
    let padding = data.len() as u64;
    data.extend_from_slice(&[0x81, 0, 0, 16]);
    data.extend_from_slice(&[0; 16]);
    data.extend_from_slice(frames);
    let (asked, ir) = reads(&data);
    let picture = (42 + 4 + pic.len() as u64 - (1 << 20), padding);
    let n = data.len() as u64;
    assert_eq!(asked, vec![(0, 1 << 16), (n - 128, 128), (padding, n - padding)]);
    assert!(asked.iter().all(|&(o, len)| o + len <= picture.0 || o >= picture.1 || o + len <= 1 << 16));
    assert_eq!(common::rebuilt(&ir, &data), data);
}

/// The run's output says whether a sharded array's ranges were joined: not for a host that
/// records checksums (spec/zarrify.md §2.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#21-virtual-shards>).
#[test]
fn the_host_says_whether_it_records_checksums() {
    use refkv_ir::run::{Format, Run};
    let data = std::fs::read(common::fixtures().join("flac/flac_blocks.flac")).unwrap();
    for checksums in [false, true] {
        let mut r = Run::new(Format::Flac, data.len() as u64, false, 4);
        while let Some(reqs) = r.poll().unwrap() {
            for (k, q) in reqs.iter().enumerate() {
                let parts = q.spans.iter().map(|&(a, b)| data[a as usize..b as usize].to_vec()).collect();
                r.complete(k, Some(parts), 0.0).unwrap();
            }
        }
        let out = r.into_output("https://data.test/a.flac", false, checksums).unwrap();
        assert_eq!(out.summary["joinRanges"], serde_json::json!(!checksums));
    }
}

/// The frames, and the IR, of a parse that reads `window` bytes of frames a round (from the
/// file's first 64 KiB on: `data` is padded past it with metadata), and its rounds.
fn windowed(data: &[u8], window: u64) -> (refkv_ir::ir::Ir, u64) {
    let mut p = Flac::with_window(data.len() as u64, window);
    let mut rounds = 0;
    while let refkv_ir::nd2::Step::Read(r) = p.step().unwrap() {
        rounds += 1;
        for (o, n) in r {
            p.feed(o, data[o as usize..(o + n) as usize].to_vec());
        }
    }
    (p.finish().unwrap().0, rounds)
}

/// A file whose frames start past its first 64 KiB (a padding block before them), read in
/// windows of 1 byte, 7, 100 (frames of about 46 bytes, so that frames and headers straddle the
/// reads) and the default: the same IR, frame for frame, in more rounds for smaller windows.
#[test]
fn frames_found_across_windows_are_the_same() {
    let src = std::fs::read(common::fixtures().join("flac/flac_many_frames.flac")).unwrap();
    let (info, frames) = (&src[8..42], &src[42..]);
    let mut data = b"fLaC\x00\x00\x00\x22".to_vec();
    data.extend_from_slice(info);
    data.extend_from_slice(&[0x81, 0x01, 0x00, 0x00]);
    data.extend_from_slice(&[0; 1 << 16]);
    data.extend_from_slice(frames);
    let (want, _) = windowed(&data, refkv_ir::flac::WINDOW);
    let mut rounds = Vec::new();
    for window in [1, 7, 100] {
        let (ir, n) = windowed(&data, window);
        assert_eq!((&ir.start, &ir.len, &ir.kind), (&want.start, &want.len, &want.kind), "{window}");
        rounds.push(n);
    }
    assert!(rounds[0] > rounds[1] && rounds[1] > rounds[2] && rounds[2] > 2, "{rounds:?}");
}
