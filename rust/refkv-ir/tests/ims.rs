//! The IMS parser (src/ims.rs; spec/zarrify/ims.md): one test per rejected fixture, and the
//! walk's room.

mod common;

use common::{fixtures, rebuilt};
use refkv_ir::check;
use refkv_ir::ims::Ims;

// one test per rejected fixture, each the message its check gives
// one test per case of conformance/zarrify/rejects/ims.json (build.rs), each the message its check gives
macro_rules! reject {
    ($name:ident, $input:literal, $msg:literal) => {
        common::reject!($name, Ims, concat!("ims/", $input), $msg);
    };
}
include!(concat!(env!("OUT_DIR"), "/rejects_ims.rs"));

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
            refkv_ir::nd2::Step::Beside => panic!("the file names siblings"),
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

/// Every image dataset of an accepted fixture's facts names its description by the description's
/// key, the struct of the dataset's object, and the description's rows are the dataset's chunks.
/// Each row of the stored arrays as the bytes of the file it names: its coordinates and its
/// (offset, length) parts, and whether it names them as ranges of the source.
fn row_extents(ir: &refkv_ir::ir::Ir) -> Vec<(Vec<u64>, Vec<(u64, u64)>, bool)> {
    let m = &ir.stored.map;
    (0..m.len())
        .map(|r| match m.ranges_of(r) {
            Some(parts) => {
                assert!(parts.clone().all(|p| m.range_object[p].is_none()));
                (m.coords_of(r).to_vec(), parts.map(|p| (m.range_offset[p], m.range_length[p])).collect(), true)
            }
            None => {
                let e = m.element[r].unwrap() as usize;
                let (o, n) = m.range[r].unwrap_or((0, ir.len[e]));
                (m.coords_of(r).to_vec(), vec![(ir.start[e] + o, n)], false)
            }
        })
        .collect()
}

#[test]
fn past_the_walks_room_a_row_names_the_same_bytes_as_a_range_of_the_source() {
    let mut as_ranges = 0;
    for name in ["ims_latest_many_channels", "ims_latest_dense_links", "ims_latest_indexes", "ims_latest_review2", "ims_earliest_review2"] {
        let data = std::fs::read(fixtures().join(format!("ims/{name}.ims"))).unwrap();
        let (all, _) = ims_with_room(&data, None);
        let mut whole: Vec<_> = row_extents(&all).into_iter().map(|(c, parts, _)| (c, parts)).collect();
        whole.sort();
        for room in [0, all.len() as u64 / 4, all.len() as u64 / 2] {
            let (ir, _) = ims_with_room(&data, Some(room));
            refkv_ir::array::check(&ir).unwrap();
            let rows = row_extents(&ir);
            as_ranges += rows.iter().filter(|r| r.2).count();
            let mut short: Vec<_> = rows.into_iter().map(|(c, parts, _)| (c, parts)).collect();
            short.sort();
            assert!(short == whole, "{name} room {room}: {} rows, {} with room for all", short.len(), whole.len());
        }
    }
    assert!(as_ranges > 100, "{as_ranges} rows of ranges");
}

#[test]
fn every_accepted_fixtures_images_name_their_descriptions() {
    let mut images = 0;
    for e in std::fs::read_dir(fixtures().join("ims")).unwrap() {
        let p = e.unwrap().path();
        let name = p.file_name().unwrap().to_str().unwrap().to_string();
        if name.contains("reject") || !name.ends_with(".ims") {
            continue;
        }
        let (ir, facts) = ims_with_room(&std::fs::read(&p).unwrap(), None);
        let s = &ir.stored;
        let mut keys = std::collections::HashSet::new();
        for im in facts["images"].as_array().unwrap() {
            let key = im[5].as_u64().unwrap_or_else(|| panic!("{name}: an image with no key")) as u32;
            assert!(keys.insert(key), "{name}: two images name one description");
            let described: Vec<usize> = (0..s.arrays.len()).filter(|&k| s.arrays[k].element == key).collect();
            let [k] = described[..] else { panic!("{name}: {} descriptions of an image", described.len()) };
            assert!(s.arrays[k].grid.is_some(), "{name}");
            let rows = (0..s.map.len()).filter(|&r| s.map.array[r] == k as u32).count();
            let chunks = im[3].as_u64().map_or(0, |el| ir.members(el as u32).len());
            assert_eq!(rows, chunks, "{name}");
            images += 1;
        }
    }
    assert!(images >= 10, "{images} images");
}
