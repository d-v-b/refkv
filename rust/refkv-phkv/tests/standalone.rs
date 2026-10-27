//! Standalone phkv stores, written and read back (spec/bindings/phkv.md
//! <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md>).

use refkv_phkv::format::{self, Superblock};
use refkv_phkv::{Class, Codec, EntryInfo, Kind, NoUrls, Options, Prop, Request, Resolver, Store, Writer};
use refkv_vzip::pb::{Range, Source};
use std::collections::HashMap;

fn write(opts: Options, f: impl FnOnce(&mut Writer<Vec<u8>>)) -> Vec<u8> {
    let mut w = Writer::new(Vec::new(), opts);
    f(&mut w);
    w.close().expect("close")
}

fn whole(s: &Store<Vec<u8>>, key: &str) -> Option<Vec<u8>> {
    s.get(key, Request::Whole, &NoUrls).expect("get")
}

/// A resolver over in-memory objects, which checks the size pin.
struct Objects(HashMap<String, Vec<u8>>);

impl Resolver for Objects {
    fn read(&self, src: &Source, offset: u64, length: u64) -> Result<Vec<u8>, String> {
        let o = self.0.get(src.url_str().unwrap()).ok_or("no such object")?;
        if src.size.is_some_and(|n| n != o.len() as u64) {
            return Err("size pin fails".into());
        }
        o.get(offset as usize..(offset + length) as usize).map(|b| b.to_vec()).ok_or_else(|| "short object".into())
    }
}

#[test]
fn empty_store() {
    let b = write(Options::default(), |_| {});
    let s = Store::open(b).unwrap();
    assert_eq!(s.key_count(), 0);
    assert_eq!(s.classify("a").unwrap(), None);
    assert_eq!(whole(&s, "a"), None);
    assert!(s.list("").unwrap().is_empty());
    assert_eq!(s.revision(), Some(refkv_vzip::SPEC_REVISION));
}

#[test]
fn one_key() {
    let b = write(Options::default(), |w| w.add_bytes("zarr.json", b"{}").unwrap());
    let s = Store::open(b).unwrap();
    assert_eq!(s.classify("zarr.json").unwrap(), Some(Kind::Bytes));
    assert_eq!(whole(&s, "zarr.json").unwrap(), b"{}");
    assert_eq!(s.classify("zarr.jso").unwrap(), None);
    assert_eq!(s.list("").unwrap(), ["zarr.json"]);
}

