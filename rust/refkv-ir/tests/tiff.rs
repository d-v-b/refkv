//! The TIFF parser (src/tiff.rs; spec/virtualize/tiff.md): one test per rejected fixture; NDPI
//! found as NDPI; and the round-3 probes built here (IFD loops, huge counts, too many IFDs).

mod common;

use common::{fixtures, rebuilt, reject};
use refkv_ir::check;
use refkv_ir::parse_bytes;
use refkv_ir::tiff::Tiff;

// one test per rejected fixture, each the message its check gives
reject!(tiff_edge_reject_big_offset, Tiff, "tiff/edge_reject_big_offset.tif", "a value of tag 324 is more than 2^53 - 1");
reject!(tiff_edge_reject_bigtiff_reserved, Tiff, "tiff/edge_reject_bigtiff_reserved.tif", "invalid BigTIFF header");
reject!(tiff_edge_reject_candidate_planar3, Tiff, "tiff/edge_reject_candidate_planar3.tif", "PlanarConfiguration 3");
reject!(tiff_edge_reject_cycle, Tiff, "tiff/edge_reject_cycle.tif", "IFD offset 4136 read twice");
reject!(tiff_edge_reject_dimension_order, Tiff, "tiff/edge_reject_dimension_order.tif", "DimensionOrder 'XYZZT'");
reject!(tiff_edge_reject_fill_order, Tiff, "tiff/edge_reject_fill_order.tif", "the image at 4136 has FillOrder 2");
reject!(tiff_edge_reject_first_z, Tiff, "tiff/edge_reject_first_z.tif", "TiffData starts outside the planes");
reject!(tiff_edge_reject_jpeg_16bit, Tiff, "tiff/edge_reject_jpeg_16bit.tif", "unsupported JPEG: 16-bit, 3 samples, planar 1, p");
reject!(tiff_edge_reject_jpeg_no_photometric, Tiff, "tiff/edge_reject_jpeg_no_photometric.tif", "unsupported JPEG: 8-bit, 3 samples, planar 1, ph");
reject!(tiff_edge_reject_jpeg_planar, Tiff, "tiff/edge_reject_jpeg_planar.tif", "unsupported JPEG: 8-bit, 3 samples, planar 2, ph");
reject!(tiff_edge_reject_jpeg_short_tile, Tiff, "tiff/edge_reject_jpeg_short_tile.tif", "JPEG tile 0 of the IFD at 16 is too short");
reject!(tiff_edge_reject_jpeg_tables, Tiff, "tiff/edge_reject_jpeg_tables.tif", "the IFD at 834 has malformed JPEGTables");
reject!(tiff_edge_reject_multifile, Tiff, "tiff/edge_reject_multifile.tif", "multi-file OME-TIFF is not supported");
reject!(tiff_edge_reject_multifile_uuid, Tiff, "tiff/edge_reject_multifile_uuid.tif", "multi-file OME-TIFF is not supported");
reject!(tiff_edge_reject_nbsp_size, Tiff, "tiff/edge_reject_nbsp_size.tif", "SizeZ=' 2' is not an integer");
reject!(tiff_edge_reject_no_ifds, Tiff, "tiff/edge_reject_no_ifds.tif", "no images");
reject!(tiff_edge_reject_no_width, Tiff, "tiff/edge_reject_no_width.tif", "IFD at 4136 has no tag 256");
reject!(tiff_edge_reject_planar3, Tiff, "tiff/edge_reject_planar3.tif", "PlanarConfiguration 3");
reject!(tiff_edge_reject_plane_limit, Tiff, "tiff/edge_reject_plane_limit.tif", "100001 planes is more than 100000");
reject!(tiff_edge_reject_plane_size, Tiff, "tiff/edge_reject_plane_size.tif", "planes of one pyramid level differ in size, tili");
reject!(tiff_edge_reject_sample_format, Tiff, "tiff/edge_reject_sample_format.tif", "SampleFormat values are missing or differ");
reject!(tiff_edge_reject_scalar_count0, Tiff, "tiff/edge_reject_scalar_count0.tif", "tag 259 has no value");
reject!(tiff_edge_reject_shared_subifd, Tiff, "tiff/edge_reject_shared_subifd.tif", "IFD offset 1032 read twice");
reject!(tiff_edge_reject_signed_width, Tiff, "tiff/edge_reject_signed_width.tif", "tag 256 has field type 8");
reject!(tiff_edge_reject_size_sign, Tiff, "tiff/edge_reject_size_sign.tif", "SizeZ='+2' is not an integer");
reject!(tiff_edge_reject_size_zero, Tiff, "tiff/edge_reject_size_zero.tif", "SizeZ='0' is less than 1");
reject!(tiff_edge_reject_subifd_count, Tiff, "tiff/edge_reject_subifd_count.tif", "the IFD at 9832 has fewer SubIFDs than IFD 0");
reject!(tiff_edge_reject_tiffdata_steps, Tiff, "tiff/edge_reject_tiffdata_steps.tif", "the TiffData elements cover more than 1008 plane");
reject!(tiff_edge_reject_tile_count, Tiff, "tiff/edge_reject_tile_count.tif", "IFD at 32 has 3 tiles, expected 4");
reject!(tiff_edge_reject_tile_outside, Tiff, "tiff/edge_reject_tile_outside.tif", "tile 0 of the IFD at 40 is outside the file");
reject!(tiff_edge_reject_unmapped, Tiff, "tiff/edge_reject_unmapped.tif", "OME-XML planes do not match the TIFF's images");
reject!(tiff_edge_reject_unused_ifd_type, Tiff, "tiff/edge_reject_unused_ifd_type.tif", "tag 259 has field type 99");
reject!(tiff_edge_reject_unused_value_outside, Tiff, "tiff/edge_reject_unused_value_outside.tif", "the value of tag 324 is outside the file");
reject!(tiff_edge_reject_uuid_self_closing, Tiff, "tiff/edge_reject_uuid_self_closing.tif", "multi-file OME-TIFF is not supported");
reject!(tiff_edge_reject_ycbcr_default, Tiff, "tiff/edge_reject_ycbcr_default.tif", "the image at 12334 is YCbCr with subsampling [2,");
reject!(tiff_edge_reject_ycbcr_subsampled, Tiff, "tiff/edge_reject_ycbcr_subsampled.tif", "the image at 12334 is YCbCr with subsampling [2,");
reject!(tiff_edge_reject_zero_tile, Tiff, "tiff/edge_reject_zero_tile.tif", "the image at 8 has an empty tile size");
reject!(tiff_unsupported_lzw, Tiff, "tiff/unsupported_lzw.tif", "unsupported compression 5");
reject!(tiff_unsupported_predictor, Tiff, "tiff/unsupported_predictor.tif", "unsupported predictor 2");
reject!(tiff_unsupported_strips, Tiff, "tiff/unsupported_strips.tif", "only tiled TIFFs are supported; the image at 8 i");

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
