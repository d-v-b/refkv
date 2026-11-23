//! A run's chunking (`out::Chunking`) is its own: runs with different chunkings, driven in one
//! process and interleaved request by request, each give the output a run with its chunking gives
//! alone. Before d-v-b/refkv#459's step 2b the settings were process-wide, and a run read whatever
//! the last one set.

mod common;

use refkv_ir::out::{Chunking, Out};
use refkv_ir::run::{Format, Run};

/// What an output holds, comparable.
fn held(out: &Out) -> String {
    format!("{:?} {:?} {:?} {}", out.entries, out.refs, out.data, out.summary)
}

/// The outputs of a run of `format` over `data` for each chunking, the runs interleaved: each
/// answers one batch of requests in turn, until all are done.
fn interleaved(format: Format, data: &[u8], chunkings: &[Chunking]) -> Vec<Out> {
    let mut runs: Vec<Option<Run>> = chunkings.iter().map(|&c| Some(Run::with_chunking(format, data.len() as u64, false, 4, c))).collect();
    let mut outs: Vec<Option<Out>> = chunkings.iter().map(|_| None).collect();
    while outs.iter().any(Option::is_none) {
        for (k, slot) in runs.iter_mut().enumerate() {
            let Some(run) = slot.as_mut() else { continue };
            match run.poll().unwrap() {
                Some(requests) => {
                    for (i, q) in requests.iter().enumerate() {
                        let parts = q.spans.iter().map(|&(a, b)| data[a as usize..b as usize].to_vec()).collect();
                        run.complete(i, Some(parts), 0.0).unwrap();
                    }
                }
                None => outs[k] = Some(slot.take().unwrap().into_output("https://data.test/f", true, false).unwrap()),
            }
        }
    }
    outs.into_iter().map(Option::unwrap).collect()
}

#[test]
fn runs_with_different_chunkings_in_one_process_do_not_interfere() {
    let small = Chunking::new(Some(8), Some(4), Some(3));
    let cases = [
        (Format::Mp3, "mp3/mp3_mpeg1_stereo_cbr.mp3", Some(("chunkFrames", 4))),
        (Format::Flac, "flac/flac_many_frames.flac", Some(("shardFrames", 3))),
        // its edge chunk is copied past 8 bytes, and referenced with its padding below
        (Format::Nifti, "nifti/nifti_chunk_edge_literal.nii", None),
    ];
    for (format, fixture, small_member) in cases {
        let data = std::fs::read(common::fixtures().join(fixture)).unwrap();
        let alone: Vec<String> = [Chunking::default(), small].iter().map(|&c| held(&interleaved(format, &data, &[c])[0])).collect();
        assert_ne!(alone[0], alone[1], "{fixture}: the chunkings give the same output");
        let both = interleaved(format, &data, &[Chunking::default(), small, Chunking::default()]);
        assert_eq!([held(&both[0]), held(&both[1]), held(&both[2])], [&alone[0], &alone[1], &alone[0]].map(String::clone), "{fixture}");
        if let Some((member, frames)) = small_member {
            assert_eq!(both[1].summary[member], frames, "{fixture}");
        }
    }
}