#[test]
fn values_references_and_listing() {
    let big: Vec<u8> = (0..100_000u32).flat_map(|i| (i % 97).to_le_bytes()).collect();
    let object: Vec<u8> = (0..=255u8).cycle().take(5000).collect();
    let opts = Options { inline_max: 64, ..Options::default() };
    let b = write(opts, |w| {
        w.add_bytes("a/zarr.json", b"{\"zarr_format\":3}").unwrap();
        w.add_bytes_with("a/c/0", &big, Codec::None).unwrap();
        w.add_bytes_with("a/c/1", &big, Codec::Deflate).unwrap();
        w.add_bytes_with("a/c/2", &big, Codec::Zstd).unwrap();
        w.add_bytes_with("a/c/3", b"tiny", Codec::Zstd).unwrap(); // inline and compressed
        w.add_bytes("empty", b"").unwrap();
        let header = w.blob(b"HEADER").unwrap();
        let shared = w.internal("a/c/2").unwrap();
        let url = w.source(Source { size: Some(object.len() as u64), ..Source::url("https://example.org/o.bin") }).unwrap();
        w.add_ranges("r/one", &[Range::of(url, 10, 20)]).unwrap();
        w.add_ranges("r/concat", &[Range::of(header, 0, 6), Range::literal(b"--".to_vec()), Range::of(shared, 4, 8)]).unwrap();
        let crc = format::crc32c(&object[100..200]);
        w.add_ranges("r/crc", &[Range { crc32c: Some(crc), ..Range::of(url, 100, 100) }]).unwrap();
        w.set_property("a/c/0", "mtime", Prop::I64(-5)).unwrap();
        w.set_property("a/c/2", "mtime", Prop::I64(1_700_000_000_000_000_000)).unwrap();
        w.set_property("a/zarr.json", "media_type", Prop::Str("application/json".into())).unwrap();
        w.set_property("r/one", "org.example:tag", Prop::Bytes(vec![1, 2, 3])).unwrap();
        w.set_metadata("writer", b"tests").unwrap();
    });
    let s = Store::open(b).unwrap();
    let objects = Objects(HashMap::from([("https://example.org/o.bin".to_string(), object.clone())]));

    for k in ["a/c/0", "a/c/1", "a/c/2"] {
        assert_eq!(whole(&s, k).unwrap(), big, "{k}");
        assert_eq!(s.get(k, Request::Range(10, 20), &NoUrls).unwrap().unwrap(), big[10..20]);
        assert_eq!(s.get(k, Request::Suffix(3), &NoUrls).unwrap().unwrap(), big[big.len() - 3..]);
        assert_eq!(s.get(k, Request::Offset(big.len() as u64 + 9), &NoUrls).unwrap().unwrap(), b"");
    }
    assert_eq!(whole(&s, "a/c/3").unwrap(), b"tiny");
    assert_eq!(whole(&s, "empty").unwrap(), b"");
    assert_eq!(s.get("r/one", Request::Whole, &objects).unwrap().unwrap(), object[10..30]);
    let mut concat = b"HEADER--".to_vec();
    concat.extend_from_slice(&big[4..12]);
    assert_eq!(whole(&s, "r/concat").unwrap(), concat);
    assert_eq!(s.get("r/concat", Request::Range(5, 9), &NoUrls).unwrap().unwrap(), concat[5..9]);
    assert_eq!(s.get("r/crc", Request::Range(1, 2), &objects).unwrap().unwrap(), object[101..102]);
    // a url source without a resolver is a resolution error, for the requests that need it
    assert_eq!(s.get("r/one", Request::Whole, &NoUrls).unwrap_err().class, Class::Resolution);
    assert_eq!(s.get("a/c/0", Request::Range(5, 4), &NoUrls).unwrap_err().class, Class::Request);

    assert_eq!(s.classify("r/one").unwrap(), Some(Kind::Reference));
    assert_eq!(s.list("").unwrap(), ["a/c/0", "a/c/1", "a/c/2", "a/c/3", "a/zarr.json", "empty", "r/concat", "r/crc", "r/one"]);
    assert_eq!(s.list("a/c/").unwrap(), ["a/c/0", "a/c/1", "a/c/2", "a/c/3"]);
    assert_eq!(s.list("r/c").unwrap(), ["r/concat", "r/crc"]);
    assert!(s.list("zzz").unwrap().is_empty());

    // raw gives the stored bytes, and entry_info says how to read them
    let raw = s.raw("a/c/2").unwrap().unwrap();
    let Some(EntryInfo::Bytes { codec: Codec::Zstd, stored_length, size, .. }) = s.entry_info("a/c/2").unwrap() else { panic!() };
    assert_eq!((raw.len() as u64, size), (stored_length, big.len() as u64));
    assert_eq!(format::decode(Codec::Zstd, &raw, size).unwrap(), big);
    assert_eq!(s.raw("a/c/0").unwrap().unwrap(), big);
    assert_eq!(s.raw("r/one").unwrap().unwrap(), Range::of(2, 10, 20).encode());

    assert_eq!(s.property("a/c/0", "mtime").unwrap(), Some(Some(Prop::I64(-5))));
    assert_eq!(s.property("a/c/2", "mtime").unwrap(), Some(Some(Prop::I64(1_700_000_000_000_000_000))));
    assert_eq!(s.property("a/c/1", "mtime").unwrap(), Some(None));
    assert_eq!(s.property("a/zarr.json", "media_type").unwrap(), Some(Some(Prop::Str("application/json".into()))));
    assert_eq!(s.property("r/one", "org.example:tag").unwrap(), Some(Some(Prop::Bytes(vec![1, 2, 3]))));
    assert_eq!(s.property("r/concat", "org.example:tag").unwrap(), Some(None));
    assert_eq!(s.property("nope", "mtime").unwrap(), None);
    assert_eq!(s.property("a/c/0", "org.example:none").unwrap(), Some(None));
    assert_eq!(s.metadata(), [("writer".to_string(), b"tests".to_vec())]);
}

