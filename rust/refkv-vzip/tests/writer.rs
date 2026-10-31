//! The writer against the Python writer's archives (tests/golden/make.py writes them), byte for
//! byte, and the writer's refusals, one test each.

use refkv_vzip::{Error, Options, Range, Source, SourceKind, Writer};
use std::path::Path;

/// `got` is the golden archive `name`; else it is kept beside the tests' temporary files.
fn same(got: Vec<u8>, name: &str) {
    let want = std::fs::read(Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/golden").join(format!("{name}.vzip"))).unwrap();
    if got != want {
        let kept = Path::new(env!("CARGO_TARGET_TMPDIR")).join(format!("{name}.vzip"));
        std::fs::write(&kept, &got).unwrap();
        panic!("{name}: the archive differs from the golden one; it is at {}", kept.display());
    }
}

/// tests/golden/make.py's stand-in for a range's CRC-32C.
fn crc(url: &str, offset: u64, length: u64) -> u32 {
    ((url.len() as u64 * 1000 + offset * 10 + length) & 0xffff_ffff) as u32
}

/// make.py's `entries`, with the CRC-32C of each url source's range when `checksums`.
fn entries(w: &mut Writer<Vec<u8>>, checksums: bool) {
    let a = w.source(Source { size: Some(1000), etag: Some("\"v1\"".into()), ..Source::url("data/a%20b.bin") }).unwrap();
    let h = w.blob(b"HEADER").unwrap();
    let k = w.internal("__vz__/shared").unwrap();
    w.source(Source { modified_not_after: Some(-5), ..Source::url("http://h/x") }).unwrap();
    let url = "data/a%20b.bin";
    let of = |off: u64, len: u64| Range { crc32c: checksums.then(|| crc(url, off, len)), ..Range::of(a, off, len) };
    w.add_hidden("shared", b"0123456789", false, false).unwrap();
    w.add_bytes("x/zarr.json", br#"{"zarr_format":3,"node_type":"array"}"#, false, true).unwrap();
    w.add_bytes("zarr.json", br#"{"zarr_format":3,"node_type":"group"}"#.repeat(3).as_slice(), false, true).unwrap();
    w.add_bytes("x/attrs", b"attributes ".repeat(50).as_slice(), true, false).unwrap();
    w.add_bytes("x/raw", &(0..=255).collect::<Vec<u8>>(), false, false).unwrap();
    w.add_bytes("x/empty", b"", false, false).unwrap();
    w.add_ranges("x/c/0", vec![of(10, 4)]).unwrap();
    w.add_ranges("x/c/1", vec![Range::of(h, 0, 6), of(300, 70000), Range::literal(vec![0, 0xff])]).unwrap();
    w.add_ranges("x/c/2", vec![Range::of(k, 2, 3)]).unwrap();
    w.add_ranges("x/c/3", vec![Range { crc32c: Some(7), ..Range::of(a, 0, 0) }]).unwrap();
    for i in 0..40u64 {
        w.add_ranges(&format!("y/c/{i}"), vec![of(i * 25, 25)]).unwrap();
    }
}

#[test]
fn writes_the_python_writers_archives() {
    for (name, mirror_refs, page_size, checksums) in
        [("basic", true, None, false), ("paged", false, Some(200), false), ("checksums", true, Some(65536), true)]
    {
        let mut w = Writer::new(Vec::new(), Options { mirror_refs, page_size });
        entries(&mut w, checksums);
        same(w.close().unwrap(), name);
    }
    for (name, page_size) in [("empty", None), ("empty_paged", Some(1))] {
        let w = Writer::new(Vec::new(), Options { mirror_refs: true, page_size });
        same(w.close().unwrap(), name);
    }
    let mut w = Writer::new(Vec::new(), Options { mirror_refs: true, page_size: Some(100) });
    w.add_url_refs(
        (0..30u64).map(|i| {
            let url = format!("http://h/s/a/{i}");
            let c = crc(&url, 0, i * 3);
            (format!("a/{i}"), url, i * 3, Some(c))
        }),
        true,
    )
    .unwrap();
    w.add_url_refs(
        (0..3).map(|i| {
            let url = format!("http://h/s/b/{i}");
            let c = crc(&url, 0, 5);
            (format!("b/{i}"), url, 5, Some(c))
        }),
        false,
    )
    .unwrap();
    w.add_bytes("a/zarr.json", b"{}", false, true).unwrap();
    same(w.close().unwrap(), "bulk");

    let dir = tempfile::tempdir().unwrap();
    let p = dir.path().join("f.bin");
    std::fs::write(&p, b"file bytes ".repeat(100)).unwrap();
    let mut w = Writer::new(Vec::new(), Options::default());
    w.add_file("f", &p).unwrap();
    w.add_bytes("g", b"g", false, false).unwrap();
    same(w.close().unwrap(), "file");
}

fn refused(r: Result<impl std::fmt::Debug, Error>) {
    assert!(matches!(r, Err(Error::Invalid(_))), "{r:?}");
}

fn writer() -> Writer<Vec<u8>> {
    Writer::new(Vec::new(), Options::default())
}

#[test]
fn refuses_an_empty_key() {
    refused(writer().add_bytes("", b"", false, false));
}

#[test]
fn refuses_a_key_longer_than_a_zip_name() {
    refused(writer().add_bytes(&"k".repeat(65536), b"", false, false));
}

#[test]
fn refuses_a_duplicate_key() {
    let mut w = writer();
    w.add_bytes("k", b"", false, false).unwrap();
    refused(w.add_bytes("k", b"", false, true));
}

#[test]
fn refuses_a_duplicate_late_key() {
    let mut w = writer();
    w.add_bytes("k", b"", false, true).unwrap();
    refused(w.add_bytes("k", b"", false, true));
}

#[test]
fn refuses_a_reserved_key() {
    refused(writer().add_bytes("__vz__/x", b"", false, false));
}

#[test]
fn refuses_a_reserved_reference_key() {
    refused(writer().add_ranges("__vz__/x", vec![Range::literal(vec![1])]));
}

#[test]
fn refuses_a_hidden_format_entry() {
    refused(writer().add_hidden("sources", b"", false, false));
}

#[test]
fn refuses_an_unregistered_source() {
    refused(writer().add_ranges("k", vec![Range::of(0, 0, 1)]));
}

#[test]
fn refuses_a_literal_range_with_a_checksum() {
    refused(writer().add_ranges("k", vec![Range { crc32c: Some(1), ..Range::literal(vec![1]) }]));
}

#[test]
fn a_refused_reference_leaves_no_range_to_check() {
    let mut w = writer();
    let blob = w.blob(b"abc").unwrap();
    w.add_bytes("k", b"x", false, false).unwrap();
    // were its range kept, close would find it past the end of the blob
    refused(w.add_ranges("k", vec![Range::of(blob, 2, 2)]));
    w.close().unwrap();
}

#[test]
fn a_refused_bulk_reference_leaves_no_source() {
    let mut w = writer();
    w.add_bytes("k", b"x", false, false).unwrap();
    // were its url kept, close would find it invalid
    refused(w.add_url_refs([("k".to_string(), "not a uri".to_string(), 1, None)], false));
    w.close().unwrap();
}

#[test]
fn a_reference_cannot_take_the_key_of_a_late_entry() {
    let mut w = writer();
    let blob = w.blob(b"abc").unwrap();
    w.add_bytes("k", b"x", false, true).unwrap();
    refused(w.add_ranges("k", vec![Range::of(blob, 0, 1)]));
    refused(w.add_url_refs([("k".to_string(), "http://h/o".to_string(), 1, None)], false));
    refused(w.add_bytes("k", b"y", false, false));
    w.close().unwrap();
}

#[test]
fn refuses_a_range_past_a_reference_as_a_key_source_naming_one() {
    // as the phkv writer does, whatever the range
    let mut w = writer();
    let blob = w.blob(b"abc").unwrap();
    w.add_ranges("r", vec![Range::of(blob, 0, 1)]).unwrap();
    let r = w.internal("r").unwrap();
    w.add_ranges("s", vec![Range::of(r, 0, 1000)]).unwrap();
    match w.close() {
        Err(Error::Invalid(m)) => assert!(m.contains("internal references to reference entries"), "{m:?}"),
        r => panic!("{r:?}"),
    }
}

#[test]
fn a_bulk_reference_is_kept_only_where_it_was_prepared() {
    use refkv_vzip::sources::SourceTable;
    let mut t = SourceTable::default();
    let a = t.url_reference("a", "http://h/a".into(), 1, None, false).unwrap();
    let b = t.url_reference("b", "http://h/b".into(), 1, None, false).unwrap();
    t.keep(a).unwrap();
    // `b` names source 0, which is `a`'s
    refused(t.keep(b));
    assert_eq!(t.len(), 1);

    let c = t.url_reference("c", "http://h/c".into(), 1, None, false).unwrap();
    t.add(Source::url("http://h/other")).unwrap();
    refused(t.keep(c));
    assert_eq!(t.len(), 2);
}

#[test]
fn refuses_a_range_past_2_64() {
    let mut w = writer();
    let s = w.url("a").unwrap();
    refused(w.add_ranges("k", vec![Range::of(s, u64::MAX, 1)]));
}

#[test]
fn refuses_a_reference_past_2_64() {
    let mut w = writer();
    let s = w.url("a").unwrap();
    refused(w.add_ranges("k", vec![Range::of(s, 0, u64::MAX), Range::of(s, 0, 1)]));
}

#[test]
fn refuses_a_payload_over_the_limit() {
    refused(writer().add_ranges("k", vec![Range::literal(vec![0; 65520])]));
}

#[test]
fn refuses_pins_on_a_data_source() {
    refused(writer().source(Source { size: Some(1), ..Source::data(vec![1]) }));
}

#[test]
fn refuses_a_weak_etag() {
    refused(writer().source(Source { etag: Some("W/\"x\"".into()), ..Source::url("a") }));
}

#[test]
fn refuses_an_invalid_bulk_url() {
    let mut w = writer();
    w.add_url_refs([("k".to_string(), "a b".to_string(), 1, None)], false).unwrap();
    refused(w.close());
}

#[test]
fn refuses_an_empty_key_source() {
    let mut w = writer();
    w.internal("").unwrap();
    refused(w.close());
}

#[test]
fn refuses_a_range_past_a_data_source() {
    let mut w = writer();
    let s = w.blob(b"abc").unwrap();
    w.add_ranges("k", vec![Range::of(s, 2, 2)]).unwrap();
    refused(w.close());
}

#[test]
fn refuses_a_range_past_a_key_source() {
    let mut w = writer();
    let s = w.internal("__vz__/h").unwrap();
    w.add_hidden("h", b"abc", false, true).unwrap();
    w.add_ranges("k", vec![Range::of(s, 0, 4)]).unwrap();
    refused(w.close());
}

#[test]
fn refuses_a_key_source_to_a_missing_entry() {
    let mut w = writer();
    w.internal("nowhere").unwrap();
    refused(w.close());
}

#[test]
fn refuses_a_key_source_naming_a_format_entry() {
    let mut w = writer();
    w.internal("__vz__/sources").unwrap();
    refused(w.close());
}

#[test]
fn refuses_an_invalid_url_source() {
    let mut w = writer();
    w.url("a b").unwrap();
    refused(w.close());
}

#[test]
fn refuses_an_empty_url_source() {
    let mut w = writer();
    w.source(Source { kind: SourceKind::Url(String::new()), size: None, etag: None, modified_not_after: None }).unwrap();
    refused(w.close());
}

#[test]
fn refuses_a_key_source_to_a_reference() {
    let mut w = writer();
    w.internal("r").unwrap();
    w.add_ranges("r", vec![Range::literal(vec![1])]).unwrap();
    refused(w.close());
}
