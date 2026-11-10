//! The TIFF parser (src/tiff.rs; spec/zarrify/tiff.md): one test per rejected fixture; NDPI
//! found as NDPI; and the round-3 probes built here (IFD loops, huge counts, too many IFDs).

mod common;

use common::{fixtures, rebuilt};
use refkv_ir::check;
use refkv_ir::parse_bytes;
use refkv_ir::tiff::Tiff;

// one test per rejected fixture, each the message its check gives
// one test per case of conformance/zarrify/rejects/tiff.json (build.rs), each the message its check gives
macro_rules! reject {
    ($name:ident, $input:literal, $msg:literal) => {
        common::reject!($name, Tiff, concat!("tiff/", $input), $msg);
    };
}
include!(concat!(env!("OUT_DIR"), "/rejects_tiff.rs"));

/// The TIFF parser reads an NDPI as NDPI (spec/zarrify/ndpi.md §7
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#7-ndpi-profile>): each
/// accepted NDPI fixture's facts name the profile, and a TIFF's do not.
#[test]
fn the_tiff_parser_finds_ndpi() {
    for (file, ndpi) in [("ndpi/ndpi_levels.ndpi", true), ("ndpi/ndpi_pointers.ndpi", true), ("tiff/jpeg_gray.tif", false)] {
        let data = std::fs::read(fixtures().join(file)).unwrap();
        let (_, facts) = parse_bytes::<Tiff>(&data).unwrap();
        assert_eq!(facts["profile"] == "ndpi", ndpi, "{file}");
    }
}

/// A Micro-Manager file's summary is a JSON object whose numbers are finite (spec/zarrify/tiff.md
/// §7.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#75-micro-manager-stacks>):
/// its facts hold it, its numbers as written.
#[test]
fn a_micromanager_summary_is_read() {
    let data = std::fs::read(fixtures().join("tiff/mm_single_MMStack_Pos0.ome.tif")).unwrap();
    let (_, facts) = parse_bytes::<Tiff>(&data).unwrap();
    assert_eq!(facts["micromanager"]["PixelSize_um"], serde_json::json!(0.5));
    assert_eq!(facts["micromanager"]["Channels"], serde_json::json!(2));
}