#[test]
fn many_keys_and_misses() {
    let n = 50_000;
    let opts = Options { page_size: 1024, ..Options::default() };
    let b = write(opts, |w| {
        for i in 0..n {
            w.add_bytes(&format!("arr/c/{}/{}", i / 100, i % 100), &(i as u32).to_le_bytes()).unwrap();
        }
    });
    let s = Store::open(b).unwrap();
    for i in 0..n {
        assert_eq!(whole(&s, &format!("arr/c/{}/{}", i / 100, i % 100)).unwrap(), (i as u32).to_le_bytes());
        assert_eq!(whole(&s, &format!("arr/c/{}/{}x", i / 100, i % 100)), None);
    }
    assert_eq!(s.list("").unwrap().len(), n);
    assert_eq!(s.list("arr/c/12/").unwrap().len(), 100);
    assert_eq!(s.list("arr/c/12").unwrap().len(), 1100); // 12/, and 120/ to 129/
}

#[test]
fn every_probe_distance() {
    for d in format::probe_distances().take_while(|&d| d <= 1 << 20) {
        let b = write(Options { distance: d, ..Options::default() }, |w| w.add_bytes("k", b"v").unwrap());
        let s = Store::open(b).unwrap_or_else(|e| panic!("D = {d}: {e}"));
        assert_eq!(s.superblock().distance, d);
        assert_eq!(whole(&s, "k").unwrap(), b"v");
    }
    let r = Writer::new(Vec::new(), Options { distance: 3, ..Options::default() }).close();
    assert!(r.is_err(), "3 is not a probe distance");
}

#[test]
fn appended_bytes_hide_the_superblock() {
    // the superblock records its distance, so bytes appended after it do not leave a stale match
    let mut b = write(Options::default(), |w| w.add_bytes("k", b"v").unwrap());
    b.extend_from_slice(&[0u8; 1024]);
    assert_eq!(Store::open(b).err().unwrap().class, Class::Archive);
}

#[test]
fn deterministic() {
    let make = || {
        write(Options::default(), |w| {
            for i in (0..1000).rev() {
                w.add_bytes_with(&format!("k{i}"), format!("value {i}").repeat(i % 7).as_bytes(), if i % 2 == 0 { Codec::Zstd } else { Codec::Deflate }).unwrap();
            }
            w.set_property("k3", "mode", Prop::U64(0o644)).unwrap();
        })
    };
    assert_eq!(make(), make());
}

/// `r` is an invalid-store error whose message has `needle`: the text the vzip writer gives for the
/// same store, which the source table shares.
fn refused_with(r: Result<impl std::fmt::Debug, refkv_phkv::Error>, needle: &str) {
    match r {
        Err(refkv_phkv::Error::Invalid(m)) => assert!(m.contains(needle), "{m:?} lacks {needle:?}"),
        r => panic!("not refused as invalid: {r:?}"),
    }
}

fn writer() -> Writer<Vec<u8>> {
    Writer::new(Vec::new(), Options::default())
}

#[test]
fn refuses_a_duplicate_key() {
    let mut w = writer();
    w.add_bytes("a", b"x").unwrap();
    assert!(w.add_bytes("a", b"y").is_err());
}

#[test]
fn refuses_an_empty_key() {
    assert!(writer().add_bytes("", b"y").is_err());
}

#[test]
fn refuses_an_invalid_url() {
    let mut w = writer();
    w.url("not a uri").unwrap(); // checked at close, as the vzip writer does
    refused_with(w.close(), "empty or invalid url sources");
}

#[test]
fn refuses_an_empty_key_source() {
    let mut w = writer();
    w.internal("").unwrap();
    refused_with(w.close(), "empty key source");
}

#[test]
fn refuses_a_key_source_naming_a_reference() {
    let mut w = writer();
    let blob = w.blob(b"abc").unwrap();
    w.add_ranges("r", &[Range::of(blob, 0, 1)]).unwrap();
    let r = w.internal("r").unwrap();
    w.add_ranges("s", &[Range::of(r, 0, 1)]).unwrap();
    refused_with(w.close(), "internal references to reference entries");
}

