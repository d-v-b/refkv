//! The QPTIFF profile (spec/virtualize/qptiff.md), read by the TIFF parser: QPTIFF found as
//! QPTIFF, the facts its descriptions give, and one test per rejected fixture.

mod common;

use common::{fixtures, reject};
use refkv_ir::parse_bytes;
use refkv_ir::tiff::Tiff;

// one test per rejected fixture, each the message its check gives
reject!(qptiff_edge_reject_channel_count, Tiff, "qptiff/edge_reject_qptiff_channel_count.qptiff", "a QPTIFF with 3 channels has 2 ReducedResolution images, not a multiple of 3");
reject!(qptiff_edge_reject_rgb_channels, Tiff, "qptiff/edge_reject_qptiff_rgb_channels.qptiff", "QPTIFF channels have 3 samples per pixel");
reject!(qptiff_edge_reject_not_baseline, Tiff, "qptiff/edge_reject_qptiff_not_baseline.qptiff", "IFD 0 of a QPTIFF is not a FullResolution image");
reject!(qptiff_edge_reject_no_description, Tiff, "qptiff/edge_reject_qptiff_no_description.qptiff", "IFD 0 of a QPTIFF has no PerkinElmer-QPI image description");
reject!(qptiff_edge_reject_level_grows, Tiff, "qptiff/edge_reject_qptiff_level_grows.qptiff", "QPTIFF level 1 is not smaller than level 0");
reject!(qptiff_edge_reject_late_full, Tiff, "qptiff/edge_reject_qptiff_late_full.qptiff", "a QPTIFF FullResolution image follows the baseline images");
reject!(qptiff_edge_reject_level_mismatch, Tiff, "qptiff/edge_reject_qptiff_level_mismatch.qptiff", "the images of QPTIFF level 1 differ in size, tiling or format");
reject!(qptiff_edge_reject_predictor, Tiff, "qptiff/edge_reject_qptiff_predictor.qptiff", "unsupported predictor 2");
reject!(qptiff_edge_reject_strip_count, Tiff, "qptiff/edge_reject_qptiff_strip_count.qptiff", "IFD at 8 has 3 tiles or strips, expected 5");

/// The TIFF parser reads a QPTIFF as QPTIFF (spec/virtualize/qptiff.md §7
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/qptiff.md#7-qptiff-profile>): each
/// accepted QPTIFF fixture's facts name the profile, its channels and levels; a TIFF's and an
/// NDPI's do not.
#[test]
fn the_tiff_parser_finds_qptiff() {
    for (file, channels, levels) in [
        ("qptiff/qptiff_fl_uint8_lzw.qptiff", 3, 3),
        ("qptiff/qptiff_fl_uint16_lzw_bigtiff.qptiff", 2, 2),
        ("qptiff/qptiff_bf_rgb_jpeg.qptiff", 1, 2), // its JPEG level in strips, short at the end, is no level
        ("qptiff/qptiff_bf_rgb_uncompressed.qptiff", 1, 2),
        ("qptiff/qptiff_component_float32_strips.qptiff", 2, 1),
        ("qptiff/qptiff_fl_lzw_narrow_eoi.qptiff", 1, 1),
    ] {
        let data = std::fs::read(fixtures().join(file)).unwrap();
        let (_, facts) = parse_bytes::<Tiff>(&data).unwrap();
        assert_eq!(facts["profile"], "qptiff", "{file}");
        assert_eq!(facts["channels"].as_array().unwrap().len(), channels, "{file}");
        assert_eq!(facts["levels"].as_array().unwrap().len(), levels, "{file}");
    }
    for file in ["tiff/jpeg_gray.tif", "ndpi/ndpi_levels.ndpi"] {
        let data = std::fs::read(fixtures().join(file)).unwrap();
        let (_, facts) = parse_bytes::<Tiff>(&data).unwrap();
        assert_ne!(facts["profile"], "qptiff", "{file}");
    }
}

/// The channels' names and colors, the image name and the levels' tiling, as the layout reads
/// them (spec/virtualize/qptiff.md §3
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/qptiff.md#3-images-and-channels>): an
/// entity reference decoded, a Name nested in the ScanProfile not taken, the smallest level in
/// strips.
#[test]
fn qptiff_facts_read_the_descriptions() {
    let data = std::fs::read(fixtures().join("qptiff/qptiff_fl_uint8_lzw.qptiff")).unwrap();
    let (_, facts) = parse_bytes::<Tiff>(&data).unwrap();
    let names: Vec<&str> = facts["channels"].as_array().unwrap().iter().map(|c| c["name"].as_str().unwrap()).collect();
    assert_eq!(names, ["DAPI", "Cy3 & TRITC", "Opal 520"]);
    assert_eq!(facts["channels"][1]["color"], "255, 60, 157");
    assert_eq!(facts["name"], "Fixture-Slide");
    assert_eq!(facts["axes"], serde_json::json!(["c", "y", "x"]));
    let strips: Vec<bool> = facts["levels"].as_array().unwrap().iter().map(|l| l["strips"].as_bool().unwrap()).collect();
    assert_eq!(strips, [false, false, true]);
    assert_eq!(facts["levels"][2]["th"], 16);
    // the last strip (8 rows of 16) is padded by a literal tail spliced into its LZW stream
    let pads = facts["levels"][2]["pad"]["lzw"].as_array().unwrap();
    assert_eq!(pads.len(), 3);
    assert!(pads.iter().all(|p| p["k"] == 1 && p["prefix"].as_u64().unwrap() > 0 && !p["tail"].as_array().unwrap().is_empty()));
    assert_eq!(facts["translation"], serde_json::json!([0, 484400.0, 88600.0]));
}