/// A summary with a number that is not finite in binary64 (1e400) is no Micro-Manager summary:
/// the file is a TIFF like another, and its stack of pages without OME-XML is rejected.
#[test]
fn a_summary_with_a_number_not_finite_is_none() {
    let mut data = std::fs::read(fixtures().join("tiff/mm_single_MMStack_Pos0.ome.tif")).unwrap();
    let (old, new) = (br#""1.4.22""#, b"1e400   ");
    let at = data.windows(old.len()).position(|w| w == old).unwrap();
    data[at..at + old.len()].copy_from_slice(new);
    let e = parse_bytes::<Tiff>(&data).err().unwrap();
    assert!(e.contains("a stack of pages without OME-XML"), "{e}");
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

/// What the facts' levels say of their planes against the stored arrays of `ir`: each plane that
/// has tiles or strips names, in `declared_by`, the one description its IFD's element keys, whose
/// rows' elements are the plane's tiles or strips. Returns the planes checked whose description
/// has a grid, those whose description has none, and the objects (or the root) that hold them.
fn planes_name_their_descriptions(name: &str, ir: &refkv_ir::ir::Ir, facts: &serde_json::Value) -> (usize, usize, std::collections::BTreeSet<u32>) {
    let s = &ir.stored;
    let (mut planes, mut unmodeled, mut holders) = (0, 0, std::collections::BTreeSet::new());
    for (l, lv) in facts["levels"].as_array().unwrap().iter().enumerate() {
        let (units, keys) = (lv["planes"].as_array().unwrap(), lv["declared_by"].as_array().unwrap());
        assert_eq!(units.len(), keys.len(), "{name} level {l}");
        for (p, (unit, key)) in units.iter().zip(keys).enumerate() {
            let at = format!("{name} level {l} plane {p}");
            // the planes after the first of an ImageJ stack in one IFD have strips no IFD declares
            let undeclared = p > 0 && key.is_null() && facts["imagej"]["one_ifd"].as_bool() == Some(true);
            assert!(undeclared || unit.is_null() == key.is_null(), "{at}");
            let (Some(unit), Some(key)) = (unit.as_u64(), key.as_u64()) else { continue };
            let described: Vec<usize> = (0..s.arrays.len()).filter(|&k| s.arrays[k].element == key as u32).collect();
            let [k] = described[..] else { panic!("{at}: {} descriptions", described.len()) };
            let rows: Vec<usize> = (0..s.map.len()).filter(|&r| s.map.array[r] == k as u32).collect();
            assert_eq!(rows.is_empty(), s.arrays[k].grid.is_none(), "{at}");
            assert!(rows.iter().all(|&r| ir.parent[s.map.element[r].unwrap() as usize] == unit as u32), "{at}");
            // the IFD is a child of the root, or of the file's object
            assert!(ir.path(key as u32).rsplit('/').nth(1).is_some_and(|n| n == "ifds" || n == "subifds"), "{at}: {}", ir.path(key as u32));
            holders.insert(ir.parent[key as usize]);
            if s.arrays[k].grid.is_some() {
                planes += 1;
            } else {
                unmodeled += 1;
            }
        }
    }
    (planes, unmodeled, holders)
}

/// Each plane of each level of an accepted TIFF or QPTIFF fixture names its description by the
/// description's key, the element of the plane's IFD (spec/stored-array.md §7
/// <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>). One presented
/// plane has a description with no grid: its strip of no bytes at an offset is a chunk the
/// profile leaves out, and one the IR does not model.
#[test]
fn every_accepted_fixtures_planes_name_their_descriptions() {
    let (mut planes, mut unmodeled, mut skipped) = (0, Vec::new(), Vec::new());
    for dir in ["tiff", "qptiff"] {
        for e in std::fs::read_dir(fixtures().join(dir)).unwrap() {
            let p = e.unwrap().path();
            let name = p.file_name().unwrap().to_str().unwrap().to_string();
            if name.contains("reject") || !p.is_file() {
                continue;
            }
            let (ir, facts) = match parse_bytes::<Tiff>(&std::fs::read(&p).unwrap()) {
                Ok(parsed) => parsed,
                // a file of a set, which a store run reads (the next test)
                Err(e) if e.contains("names siblings") => continue,
                Err(_) => {
                    skipped.push(name);
                    continue;
                }
            };
            let (modeled, without, _) = planes_name_their_descriptions(&name, &ir, &facts);
            planes += modeled;
            unmodeled.extend(std::iter::repeat_n(name, without));
        }
    }
    skipped.sort();
    // the file the profile rejects whose name does not say so
    assert_eq!(skipped, ["unsupported_predictor_2_float.tif"]);
    assert_eq!(unmodeled, ["edge_strips_absent.tif"]);
    assert!(planes >= 200, "{planes} planes");
}

/// In a store run of an OME-TIFF set, each plane names its description as the store IR numbers
/// it: the element of its IFD, in the file that holds the plane.
#[test]
fn a_sets_planes_name_their_descriptions_in_their_files() {
    use refkv_ir::store::{StoreFormat, StoreRun};
    let dir = fixtures().join("tiff");
    let key = "ome_set_a.ome.tif".to_string();
    let mut files = std::collections::BTreeMap::from([(key.clone(), std::fs::read(dir.join(&key)).unwrap())]);
    let mut run = StoreRun::new(StoreFormat::Tiff, vec![(key.clone(), files[&key].len() as u64)], 4).unwrap();
    while let Some(reqs) = run.poll().unwrap() {
        if reqs.is_empty() {
            for k in run.probes() {
                let data = std::fs::read(dir.join(&k)).ok();
                run.found(data.as_ref().map(|d| d.len() as u64)).unwrap();
                files.extend(data.map(|d| (k, d)));
            }
            continue;
        }
        for (k, q) in reqs.iter().enumerate() {
            let data = &files[&q.key];
            run.complete(k, q.spans.iter().map(|&(a, b)| data[a as usize..b as usize].to_vec()).collect()).unwrap();
        }
    }
    let (ir, facts, _) = run.finish().unwrap();
    let (planes, unmodeled, holders) = planes_name_their_descriptions(&key, &ir, &facts);
    assert!(planes >= 2 && unmodeled == 0, "{planes} planes, {unmodeled} with no grid");
    assert!(holders.len() >= 2 && holders.iter().all(|&o| ir.kind[o as usize] == refkv_ir::ir::OBJECT), "planes in {holders:?}");
}

// ---- which IFD is tiled, and the tables that list its tiles (spec/zarrify/tiff.md §2
// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md#2-the-source>)

type Entry = (u16, u16, u32, u32);
const TILE_SIZE: [Entry; 2] = [(322, 3, 1, 16), (323, 3, 1, 16)];
const ROWS_PER_STRIP: Entry = (278, 3, 1, 8);
const TILE_TABLES: [Entry; 2] = [(324, 4, 4, 1032), (325, 4, 4, 1048)];
const STRIP_TABLES: [Entry; 2] = [(273, 4, 4, 1032), (279, 4, 4, 1048)];

/// `image()`'s IFD with `with` in place of its tile size and tables. Its four units of 256 bytes
/// are 16 x 16 tiles and strips of 8 rows alike, and its one pair of tables lists them under
/// either pair of tags.
fn units(with: &[Entry]) -> Vec<u8> {
    let (e, blobs) = image();
    let e = e.into_iter().filter(|x| !(322..=325).contains(&x.0)).chain(with.iter().copied()).collect();
    tiff(&[e], &blobs, &[None])
}

/// An IFD with TileWidth and TileLength is tiled, and its tiles are those of TileOffsets and
/// TileByteCounts, or of StripOffsets and StripByteCounts where it has neither; an IFD without
/// both tags of the size is in strips, whatever tile tables it has. The level's chunks, their
/// element and its members' shape are the IFD's tiles or strips, and its description, which takes
/// an IFD as tiled by the same tags, has the level's grid over the same element.
#[test]
fn an_ifd_is_tiled_by_its_tile_size_and_read_by_its_tile_tables() {
    use refkv_ir::array::{AxisUnits, Edge};
    // (the IFD's tile size and tables, whether it is tiled, the element of its chunks)
    let cases = [
        ("its own tables", [&TILE_SIZE[..], &TILE_TABLES].concat(), true, "tiles"),
        ("tiles listed in the strips' tables", [&TILE_SIZE[..], &STRIP_TABLES].concat(), true, "strips"),
        ("both pairs of tables: its own", [&TILE_SIZE[..], &TILE_TABLES, &STRIP_TABLES].concat(), true, "tiles"),
        ("strips", [&STRIP_TABLES[..], &[ROWS_PER_STRIP]].concat(), false, "strips"),
        ("TileWidth without TileLength", [&TILE_SIZE[..1], &TILE_TABLES, &STRIP_TABLES, &[ROWS_PER_STRIP]].concat(), false, "strips"),
        ("TileLength without TileWidth", [&TILE_SIZE[1..], &TILE_TABLES, &STRIP_TABLES, &[ROWS_PER_STRIP]].concat(), false, "strips"),
        ("tile tables without a tile size", [&TILE_TABLES[..], &STRIP_TABLES, &[ROWS_PER_STRIP]].concat(), false, "strips"),
    ];
    for (name, with, tiled, element) in cases {
        let data = units(&with);
        let (ir, facts) = parse_bytes::<Tiff>(&data).unwrap_or_else(|e| panic!("{name}: {e}"));
        check::check(&ir).unwrap();
        assert_eq!(rebuilt(&ir, &data), data, "{name}");
        let lv = &facts["levels"][0];
        let (rows, columns) = if tiled { (16, 16) } else { (8, 32) };
        assert_eq!((lv["strips"].as_bool(), lv["th"].as_u64(), lv["tw"].as_u64()), (Some(!tiled), Some(rows), Some(columns)), "{name}");
        // the plane's chunks: the four members of the element of its tables, each of a unit's shape
        let unit = lv["planes"][0].as_u64().unwrap() as u32;
        assert_eq!(ir.path(unit), format!("ifds/0/{element}"), "{name}");
        let members: Vec<u32> = (0..ir.parent.len() as u32).filter(|&i| ir.parent[i as usize] == unit).collect();
        assert_eq!(members.len(), 4, "{name}");
        for &m in &members {
            let form: serde_json::Value = serde_json::from_str(ir.form(m).unwrap()).unwrap();
            assert_eq!(form["geometry"]["shape"], serde_json::json!([rows, columns]), "{name}");
        }
        // its description: the same grid, over the same element
        assert_eq!(planes_name_their_descriptions(name, &ir, &facts).0, 1, "{name}");
        let key = lv["declared_by"][0].as_u64().unwrap() as u32;
        let grid = ir.stored.arrays.iter().find(|a| a.element == key).unwrap().grid.as_ref().unwrap();
        assert_eq!(grid.axes, [AxisUnits::Each(rows), AxisUnits::Each(columns)], "{name}");
        assert_eq!(grid.edge, if tiled { Edge::Full } else { Edge::Short }, "{name}");
    }
}

/// A tiled IFD with none of the four tables has no tiles.
#[test]
fn a_tiled_ifd_without_tables_is_rejected() {
    rejects(&units(&TILE_SIZE), "the tiled image at 1064 has neither tile tables nor strip tables");
}

/// A tiled IFD with one table of a pair and not the other is rejected: one of its own tables is
/// not paired with a strips' table, as libtiff and tifffile would pair them, nor passed over for
/// the strips' two, as its description passes it over.
#[test]
fn a_tiled_ifd_with_half_of_its_tile_tables_is_rejected() {
    let cases = [
        ([&TILE_SIZE[..], &TILE_TABLES[..1], &STRIP_TABLES].concat(), 325),
        ([&TILE_SIZE[..], &TILE_TABLES[1..], &STRIP_TABLES].concat(), 324),
        ([&TILE_SIZE[..], &STRIP_TABLES[..1]].concat(), 279),
        ([&TILE_SIZE[..], &STRIP_TABLES[1..]].concat(), 273),
    ];
    for (with, missing) in cases {
        rejects(&units(&with), &format!("IFD at 1064 has no tag {missing}"));
    }
}

/// An IFD without both tags of a tile's size is in strips, and its strips are those of
/// StripOffsets and StripByteCounts alone: with tile tables only, it has none.
#[test]
fn tile_tables_without_a_tile_size_are_not_strips() {
    for size in [&TILE_SIZE[..0], &TILE_SIZE[..1], &TILE_SIZE[1..]] {
        rejects(&units(&[size, &TILE_TABLES, &[ROWS_PER_STRIP]].concat()), "the image at 1064 has neither tiles nor strips");
    }
}