#[test]
fn refuses_a_range_past_a_reference_as_a_key_source_naming_one() {
    // as the vzip writer does, whatever the range
    let mut w = writer();
    let blob = w.blob(b"abc").unwrap();
    w.add_ranges("r", &[Range::of(blob, 0, 1)]).unwrap();
    let r = w.internal("r").unwrap();
    w.add_ranges("s", &[Range::of(r, 0, 1000)]).unwrap();
    refused_with(w.close(), "internal references to reference entries");
}

#[test]
fn takes_a_key_source_named_like_a_vzip_format_entry() {
    // a standalone store reserves no names
    let mut w = writer();
    w.add_bytes("__vz__/sources", b"abc").unwrap();
    let k = w.internal("__vz__/sources").unwrap();
    w.add_ranges("r", &[Range::of(k, 1, 2)]).unwrap();
    let s = Store::open(w.close().unwrap()).unwrap();
    assert_eq!(whole(&s, "r").unwrap(), b"bc");
}

#[test]
fn refuses_an_unregistered_source() {
    assert!(writer().add_ranges("r", &[Range::of(7, 0, 1)]).is_err());
}

#[test]
fn refuses_a_registered_property_of_the_wrong_type() {
    let mut w = writer();
    w.add_bytes("a", b"x").unwrap();
    assert!(w.set_property("a", "mtime", Prop::U64(1)).is_err());
}

#[test]
fn refuses_an_unregistered_property_name_without_a_colon() {
    let mut w = writer();
    w.add_bytes("a", b"x").unwrap();
    assert!(w.set_property("a", "unregistered", Prop::U64(1)).is_err());
}

#[test]
fn refuses_a_range_past_the_end_of_a_data_source() {
    let mut w = writer();
    let blob = w.blob(b"abc").unwrap();
    w.add_ranges("r", &[Range::of(blob, 2, 2)]).unwrap();
    refused_with(w.close(), "past the end of source");
}

#[test]
fn refuses_a_key_source_naming_a_missing_key() {
    let mut w = writer();
    let k = w.internal("missing").unwrap();
    w.add_ranges("r", &[Range::of(k, 0, 1)]).unwrap();
    refused_with(w.close(), "internal references to missing entries");
}

#[test]
fn refuses_a_property_of_a_missing_key() {
    let mut w = writer();
    w.set_property("ghost", "mode", Prop::U64(1)).unwrap();
    w.add_bytes("a", b"x").unwrap();
    assert!(w.close().is_err());
}

#[test]
fn a_file_that_does_not_open_adds_no_key() {
    let mut w = writer();
    w.add_bytes("a", b"x").unwrap();
    assert!(w.add_file("b", std::path::Path::new("/no/such/file")).is_err());
    // "b" is not in the store: a property of it fails at close, without a panic
    w.set_property("b", "mode", Prop::U64(1)).unwrap();
    assert!(w.close().is_err());
}

#[test]
fn a_key_whose_file_does_not_open_can_be_added_again() {
    let mut w = writer();
    assert!(w.add_file("b", std::path::Path::new("/no/such/file")).is_err());
    w.add_bytes("b", b"y").unwrap();
    let s = Store::open(w.close().unwrap()).unwrap();
    assert_eq!(whole(&s, "b").unwrap(), b"y");
}

#[test]
fn a_reference_with_a_duplicate_key_leaves_no_range_to_check() {
    let mut w = writer();
    let blob = w.blob(b"abc").unwrap();
    w.add_bytes("r", b"x").unwrap();
    // were its range kept, close would find it past the end of the blob
    assert!(w.add_ranges("r", &[Range::of(blob, 2, 2)]).is_err());
    let s = Store::open(w.close().unwrap()).unwrap();
    assert_eq!(whole(&s, "r").unwrap(), b"x");
}

#[test]
fn a_reference_with_too_long_a_payload_leaves_no_range_to_check() {
    let mut w = writer();
    let blob = w.blob(b"abc").unwrap();
    let ranges = [Range::literal(vec![0; 1 << 24]), Range::of(blob, 2, 2)];
    assert!(w.add_ranges("r", &ranges).is_err());
    w.close().unwrap();
}

/// The superblock of a store written with D = 0.
fn superblock(b: &[u8]) -> Superblock {
    Superblock::matching(&b[b.len() - 64..], 0).unwrap()
}

