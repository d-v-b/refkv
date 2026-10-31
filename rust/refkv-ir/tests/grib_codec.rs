//! The `refkv.grib` codec (src/grib/codec.rs; spec/conventions.md §12
//! <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#12-grib-messages>): every field
//! of every fixture decodes to eccodes' values (fixtures/generators/grib/write_fixtures.py), and
//! one test per error.

mod common;

use refkv_ir::grib::codec::{Config, decode, grid_shape};
use refkv_ir::grib::sections::{Section, fields, indicator, sections};
use std::path::Path;

fn fixture(name: &str) -> Vec<u8> {
    std::fs::read(common::fixtures().join("grib").join(name)).unwrap()
}

/// A little-endian float64 .npy array: its shape and values.
fn npy(path: &Path) -> (Vec<u64>, Vec<f64>) {
    let b = std::fs::read(path).unwrap();
    assert_eq!(&b[..6], b"\x93NUMPY", "{}", path.display());
    let (len, at) = if b[6] == 1 { (u16::from_le_bytes([b[8], b[9]]) as usize, 10) } else { (u32::from_le_bytes(b[8..12].try_into().unwrap()) as usize, 12) };
    let header = std::str::from_utf8(&b[at..at + len]).unwrap();
    assert!(header.contains("'descr': '<f8'") && header.contains("'fortran_order': False"), "{header}");
    let shape = header.split("'shape': (").nth(1).unwrap().split(')').next().unwrap();
    let shape = shape.split(',').filter(|s| !s.trim().is_empty()).map(|s| s.trim().parse().unwrap()).collect();
    let values = b[at + len..].chunks(8).map(|c| f64::from_le_bytes(c.try_into().unwrap())).collect();
    (shape, values)
}

/// FNV-1a, 64 bits, of the values' little-endian bytes, each NaN as 0x7ff8000000000000.
fn fnv1a64(values: &[f64]) -> String {
    let mut h: u64 = 0xcbf29ce484222325;
    for v in values {
        let bits = if v.is_nan() { 0x7ff8000000000000 } else { v.to_bits() };
        for byte in bits.to_le_bytes() {
            h = (h ^ byte as u64).wrapping_mul(0x100000001b3);
        }
    }
    format!("{h:016x}")
}

/// A field's expected values: eccodes', or for a sample's large field their digest and NaNs.
enum Want {
    Values(Vec<f64>),
    Digest(String, u64),
}

/// The fixtures the codec rejects (the other tests' inputs).
const ERRORS: &[&str] = &["grib_jpeg2000.grib2", "grib_reject_truncated.grib2", "grib_reject_end.grib2"];

