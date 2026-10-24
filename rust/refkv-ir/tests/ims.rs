//! The IMS parser (src/ims.rs; spec/virtualize/ims.md): one test per rejected fixture, and the
//! walk's room.

mod common;

use common::{fixtures, rebuilt, reject};
use refkv_ir::check;
use refkv_ir::ims::Ims;

// one test per rejected fixture, each the message its check gives
reject!(ims_reject_chunk_mismatch, Ims, "ims/ims_reject_chunk_mismatch.ims", "the datasets of level 0 differ in size, chunk shape or filters");
reject!(ims_reject_contiguous, Ims, "ims/ims_reject_contiguous.ims", "dataset layout class 1 is not chunked");
reject!(ims_reject_dtype_mismatch, Ims, "ims/ims_reject_dtype_mismatch.ims", "the datasets have different data types");
reject!(ims_reject_fill_value, Ims, "ims/ims_reject_fill_value.ims", "the fill value is not zero");
reject!(ims_reject_fletcher32, Ims, "ims/ims_reject_fletcher32.ims", "unsupported HDF5 filters [3]");
reject!(ims_reject_float64, Ims, "ims/ims_reject_float64.ims", "unsupported HDF5 datatype (class 1, 8 bytes)");
reject!(ims_reject_image_larger, Ims, "ims/ims_reject_image_larger.ims", "the image of level 0 is larger than its dataset");
reject!(ims_reject_imagesize_text, Ims, "ims/ims_reject_imagesize_text.ims", "ImageSizeX of level 0 is not a positive integer");
reject!(ims_reject_imagesize_vlen, Ims, "ims/ims_reject_imagesize_vlen.ims", "attribute ImageSizeX is not a string");
reject!(ims_reject_missing_channel, Ims, "ims/ims_reject_missing_channel.ims", "no Channel 1");
reject!(ims_reject_not_imaris, Ims, "ims/ims_reject_not_imaris.ims", "not an Imaris file (no DataSet group)");
reject!(ims_reject_offset_size, Ims, "ims/ims_reject_offset_size.ims", "HDF5 offsets and lengths of 4 and 8 bytes are not supported");
reject!(ims_reject_rank, Ims, "ims/ims_reject_rank.ims", "a dataset of level 0 is not 3-dimensional");
reject!(ims_reject_raw_edges_image, Ims, "ims/ims_reject_raw_edges_image.ims", "a chunk's filter mask is not 0");
reject!(ims_reject_relative_soft_link, Ims, "ims/ims_reject_relative_soft_link.ims", "soft link \"Data\" to a relative path");
reject!(ims_reject_shuffle, Ims, "ims/ims_reject_shuffle.ims", "unsupported HDF5 filters [2, 1]");
reject!(ims_reject_size_mismatch, Ims, "ims/ims_reject_size_mismatch.ims", "the datasets of level 0 differ in size, chunk shape or filters");
reject!(ims_reject_z_levels, Ims, "ims/ims_reject_z_levels.ims", "a level has more than one z plane, and level 0 has one");

/// Runs the IMS parser over bytes in memory, the walk's room in elements set to `room`.
fn ims_with_room(data: &[u8], room: Option<u64>) -> (refkv_ir::ir::Ir, serde_json::Value) {
    let mut p = Ims::new(data.len() as u64);
    p.room = room;
    loop {
        match p.step().unwrap() {
            refkv_ir::nd2::Step::Read(r) => {
                for (o, n) in r {
                    p.feed(o, data[o as usize..(o + n) as usize].to_vec());
                }
            }
            refkv_ir::nd2::Step::Done => break,
        }
    }
    p.finish().unwrap()
}