/// A store of three values written out of line: "a" stored as is (first, at offset 0), "b"
/// DEFLATE-compressed and "c" Zstandard-compressed.
fn three_values() -> Vec<u8> {
    write(Options { inline_max: 0, ..Options::default() }, |w| {
        w.add_bytes_with("a", b"0123456789", Codec::None).unwrap();
        w.add_bytes_with("b", &[7u8; 1000], Codec::Deflate).unwrap();
        w.add_bytes_with("c", &[9u8; 1000], Codec::Zstd).unwrap();
    })
}

#[test]
fn not_a_store_is_an_archive_error() {
    assert_eq!(Store::open(vec![0u8; 100]).err().unwrap().class, Class::Archive);
}

#[test]
fn a_damaged_superblock_is_an_archive_error() {
    let mut b = three_values();
    let n = b.len();
    b[n - 30] ^= 1;
    assert_eq!(Store::open(b).err().unwrap().class, Class::Archive);
}

#[test]
fn a_damaged_directory_is_an_archive_error() {
    let mut b = three_values();
    let sb = superblock(&b);
    b[sb.directory_offset as usize + 10] ^= 1;
    assert_eq!(Store::open(b).err().unwrap().class, Class::Archive);
}

#[test]
fn a_damaged_stored_value_fails_its_crc_when_read_whole() {
    let mut b = three_values();
    b[3] ^= 0xff;
    let s = Store::open(b).unwrap();
    assert_eq!(s.get("a", Request::Whole, &NoUrls).unwrap_err().class, Class::Body);
    // a part of it is not checked
    assert_eq!(s.get("a", Request::Range(5, 7), &NoUrls).unwrap().unwrap(), b"56");
    assert_eq!(s.raw("a").unwrap_err().class, Class::Body);
}

#[test]
fn a_damaged_compressed_value_is_a_body_error() {
    let b = three_values();
    let Some(EntryInfo::Bytes { stored_length, .. }) = Store::open(b.clone()).unwrap().entry_info("b").unwrap() else { panic!() };
    let mut b2 = b.clone();
    b2[10 + stored_length as usize - 1] ^= 0xff; // the last byte of "b"'s DEFLATE stream
    let s = Store::open(b2).unwrap();
    assert_eq!(s.get("b", Request::Range(0, 1), &NoUrls).unwrap_err().class, Class::Body);
    assert!(s.raw("b").is_ok(), "raw does not decode");
    assert_eq!(whole(&s, "c").unwrap(), vec![9u8; 1000]);
}

#[test]
fn slot_pointing_outside_records_is_an_entry_error() {
    let b = write(Options::default(), |w| w.add_bytes("k", b"v").unwrap());
    let sb = superblock(&b);
    let dir = format::DirEntry::decode_all(&b[sb.directory_offset as usize..(sb.directory_offset + sb.directory_length) as usize]).unwrap();
    let slots = dir.iter().find(|e| e.kind == format::section::SLOTS).unwrap();
    let s = Store::open(b.clone()).unwrap();
    assert!(s.classify("k").unwrap().is_some());
    // point every non-empty slot at offset 1, inside no record's start but inside the file
    let mut b2 = b.clone();
    for i in 0..slots.length / format::SLOT_SIZE {
        let at = (slots.offset + format::SLOT_SIZE * i) as usize;
        if b2[at..at + 12] != format::EMPTY_SLOT {
            b2[at..at + 12].copy_from_slice(&format::pack_slot(u64::MAX - 1, 28));
        }
    }
    let s2 = Store::open(b2).unwrap();
    assert_eq!(s2.classify("k").unwrap_err().class, Class::Entry);
}

#[test]
fn reads_from_a_file() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("t.phkv");
    let src = dir.path().join("big.bin");
    std::fs::write(&src, vec![42u8; 1 << 20]).unwrap();
    let f = std::fs::File::create(&path).unwrap();
    let mut w = Writer::new(f, Options::default());
    w.add_file("big", &src).unwrap();
    w.add_bytes("small", b"s").unwrap();
    w.close().unwrap();
    let s = Store::open(std::fs::File::open(&path).unwrap()).unwrap();
    assert_eq!(s.get("big", Request::Range(1000, 1004), &NoUrls).unwrap().unwrap(), [42u8; 4]);
    assert_eq!(s.get("big", Request::Whole, &NoUrls).unwrap().unwrap().len(), 1 << 20);
    assert_eq!(s.get("small", Request::Whole, &NoUrls).unwrap().unwrap(), b"s");
}