#[test]
fn every_field_of_every_fixture_decodes_to_eccodes_values() {
    let dir = common::fixtures().join("grib");
    let expected = dir.join("expected");
    let samples: serde_json::Value = serde_json::from_slice(&std::fs::read(expected.join("samples.json")).unwrap()).unwrap();
    let mut names: Vec<String> = std::fs::read_dir(&dir).unwrap().map(|e| e.unwrap().file_name().into_string().unwrap()).filter(|n| n.ends_with(".grib2")).collect();
    names.sort();
    let mut checked = 0;
    // the profile's rejects (a file the profile refuses, not a message the codec does) aside
    for name in names.iter().filter(|n| !ERRORS.contains(&n.as_str()) && !n.contains("_reject_")) {
        let message = fixture(name);
        let stem = name.trim_end_matches(".grib2");
        // each field's shape, and its values or their digest
        let mut want: Vec<(Vec<u64>, Want)> = Vec::new();
        if let Some(fields) = samples.get(name) {
            for f in fields.as_array().unwrap() {
                let shape = f["shape"].as_array().unwrap().iter().map(|d| d.as_u64().unwrap()).collect();
                want.push((shape, Want::Digest(f["fnv1a64"].as_str().unwrap().to_string(), f["nan"].as_u64().unwrap())));
            }
        } else {
            loop {
                let path = expected.join(format!("{stem}.{}.npy", want.len()));
                if !path.exists() {
                    break;
                }
                let (shape, values) = npy(&path);
                want.push((shape, Want::Values(values)));
            }
        }
        assert!(!want.is_empty(), "{name}: no expected values");
        for (k, (shape, values)) in want.iter().enumerate() {
            let got = decode(&message, &Config { field: k }, shape).unwrap_or_else(|e| panic!("{name} field {k}: {e}"));
            assert_eq!(grid_shape(&message, k).unwrap(), (shape[0], shape[1]), "{name} field {k}");
            match values {
                Want::Values(values) => {
                    assert_eq!(got.len(), values.len(), "{name} field {k}");
                    for (p, (a, b)) in got.iter().zip(values).enumerate() {
                        assert!(a.to_bits() == b.to_bits() || (a.is_nan() && b.is_nan()), "{name} field {k} point {p}: {a} != {b}");
                    }
                }
                Want::Digest(digest, nan) => {
                    assert_eq!(got.iter().filter(|v| v.is_nan()).count() as u64, *nan, "{name} field {k}");
                    assert_eq!(&fnv1a64(&got), digest, "{name} field {k}");
                }
            }
            checked += 1;
        }
        // and no more fields than those
        let past = decode(&message, &Config { field: want.len() }, &want[0].0).unwrap_err();
        assert!(past.contains("was asked for"), "{name}: {past}");
    }
    assert_eq!(checked, 47);
}

/// The section of number `number` (the first) of a single-field fixture.
fn section(message: &[u8], number: u8) -> Section {
    let ind = indicator(message).unwrap();
    sections(message, ind.length).unwrap().into_iter().find(|s| s.number == number).unwrap()
}

fn error(message: &[u8], field: usize, shape: &[u64]) -> String {
    decode(message, &Config { field }, shape).unwrap_err()
}

#[test]
fn rejects_a_truncated_message() {
    assert_eq!(
        error(&fixture("grib_reject_truncated.grib2"), 0, &[12, 16]),
        "refkv.grib: the chunk is 233 bytes, shorter than its message's length, 467: the message is truncated"
    );
}

#[test]
fn rejects_a_message_without_its_end() {
    assert_eq!(error(&fixture("grib_reject_end.grib2"), 0, &[12, 16]), "refkv.grib: the message does not end with 7777");
}

#[test]
fn rejects_a_field_past_the_last() {
    assert_eq!(
        error(&fixture("grib_fields_4to7.grib2"), 3, &[12, 16]),
        "refkv.grib: field 3 was asked for, and the message has 3 fields"
    );
}

#[test]
fn rejects_jpeg_2000_packing() {
    // the message's second field (its first is grib_simple's)
    assert_eq!(error(&fixture("grib_jpeg2000.grib2"), 1, &[12, 16]), "refkv.grib: JPEG 2000 packing (5.40) is not decoded");
}

#[test]
fn rejects_a_packing_it_does_not_decode() {
    // grib_simple as if its packing were 5.50 (spherical harmonics)
    let mut m = fixture("grib_simple.grib2");
    let s5 = section(&m, 5).offset as usize;
    m[s5 + 9..s5 + 11].copy_from_slice(&50u16.to_be_bytes());
    assert_eq!(error(&m, 0, &[12, 16]), "refkv.grib: data representation template 5.50 is not decoded");
}

#[test]
fn rejects_a_spatial_differencing_order_other_than_1_or_2() {
    let mut m = fixture("grib_complex_sd2.grib2");
    let s5 = section(&m, 5).offset as usize;
    m[s5 + 47] = 3;
    assert_eq!(error(&m, 0, &[12, 16]), "refkv.grib: spatial differencing of order 3 (template 5.3, octet 48) is not decoded");
}

