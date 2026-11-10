//! The DICOM parser (src/dicom/; spec/zarrify/dicom.md): one test per rejected fixture, a
//! long fragment chain built here, and a range asked for and not fed.

mod common;

use common::{fixtures, rebuilt};
use refkv_ir::check;
use refkv_ir::dicom::Dicom;

// one test per rejected fixture, each the message its check gives
// one test per case of conformance/zarrify/rejects/dicom.json (build.rs), each the message its check gives
macro_rules! reject {
    ($name:ident, $input:literal, $msg:literal) => {
        common::reject!($name, Dicom, concat!("dicom/", $input), $msg);
    };
}
include!(concat!(env!("OUT_DIR"), "/rejects_dicom.rs"));

/// A DICOM file in explicit VR little endian: `body` after the File Meta Information.
fn dicom(syntax: &str, body: &[u8]) -> Vec<u8> {
    let mut uid = syntax.as_bytes().to_vec();
    if uid.len() % 2 == 1 {
        uid.push(0);
    }
    let mut out = vec![0u8; 128];
    out.extend_from_slice(b"DICM");
    out.extend(short(2, 0x10, b"UI", &uid));
    out.extend_from_slice(body);
    out
}

/// One element of a short VR in explicit VR little endian.
fn short(g: u16, e: u16, vr: &[u8; 2], v: &[u8]) -> Vec<u8> {
    let mut b = Vec::new();
    for x in [g, e] {
        b.extend_from_slice(&x.to_le_bytes());
    }
    b.extend_from_slice(vr);
    b.extend_from_slice(&(v.len() as u16).to_le_bytes());
    b.extend_from_slice(v);
    b
}

#[test]
fn a_long_fragment_chain_is_walked_to_its_end() {
    // 600 frames of 70,000 bytes, without an offset table: each fragment's item header is
    // past the window that read the one before, so a round each
    let n = 600u32;
    let mut body = Vec::new();
    for (e, v) in [(2u16, 1u16), (0x10, 1), (0x11, 1), (0x100, 8), (0x101, 8), (0x103, 0)] {
        body.extend(short(0x28, e, b"US", &v.to_le_bytes()));
    }
    body.extend(short(0x28, 4, b"CS", b"MONOCHROME2 "));
    body.extend(short(0x28, 8, b"IS", n.to_string().as_bytes()));
    body.extend_from_slice(&[0xE0, 0x7F, 0x10, 0x00, b'O', b'B', 0, 0, 0xFF, 0xFF, 0xFF, 0xFF]);
    body.extend_from_slice(&[0xFE, 0xFF, 0x00, 0xE0, 0, 0, 0, 0]);
    for _ in 0..n {
        body.extend_from_slice(&[0xFE, 0xFF, 0x00, 0xE0]);
        body.extend_from_slice(&70_000u32.to_le_bytes());
        body.extend(std::iter::repeat_n(0u8, 70_000));
    }
    body.extend_from_slice(&[0xFE, 0xFF, 0xDD, 0xE0, 0, 0, 0, 0]);
    let data = dicom("1.2.840.10008.1.2.4.90", &body);
    let (ir, facts, _) = refkv_ir::run::run_bytes(refkv_ir::run::Format::Dicom, &data).unwrap();
    check::check(&ir).unwrap();
    assert!(rebuilt(&ir, &data) == data);
    assert!(facts["rounds"].as_u64().unwrap() > n as u64);
}

#[test]
fn rejects_a_dicom_range_asked_for_and_not_fed() {
    use refkv_ir::nd2::Step;
    let err = |r: Result<Step, String>| match r {
        Err(m) => m,
        Ok(_) => panic!("stepped on"),
    };
    let data = std::fs::read(fixtures().join("dicom/dicom_implicit_mono16.dcm")).unwrap();
    // fed short: one byte less than asked
    let mut p = Dicom::new(data.len() as u64);
    let Step::Read(r) = p.step().unwrap() else { panic!("no batch") };
    let (o, n) = r[0];
    p.feed(o, data[o as usize..(o + n - 1) as usize].to_vec());
    assert_eq!(err(p.step()), format!("internal: the range ({o}, {n}) was asked for and not fed"));
    // not fed at all
    let mut p = Dicom::new(data.len() as u64);
    assert!(p.step().is_ok());
    assert!(err(p.step()).ends_with("was asked for and not fed"));
}

/// Each accepted fixture's facts name its descriptions by their keys (spec/stored-array.md §7
/// <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): `declared_by`
/// the top-level Pixel Data's element, `palette.declared_by` the lookup tables' elements, and
/// `nested` each element that keys the Pixel Data of its items, indexed from 0. The keys are all
/// the IR describes, and the Pixel Data with a grid has a row for each unit the facts count.
#[test]
fn every_accepted_fixtures_facts_name_its_descriptions() {
    let (mut files, mut cut, mut tables, mut items) = (0, 0, 0, 0);
    for e in std::fs::read_dir(fixtures().join("dicom")).unwrap() {
        let p = e.unwrap().path();
        let name = p.file_name().unwrap().to_str().unwrap().to_string();
        if name.contains("reject") || !name.ends_with(".dcm") {
            continue;
        }
        let (ir, facts) = refkv_ir::parse_bytes::<Dicom>(&std::fs::read(&p).unwrap()).unwrap();
        let s = &ir.stored;
        let key = facts["declared_by"].as_u64().unwrap() as u32;
        assert_eq!(ir.path(key), "dataset/7FE00010", "{name}");
        assert_eq!(facts["pixel"].as_u64(), Some(key as u64), "{name}");
        let mut named: Vec<(u32, Option<u64>)> = vec![(key, None)];
        for (c, table) in facts["palette"]["declared_by"].as_array().into_iter().flatten().enumerate() {
            let table = table.as_u64().unwrap() as u32;
            assert_eq!(ir.path(table), format!("dataset/0028120{}", c + 1), "{name}");
            named.push((table, None));
            tables += 1;
        }
        for n in facts["nested"].as_array().unwrap() {
            let (sequence, count) = (n["declared_by"].as_u64().unwrap() as u32, n["count"].as_u64().unwrap());
            assert!(ir.path(sequence).starts_with("dataset/") && count >= 1, "{name}");
            named.extend((0..count).map(|i| (sequence, Some(i))));
            items += count;
        }
        let described: Vec<(u32, Option<u64>)> = s.arrays.iter().map(|a| (a.element, a.index)).collect();
        named.sort();
        assert_eq!(named, described, "{name}");
        let k = s.arrays.iter().position(|a| a.element == key).unwrap();
        let rows = (0..s.map.len()).filter(|&r| s.map.array[r] == k as u32).count() as u64;
        let units = if s.arrays[k].grid.is_some() { facts["stored"].as_u64().unwrap() } else { 0 };
        assert_eq!(rows, units, "{name}");
        cut += u64::from(units > 0);
        files += 1;
    }
    assert!(files >= 77 && cut >= 76 && tables >= 12 && items >= 2, "{files} files, {cut} with a grid, {tables} tables, {items} items");
}
