//! The H5J parser (src/h5j.rs, the HDF5 parser's last phase; spec/zarrify/h5j.md): one test per
//! rejected fixture, and the IR of an accepted one.

mod common;

use common::{fixtures, rebuilt};
use refkv_ir::check;
use refkv_ir::hdf5::file::H5jParser;

// one test per case of conformance/zarrify/rejects/h5j.json (build.rs), each the message its check gives
macro_rules! reject {
    ($name:ident, $input:literal, $msg:literal) => {
        common::reject!($name, H5jParser, concat!("h5j/", $input), $msg);
    };
}
include!(concat!(env!("OUT_DIR"), "/rejects_h5j.rs"));

/// Each channel's MP4 file is in the IR in place of its dataset's data: the IR is valid, rebuilds
/// the file, and holds each sample as a data element of the H5J file's bytes; a channel the
/// profile records keeps its data element.
#[test]
fn a_channel_s_mp4_file_takes_the_place_of_its_data() {
    for (name, presented) in [("h5j_two_channels.h5j", vec!["Channel_0", "Channel_1"]), ("h5j_recorded.h5j", vec!["Channel_0"])] {
        let data = std::fs::read(fixtures().join("h5j").join(name)).unwrap();
        let (ir, facts) = refkv_ir::parse_bytes::<H5jParser>(&data).unwrap();
        check::check(&ir).unwrap();
        assert_eq!(rebuilt(&ir, &data), data, "{name}");
        // every channel whose MP4 file reads is grafted, presented or not: its track's first sample
        // is an element of the channel's dataset
        for c in facts["h5j"]["channels"].as_array().unwrap().iter().filter(|c| !c["mp4"].is_null()) {
            let (offset, length) = (c["offset"].as_u64().unwrap(), c["length"].as_u64().unwrap());
            for t in c["mp4"]["tracks"].as_array().unwrap() {
                let first = t["elements"]["first"].as_u64().unwrap() as usize;
                assert!(ir.path(first as u32).ends_with("/data/samples/0/0"), "{name}: {}", ir.path(first as u32));
                assert!(ir.start[first] >= offset && ir.start[first] < offset + length, "{name}: a sample outside its channel");
            }
        }
        let nodes = refkv_ir::project::hdf5_nodes::nodes(&facts).unwrap().nodes;
        let planned = refkv_ir::project::h5j::plan(&facts, &nodes);
        let channels = planned["channels"].as_array().unwrap();
        let got: Vec<&str> = channels.iter().filter(|c| c["reason"].is_null()).map(|c| c["name"].as_str().unwrap()).collect();
        assert_eq!(got, presented, "{name}");
        for c in channels.iter().filter(|c| c["reason"].is_null()) {
            let (offset, length) = (c["offset"].as_u64().unwrap(), c["length"].as_u64().unwrap());
            let first = c["track"]["first"].as_u64().unwrap() as usize;
            let count = c["track"]["count"].as_u64().unwrap() as usize;
            for j in first..first + count {
                assert!(ir.path(j as u32).ends_with(&format!("/data/samples/0/{}", j - first)), "{}", ir.path(j as u32));
                assert!(ir.start[j] >= offset && ir.start[j] + ir.len[j] <= offset + length, "{name}: a sample outside its channel");
            }
        }
    }
}
