//! The NIfTI parser (src/nifti.rs; spec/zarrify/nifti.md): one test per rejected fixture.

mod common;

use refkv_ir::nd2::Step;
use refkv_ir::nifti::Nifti;
use refkv_ir::run::Format;

// one test per rejected fixture, each the message its check gives
// one test per case of conformance/zarrify/rejects/nifti.json (build.rs), each the message its check gives
macro_rules! reject {
    ($name:ident, $input:literal, $msg:literal) => {
        common::reject!($name, Nifti, concat!("nifti/", $input), $msg);
    };
}
include!(concat!(env!("OUT_DIR"), "/rejects_nifti.rs"));

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