#[test]
fn past_the_walks_room_the_image_is_described_and_the_rest_is_gaps() {
    let project = |ir: &refkv_ir::ir::Ir, facts: &serde_json::Value| {
        let out = refkv_ir::project::project(refkv_ir::run::Format::Ims, ir, facts, "u", &Default::default()).unwrap();
        out.refs.iter().map(|(k, p)| format!("{k}{p:?}")).collect::<Vec<_>>()
    };
    // dense links and attributes (fractal heaps, version 2 B-trees), huge attributes, and
    // variable-length attributes (global heap collections)
    for name in ["ims_latest_many_channels", "ims_latest_dense_links", "ims_latest_huge_attribute", "ims_latest_review2",
                 "ims_earliest_review2"] {
        let data = std::fs::read(fixtures().join(format!("ims/{name}.ims"))).unwrap();
        let (all, facts) = ims_with_room(&data, None);
        let n = all.len() as u64;
        for room in [0, n / 8, n / 4, n / 2, 3 * n / 4] {
            let (ir, f) = ims_with_room(&data, Some(room));
            check::check(&ir).unwrap();
            assert!(rebuilt(&ir, &data) == data);
            assert!(ir.len() < all.len(), "{name} room {room}");
            assert_eq!(project(&ir, &f), project(&all, &facts), "{name} room {room}");
            // the elements outside the image's datasets and the objects' structs: at most the
            // room, and a few more (each loop stops at the room, within one structure)
            let paths: Vec<String> = (0..ir.len() as u32).filter(|&i| ir.kind[i as usize] != refkv_ir::ir::GAP).map(|i| ir.path(i)).collect();
            let images: std::collections::HashSet<String> =
                paths.iter().filter_map(|p| p.strip_suffix("/chunks")).map(|p| p.to_string()).collect();
            let outside = |p: &String| {
                let parts: Vec<&str> = p.splitn(3, '/').collect();
                !(parts[0] == "objects" && parts.len() >= 2 && (parts.len() == 2 || images.contains(&format!("objects/{}", parts[1]))))
            };
            let rest: Vec<&String> = paths.iter().filter(|p| outside(p)).collect();
            assert!(rest.len() <= room as usize + 30, "{name} room {room}: {} elements outside the image", rest.len());
            if room == 0 {
                // nothing of a non-image object but its struct
                assert!(rest.iter().all(|p| !p.starts_with("objects/")), "{name}: {rest:?}");
            }
        }
    }
}

#[test]
fn the_walk_stops_inside_heaps_b_trees_and_global_heaps() {
    // (fixture, a structure whose children the room cuts: its path's start, and a child's)
    for (name, under) in [
        ("ims_latest_review2", "global_heaps/0/objects/"),
        ("ims_latest_dense_links", "/links/heap/blocks/"),
        ("ims_latest_dense_links", "/links/index/nodes/"),
        ("ims_latest_review2", "/attributes/heap/blocks/"),
    ] {
        let data = std::fs::read(fixtures().join(format!("ims/{name}.ims"))).unwrap();
        let (all, _) = ims_with_room(&data, None);
        let paths = |ir: &refkv_ir::ir::Ir| (0..ir.len() as u32).map(|i| ir.path(i)).collect::<Vec<_>>();
        let full = paths(&all);
        // the room is spent right after the structure's first child: the walk is in it
        let first = full.iter().position(|p| p.contains(under)).unwrap_or_else(|| panic!("{name}: no {under}"));
        let parent = &full[first][..full[first].find(under).unwrap() + under.len()];
        let total = full.iter().filter(|p| p.starts_with(parent)).count();
        assert!(total > 1, "{name}: {parent} has one child");
        let (ir, _) = ims_with_room(&data, Some(first as u64 + 1));
        check::check(&ir).unwrap();
        assert!(rebuilt(&ir, &data) == data);
        let kept = paths(&ir).iter().filter(|p| p.starts_with(parent)).count();
        assert!(kept >= 1 && kept < total, "{name}: {kept} of {total} under {parent}");
    }
    // the room spent right after the first collection: no later collection is described
    let data = std::fs::read(fixtures().join("ims/ims_latest_global_heaps.ims")).unwrap();
    let (all, _) = ims_with_room(&data, None);
    let full: Vec<String> = (0..all.len() as u32).map(|i| all.path(i)).collect();
    assert!(full.iter().any(|p| p.starts_with("global_heaps/2")), "fewer than three collections");
    let last = full.iter().rposition(|p| p.starts_with("global_heaps/0")).unwrap();
    let (ir, _) = ims_with_room(&data, Some(last as u64 + 1));
    check::check(&ir).unwrap();
    assert!(rebuilt(&ir, &data) == data);
    let paths: Vec<String> = (0..ir.len() as u32).map(|i| ir.path(i)).collect();
    for p in full.iter().filter(|p| p.starts_with("global_heaps/0")) {
        assert!(paths.contains(p), "{p} of collection 0 is not described");
    }
    assert!(!paths.iter().any(|p| p.starts_with("global_heaps/1")), "a later collection is described");
}