/// A file that logs each read's offset and length.
struct Logging(Vec<u8>, std::cell::RefCell<Vec<(u64, u64)>>);

impl refkv_phkv::ReadAt for Logging {
    fn size(&self) -> std::io::Result<u64> {
        self.0.size()
    }

    fn read_at(&self, offset: u64, buf: &mut [u8]) -> std::io::Result<()> {
        self.1.borrow_mut().push((offset, buf.len() as u64));
        self.0.read_at(offset, buf)
    }
}

#[test]
fn no_byte_is_read_twice() {
    // 1,500-byte values inline: a record is longer than the first read of it, and the sections are
    // longer than the tail
    let value = vec![7u8; 1500];
    // a page for each record, uncompressed, so that PAGES is longer than the tail
    let opts = Options { inline_max: 4096, page_size: 1, section_codec: Codec::None, ..Options::default() };
    let b = write(opts, |w| {
        for i in 0..5000 {
            w.add_bytes(&format!("k/{i:05}"), &value).unwrap();
        }
    });
    // a first read of the file and of a record shorter than the section and the record
    let limits = refkv_phkv::Limits { tail: (64 << 10) + 64, record_read: 512, ..Default::default() };
    let s = Store::open_with(Logging(b, Default::default()), limits).unwrap();
    assert_eq!(s.get("k/02500", Request::Whole, &NoUrls).unwrap().unwrap(), value);
    let (_, file) = s.into_reader();
    // the first read is the tail; a lookup's slot may lie inside it, since the tail is not kept after
    // open, but no other read overlaps it, or another read
    let all = file.1.borrow().clone();
    let tail = all[0];
    let mut reads: Vec<_> = all[1..].iter().copied().filter(|r| r.0 < tail.0 || r.0 + r.1 > tail.0 + tail.1).collect();
    assert!(reads.len() >= 3, "a section outside the tail, a slot and a record: {all:?}");
    reads.push(tail);
    reads.sort_unstable();
    for w in reads.windows(2) {
        assert!(w[0].0 + w[0].1 <= w[1].0, "reads overlap: {all:?}");
    }
}

#[test]
fn slots_read_at_open() {
    let b = write(Options::default(), |w| {
        for i in 0..3000 {
            w.add_bytes(&format!("k/{i:05}"), format!("value {i}").as_bytes()).unwrap();
        }
    });
    // slots one at a time, as the comparison
    let one = refkv_phkv::Limits { slot_block: 0, ..Default::default() };
    let plain = Store::open_with(Logging(b.clone(), Default::default()), one).unwrap();
    let limits = refkv_phkv::Limits { slots_at_open: 1 << 20, tail: 4096, ..Default::default() };
    let kept = Store::open_with(Logging(b.clone(), Default::default()), limits).unwrap();
    let lookups = |s: &Store<Logging>| {
        s.reader().1.borrow_mut().clear();
        for i in (0..3000).step_by(7) {
            assert_eq!(whole_in(s, &format!("k/{i:05}")).unwrap(), format!("value {i}").as_bytes());
        }
        assert_eq!(whole_in(s, "k/99999"), None);
        let n = s.reader().1.borrow().len();
        n
    };
    let (a, c) = (lookups(&plain), lookups(&kept));
    // 429 hits and a miss: a slot read and a record read each without the slots, only the record with them
    assert_eq!(a, 429 * 2 + 1, "a slot read and a record read per hit, a slot read for the miss");
    assert_eq!(c, 429, "a record read per hit, and none for the miss");
    // a limit below the section's size leaves them in the file
    let limits = refkv_phkv::Limits { slots_at_open: 100, ..one };
    let small = Store::open_with(Logging(b, Default::default()), limits).unwrap();
    assert_eq!(lookups(&small), a);
}

fn whole_in(s: &Store<Logging>, key: &str) -> Option<Vec<u8>> {
    s.get(key, Request::Whole, &NoUrls).unwrap()
}