#[test]
fn rejects_a_bitmap_that_does_not_mark_the_number_of_values() {
    let mut m = fixture("grib_bitmap.grib2");
    let s5 = section(&m, 5).offset as usize;
    m[s5 + 5..s5 + 9].copy_from_slice(&181u32.to_be_bytes());
    assert_eq!(error(&m, 0, &[12, 16]), "refkv.grib: the bitmap marks 180 points, and section 5 gives 181 values");
}

#[test]
fn rejects_a_predefined_bitmap() {
    let mut m = fixture("grib_bitmap.grib2");
    let s6 = section(&m, 6).offset as usize;
    m[s6 + 5] = 7;
    assert_eq!(error(&m, 0, &[12, 16]), "refkv.grib: a predefined bitmap (indicator 7) is not decoded");
}

#[test]
fn rejects_a_previous_bitmap_in_the_first_field() {
    let mut m = fixture("grib_bitmap.grib2");
    let s6 = section(&m, 6).offset as usize;
    m[s6 + 5] = 254;
    assert_eq!(error(&m, 0, &[12, 16]), "refkv.grib: bitmap indicator 254, and no earlier field of the message has a bitmap");
}

#[test]
fn rejects_a_number_of_values_other_than_the_grids_points() {
    let mut m = fixture("grib_simple.grib2");
    let s5 = section(&m, 5).offset as usize;
    m[s5 + 5..s5 + 9].copy_from_slice(&191u32.to_be_bytes());
    assert_eq!(error(&m, 0, &[12, 16]), "refkv.grib: section 5 gives 191 values for the grid's 192 points");
}

#[test]
fn rejects_a_chunk_shape_that_is_not_the_grids() {
    assert_eq!(
        error(&fixture("grib_simple.grib2"), 0, &[1, 12, 15]),
        "refkv.grib: the chunk shape [1, 12, 15] does not hold the grid's 12 × 16 points"
    );
}

#[test]
fn rejects_a_grid_template_it_does_not_decode() {
    // grib_simple as if its grid were 3.50 (spherical harmonic coefficients)
    let mut m = fixture("grib_simple.grib2");
    let s3 = section(&m, 3).offset as usize;
    m[s3 + 12..s3 + 14].copy_from_slice(&50u16.to_be_bytes());
    assert_eq!(error(&m, 0, &[12, 16]), "refkv.grib: grid template 3.50 is not decoded");
}

#[test]
fn rejects_a_ccsds_stream_that_ends_early() {
    let m = fixture("grib_ccsds.grib2");
    let s7 = section(&m, 7);
    // section 7 cut to half its data, the message's lengths shortened to match
    let cut = (s7.length as usize - 5) / 2;
    let mut short = m[..s7.offset as usize].to_vec();
    short.extend_from_slice(&(s7.length as u32 - cut as u32).to_be_bytes());
    short.extend_from_slice(&m[s7.offset as usize + 4..(s7.offset + s7.length) as usize - cut]);
    short.extend_from_slice(b"7777");
    let len = short.len() as u64;
    short[8..16].copy_from_slice(&len.to_be_bytes());
    assert_eq!(fields(&sections(&short, len).unwrap()).unwrap().len(), 1);
    assert_eq!(error(&short, 0, &[12, 16]), "refkv.grib: the CCSDS stream ends before its samples do");
}

#[test]
fn rejects_an_unknown_configuration_member() {
    assert_eq!(
        Config::from_json(&serde_json::json!({"field": 0, "north_up": true})).unwrap_err(),
        "refkv.grib: the configuration has an unknown member \"north_up\""
    );
}

#[test]
fn rejects_a_field_that_is_not_a_non_negative_integer() {
    assert_eq!(
        Config::from_json(&serde_json::json!({"field": -1})).unwrap_err(),
        "refkv.grib: the configuration's field is -1, not a non-negative integer"
    );
}

#[test]
fn reads_a_configuration_without_a_field_as_field_0() {
    assert_eq!(Config::from_json(&serde_json::json!({})).unwrap(), Config { field: 0 });
    assert_eq!(Config::from_json(&serde_json::json!({"field": 2})).unwrap(), Config { field: 2 });
}
