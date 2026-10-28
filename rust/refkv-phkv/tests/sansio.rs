//! The sans-IO reader (src/sansio.rs): a store read by rounds from fed ranges gives what the
//! blocking reader gives.

use refkv_phkv::sansio::{Needs, Session, Step};
use refkv_phkv::{Class, Codec, NoUrls, Options, Request, Resolver, Store, Writer};
use refkv_vzip::pb::{Range, Source};
use std::collections::HashMap;

/// The objects the url sources name.
fn objects() -> HashMap<String, Vec<u8>> {
    HashMap::from([("https://data.test/object".to_string(), (0..=255u8).cycle().take(5000).collect::<Vec<u8>>())])
}

struct Objects(HashMap<String, Vec<u8>>);

impl Resolver for Objects {
    fn read(&self, src: &Source, offset: u64, length: u64) -> Result<Vec<u8>, String> {
        let o = self.0.get(src.url_str().unwrap()).ok_or("no such object")?;
        o.get(offset as usize..(offset + length) as usize).map(|b| b.to_vec()).ok_or_else(|| "short object".into())
    }
}

fn big() -> Vec<u8> {
    (0..100_000u32).flat_map(|i| (i % 97).to_le_bytes()).collect()
}

fn store() -> Vec<u8> {
    let opts = Options { inline_max: 64, ..Options::default() };
    let mut w = Writer::new(Vec::new(), opts);
    w.add_bytes("a/zarr.json", b"{\"zarr_format\":3}").unwrap();
    w.add_bytes_with("a/plain", &big(), Codec::None).unwrap();
    w.add_bytes_with("a/deflate", &big(), Codec::Deflate).unwrap();
    w.add_bytes_with("a/zstd", &big(), Codec::Zstd).unwrap();
    let url = w.url("https://data.test/object").unwrap();
    w.add_ranges("a/ref", &[Range::of(url, 100, 300)]).unwrap();
    w.add_ranges("a/concat", &[Range::of(url, 0, 10), Range::literal(b"--".to_vec()), Range::of(url, 4990, 10)]).unwrap();
    let inner = w.internal("a/plain").unwrap();
    w.add_ranges("a/via-key", &[Range::of(inner, 4, 8)]).unwrap();
    for i in 0..3000 {
        w.add_bytes(&format!("many/{i:05}"), format!("value {i}").as_bytes()).unwrap();
    }
    w.close().unwrap()
}

/// Runs `op` by rounds until it is done, serving what each round needs from `file` and
/// `objects`; the number of rounds with it.
fn drive<T>(s: &mut Session, file: &[u8], objects: &HashMap<String, Vec<u8>>, mut op: impl FnMut(&mut Session) -> Step<T>) -> (Result<T, String>, usize) {
    for round in 1..=20 {
        match op(s) {
            Step::Done(v) => return (Ok(v), round),
            Step::Failed(e) => return (Err(format!("{e}")), round),
            Step::Need(Needs { ranges, sources }) => {
                for (offset, length) in ranges {
                    s.fed().feed(offset, &file[offset as usize..(offset + length) as usize]);
                }
                for (i, offset, length) in sources {
                    let src = s.store().expect("source reads come after open").sources()[i].clone();
                    let o = &objects[src.url_str().unwrap()];
                    s.fed().feed_source(src, offset, o[offset as usize..(offset + length) as usize].to_vec());
                }
            }
        }
    }
    panic!("not done in 20 rounds");
}

fn opened(file: &[u8]) -> Session {
    let mut s = Session::new(file.len() as u64);
    let (r, _) = drive(&mut s, file, &objects(), |s| s.open());
    r.expect("open");
    s
}

#[test]
fn every_key_reads_as_the_blocking_reader_reads_it() {
    let file = store();
    let objs = objects();
    let direct = Store::open(file.clone()).unwrap();
    let mut s = opened(&file);
    assert_eq!(s.store().unwrap().key_count(), direct.key_count());
    let all = direct.list("").unwrap();
    assert!(all.len() > 3000);
    let mut keys: Vec<String> = all.iter().step_by(37).cloned().collect();
    keys.extend(["a/zarr.json", "a/plain", "a/deflate", "a/zstd", "a/ref", "a/concat", "a/via-key"].map(String::from));
    for key in &keys {
        let want = direct.get(key, Request::Whole, &Objects(objs.clone())).unwrap();
        let (got, _) = drive(&mut s, &file, &objs, |s| s.get(key, Request::Whole));
        assert_eq!(got.unwrap(), want, "{key}");
    }
}