#[test]
fn slots_read_in_blocks() {
    let b = write(Options::default(), |w| {
        for i in 0..3000 {
            w.add_bytes(&format!("k/{i:05}"), format!("value {i}").as_bytes()).unwrap();
        }
    });
    // 3,000 keys have a little over 3,000 slots: 36 KB, nine blocks of 4,092 bytes (341 whole slots)
    // a first read too short to hold any of SLOTS, so lookups read every block
    let limits = refkv_phkv::Limits { slot_block: 4096, record_read: 512, tail: 4096, ..Default::default() };
    let s = Store::open_with(Logging(b, Default::default()), limits).unwrap();
    s.reader().1.borrow_mut().clear();
    for i in 0..3000 {
        assert_eq!(whole_in(&s, &format!("k/{i:05}")).unwrap(), format!("value {i}").as_bytes());
    }
    assert_eq!(whole_in(&s, "k/99999"), None);
    let reads = s.reader().1.borrow().clone();
    // records are read 512 bytes at a time or less, blocks are longer: a record read per key, and each
    // of the nine blocks once, the miss included
    let blocks: Vec<_> = reads.iter().filter(|r| r.1 > 512).collect();
    assert_eq!(blocks.len(), 9, "{blocks:?}");
    assert!(blocks.iter().all(|r| r.1 <= 4092));
    assert_eq!(reads.len(), 3000 + 9);
}

#[test]
fn the_first_read_supplies_slot_blocks() {
    // 20,000 keys have about 160 KiB of slots, followed by PAGES and PTRHASH: a first read of 128 KiB
    // holds the end of SLOTS
    let b = write(Options::default(), |w| {
        for i in 0..20_000 {
            w.add_bytes(&format!("k/{i:05}"), b"v").unwrap();
        }
    });
    let limits = refkv_phkv::Limits { tail: 128 << 10, record_read: 512, ..Default::default() };
    let s = Store::open_with(Logging(b, Default::default()), limits).unwrap();
    let opened = s.reader().1.borrow().clone();
    s.reader().1.borrow_mut().clear();
    for i in 0..20_000 {
        assert_eq!(whole_in(&s, &format!("k/{i:05}")).unwrap(), b"v");
    }
    let reads = s.reader().1.borrow().clone();
    let blocks = reads.iter().filter(|r| r.1 == 4096 || (r.1 > 512 && r.1 < 4096)).count();
    let slot_blocks = 160_000u64.div_ceil(4096) as usize;
    // a record read per key; the blocks the first read did not hold, each once
    assert_eq!(reads.len(), 20_000 + blocks, "opened with {opened:?}");
    assert!(blocks > 0 && blocks < slot_blocks, "{blocks} blocks read of about {slot_blocks}");
}

#[test]
fn a_record_read_stops_at_the_end_of_its_page() {
    let b = write(Options { page_size: 1024, ..Options::default() }, |w| {
        for i in 0..5000 {
            w.add_bytes(&format!("k/{i:05}"), &[i as u8; 200]).unwrap();
        }
    });
    // a first read of a record far longer than a page
    let limits = refkv_phkv::Limits { record_read: 1 << 20, slot_block: 0, ..Default::default() };
    let s = Store::open_with(Logging(b, Default::default()), limits).unwrap();
    s.reader().1.borrow_mut().clear();
    for i in (0..5000).step_by(13) {
        assert_eq!(whole_in(&s, &format!("k/{i:05}")).unwrap(), vec![i as u8; 200]);
    }
    let reads = s.reader().1.borrow().clone();
    // a slot read (8 bytes) and one record read, no longer than a page and its last record, per key
    assert_eq!(reads.len(), 2 * 5000usize.div_ceil(13));
    assert!(reads.iter().all(|r| r.1 == 8 || r.1 <= 1024 + 300), "{:?}", reads.iter().map(|r| r.1).max());
}

#[test]
fn a_slot_gives_its_record_length() {
    // records of 3 sizes, one longer than 64 KiB
    let big = vec![5u8; 70_000];
    let b = write(Options { inline_max: 1 << 20, ..Options::default() }, |w| {
        w.add_bytes("small", b"v").unwrap();
        w.add_bytes("medium", &[1u8; 3000]).unwrap();
        w.add_bytes("big", &big).unwrap();
    });
    let limits = refkv_phkv::Limits { slots_at_open: 1 << 20, ..Default::default() };
    let s = Store::open_with(Logging(b.clone(), Default::default()), limits).unwrap();
    s.reader().1.borrow_mut().clear();
    assert_eq!(whole_in(&s, "small").unwrap(), b"v");
    assert_eq!(whole_in(&s, "medium").unwrap(), vec![1u8; 3000]);
    assert_eq!(whole_in(&s, "big").unwrap(), big);
    let reads = s.reader().1.borrow().clone();
    // one read for each, exactly as long as the record
    assert_eq!(reads.len(), 3, "{reads:?}");
    assert!(reads[0].1 < 40 && reads[1].1 > 3000 && reads[1].1 < 3050 && reads[2].1 > 70_000 && reads[2].1 < 70_050, "{reads:?}");

    // a slot whose length is wrong is an entry error
    let sb = superblock(&b);
    let dir = format::DirEntry::decode_all(&b[sb.directory_offset as usize..(sb.directory_offset + sb.directory_length) as usize]).unwrap();
    let slots = dir.iter().find(|e| e.kind == format::section::SLOTS).unwrap();
    let mut b2 = b.clone();
    for i in 0..slots.length / format::SLOT_SIZE {
        let at = (slots.offset + format::SLOT_SIZE * i) as usize;
        if let Some((off, Some(len))) = format::unpack_slot(&b2[at..at + 12]).unwrap() {
            b2[at..at + 12].copy_from_slice(&format::pack_slot(off, len + 1));
        }
    }
    let s2 = Store::open(b2).unwrap();
    assert_eq!(s2.classify("small").unwrap_err().class, Class::Entry);

    // a slot that gives no length (4 GiB - 1 or more) is read as before: a first read, then the rest
    let mut b3 = b.clone();
    for i in 0..slots.length / format::SLOT_SIZE {
        let at = (slots.offset + format::SLOT_SIZE * i) as usize;
        if let Some((off, _)) = format::unpack_slot(&b3[at..at + 12]).unwrap() {
            b3[at..at + 12].copy_from_slice(&format::pack_slot(off, u64::from(format::SLOT_LENGTH_MAX)));
        }
    }
    let s3 = Store::open_with(Logging(b3, Default::default()), refkv_phkv::Limits { record_read: 512, ..Default::default() }).unwrap();
    s3.reader().1.borrow_mut().clear();
    assert_eq!(whole_in(&s3, "big").unwrap(), big);
    assert_eq!(whole_in(&s3, "medium").unwrap(), vec![1u8; 3000]);
    let reads = s3.reader().1.borrow().clone();
    assert!(reads.iter().any(|r| r.1 == 512), "a first read of 512 bytes: {reads:?}");
}

#[test]
fn a_slot_is_a_u64_offset_and_a_u32_length() {
    let slot = format::pack_slot(29_999_999, 29);
    assert_eq!(slot[..8], 29_999_999u64.to_le_bytes());
    assert_eq!(slot[8..], 29u32.to_le_bytes());
    assert_eq!(format::unpack_slot(&slot).unwrap(), Some((29_999_999, Some(29))));
    // an offset of 0 is a record, since its length is not 0
    assert_eq!(format::unpack_slot(&format::pack_slot(0, 28)).unwrap(), Some((0, Some(28))));
    // 4 GiB - 1 or more is given as 0xFFFFFFFF, and read as no length
    for n in [u64::from(u32::MAX), 1 << 32, u64::MAX] {
        assert_eq!(format::pack_slot(5, n)[8..], [0xff; 4]);
        assert_eq!(format::unpack_slot(&format::pack_slot(5, n)).unwrap(), Some((5, None)));
    }
    assert_eq!(format::unpack_slot(&format::pack_slot(5, u64::from(u32::MAX) - 1)).unwrap(), Some((5, Some(u64::from(u32::MAX) - 1))));
    // all zero is the empty slot; a length of 0 with an offset is not
    assert_eq!(format::unpack_slot(&format::EMPTY_SLOT).unwrap(), None);
    assert!(format::unpack_slot(&format::pack_slot(5, 0)).is_err());
    // no record is shorter than 6 bytes
    assert!(format::unpack_slot(&format::pack_slot(5, 5)).is_err());
    assert_eq!(format::unpack_slot(&format::pack_slot(5, 6)).unwrap(), Some((5, Some(6))));
}