#[test]
fn windows_and_missing_keys() {
    let file = store();
    let objs = objects();
    let direct = Store::open(file.clone()).unwrap();
    let mut s = opened(&file);
    for key in ["a/plain", "a/zstd", "a/ref", "a/concat"] {
        for request in [Request::Range(10, 50), Request::Offset(399_990), Request::Suffix(7), Request::Range(5, 5)] {
            let want = direct.get(key, request, &Objects(objs.clone())).unwrap();
            let (got, _) = drive(&mut s, &file, &objs, |s| s.get(key, request));
            assert_eq!(got.unwrap(), want, "{key} {request:?}");
        }
    }
    let (got, _) = drive(&mut s, &file, &objs, |s| s.get("no/such/key", Request::Whole));
    assert_eq!(got.unwrap(), None);
}

#[test]
fn list_matches() {
    let file = store();
    let objs = objects();
    let direct = Store::open(file.clone()).unwrap();
    let mut s = opened(&file);
    for prefix in ["", "a/", "many/0001", "zzz"] {
        let (got, _) = drive(&mut s, &file, &objs, |s| s.list(prefix));
        assert_eq!(got.unwrap(), direct.list(prefix).unwrap(), "{prefix:?}");
    }
}

#[test]
fn rounds_are_few() {
    let file = store();
    let objs = objects();
    let mut s = Session::new(file.len() as u64);
    let (r, rounds) = drive(&mut s, &file, &objs, |s| s.open());
    r.unwrap();
    // the tail holds a store this small whole
    assert_eq!(rounds, 2, "open: one round to read the tail, one run that finds everything");
    // a bytes value held in the record: a block of slots, then the record, then the done run
    let (_, rounds) = drive(&mut s, &file, &objs, |s| s.get("a/zarr.json", Request::Whole));
    assert!(rounds <= 3, "{rounds} rounds");
    // a reference adds the source's bytes
    let (_, rounds) = drive(&mut s, &file, &objs, |s| s.get("a/ref", Request::Whole));
    assert!(rounds <= 4, "{rounds} rounds");
    // the same key again needs nothing
    let (_, rounds) = drive(&mut s, &file, &objs, |s| s.get("a/ref", Request::Whole));
    assert_eq!(rounds, 1);
}

#[test]
fn a_store_that_does_not_open_fails_and_a_miss_is_not_a_failure() {
    // bytes that are no store: a failure, once the tail is fed
    let junk = vec![7u8; 5000];
    let mut s = Session::new(junk.len() as u64);
    let (r, _) = drive(&mut s, &junk, &objects(), |s| s.open());
    assert!(r.unwrap_err().contains("not a phkv store"));
    // before the tail is fed, the open is a need, not an error
    let mut s = Session::new(5000);
    assert!(matches!(s.open(), Step::Need(n) if !n.ranges.is_empty()));
    // a get before open is a request error
    assert!(matches!(s.get("a", Request::Whole), Step::Failed(e) if e.class == Class::Request));
}

#[test]
fn feeding_merges_ranges_and_new_bytes_win() {
    let file = store();
    let s = refkv_phkv::sansio::Fed::new(file.len() as u64);
    s.feed(100, &file[100..200]);
    s.feed(300, &file[300..400]);
    s.feed(200, &file[200..300]);
    let mut buf = vec![0u8; 300];
    refkv_phkv::ReadAt::read_at(&s, 100, &mut buf).unwrap();
    assert_eq!(buf, &file[100..400]);
    // overlapping bytes are replaced
    s.feed(150, &[0xAA; 10]);
    let mut buf = vec![0u8; 10];
    refkv_phkv::ReadAt::read_at(&s, 150, &mut buf).unwrap();
    assert_eq!(buf, [0xAA; 10]);
    // a read that crosses the end of what is fed fails
    let mut buf = vec![0u8; 10];
    assert!(refkv_phkv::ReadAt::read_at(&s, 395, &mut buf).is_err());
    let _ = NoUrls;
}
