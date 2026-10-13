//! The store run (src/store.rs): a store parser's batches of object reads, as requests (one
//! per object, its ranges merged into spans), the bytes fed back, the held objects' bytes
//! kept; one test of a run, and one per error.

use serde_json::{json, Value as J};
use vzip_ir::ir::{Ir, Res, NO_INDEX};
use vzip_ir::store::{ObjectRequest, StoreFormat, StoreParser, StoreRun, StoreStep};

/// A parser that asks for `batches` in turn, then holds the objects `hold` (one value each).
struct Asks {
    ir: Option<Ir>,
    objects: Vec<u32>,
    batches: Vec<Vec<(usize, u64, u64)>>,
    hold: Vec<usize>,
    fed: Vec<(usize, u64, Vec<u8>)>,
    folders: Vec<String>,
}

impl StoreParser for Asks {
    fn step(&mut self) -> Res<StoreStep> {
        Ok(if self.batches.is_empty() { StoreStep::Done } else { StoreStep::Read(self.batches.remove(0)) })
    }
    fn feed(&mut self, object: usize, offset: u64, data: &[u8]) {
        self.fed.push((object, offset, data.to_vec()));
    }
    fn folders(&mut self, folders: &[String]) {
        self.folders = folders.to_vec();
    }
    fn finish(self: Box<Self>) -> Res<(Ir, J)> {
        let mut ir = self.ir.unwrap();
        for &k in &self.hold {
            let o = self.objects[k];
            let n = ir.len[o as usize];
            ir.value(o, "v", NO_INDEX, &format!("bytes[{n}]"), 0, (0, n), None)?;
        }
        vzip_ir::check::finish(&mut ir)?;
        Ok((ir, json!({"fed": self.fed.iter().map(|f| json!([f.0, f.1, f.2])).collect::<Vec<_>>(), "folders": self.folders})))
    }
}

fn keys() -> Vec<(String, u64)> {
    vec![("a/.zarray".into(), 10), ("a/0".into(), 100_000), ("b".into(), 3)]
}

fn bytes(k: usize) -> Vec<u8> {
    (0..keys()[k].1).map(|i| (i % 251) as u8 + k as u8).collect()
}

fn run(batches: Vec<Vec<(usize, u64, u64)>>, hold: Vec<usize>) -> Res<StoreRun> {
    StoreRun::with(StoreFormat::Zarr2, keys(), 4, &|ir, objects, _| {
        Ok(Box::new(Asks { ir: Some(ir), objects, batches: batches.clone(), hold: hold.clone(), fed: Vec::new(), folders: Vec::new() }))
    })
}

fn drive(r: &mut StoreRun) -> Res<Vec<Vec<ObjectRequest>>> {
    let mut seen = Vec::new();
    while let Some(reqs) = r.poll()? {
        for (k, q) in reqs.iter().enumerate() {
            let b = bytes(q.object);
            r.complete(k, q.spans.iter().map(|&(a, e)| b[a as usize..e as usize].to_vec()).collect())?;
        }
        seen.push(reqs);
    }
    Ok(seen)
}

#[test]
fn a_store_run_reads_each_batch_as_one_request_per_object() {
    // a batch of two whole documents, then ranges of one object: near ones merged, far ones apart
    let mut r = run(vec![vec![(0, 0, 10), (2, 0, 3)], vec![(1, 0, 16), (1, 100, 16), (1, 90_000, 8)]], vec![0, 2]).unwrap();
    let seen = drive(&mut r).unwrap();
    assert_eq!(seen, vec![
        vec![ObjectRequest { object: 0, key: "a/.zarray".into(), spans: vec![(0, 10)] },
             ObjectRequest { object: 2, key: "b".into(), spans: vec![(0, 3)] }],
        vec![ObjectRequest { object: 1, key: "a/0".into(), spans: vec![(0, 116), (90_000, 90_008)] }],
    ]);
    let (ir, facts, stats) = r.finish().unwrap();
    assert_eq!(stats, json!({"objects": 3, "batches": 2, "ranges": 5, "asked": 53, "requests": 3, "spans": 4,
                             "bytes": 137, "listed": 3}));
    let fed = facts["fed"].as_array().unwrap();
    assert_eq!(fed.len(), 5);
    assert_eq!(fed[3], json!([1, 100, bytes(1)[100..116]]));
    // the objects read whole and given children hold their bytes
    let objs = ir.sources().objects;
    let held: Vec<(String, Vec<u8>)> = objs.iter().filter_map(|&o| ir.value_bytes(o).map(|b| (ir.key(o), b.to_vec()))).collect();
    assert_eq!(held, vec![("a/.zarray".to_string(), bytes(0)), ("b".to_string(), bytes(2))]);
}

#[test]
fn a_batch_of_empty_ranges_alone_is_fed_without_a_request() {
    let mut r = run(vec![vec![(2, 0, 0), (0, 3, 0)], vec![(2, 0, 3)]], vec![]).unwrap();
    let seen = drive(&mut r).unwrap();
    assert_eq!(seen.iter().map(|b| b.len()).collect::<Vec<_>>(), vec![0, 1]);
    let (_, facts, _) = r.finish().unwrap();
    assert_eq!(facts["fed"], json!([[2, 0, []], [0, 3, []], [2, 0, bytes(2)]]));
}

#[test]
fn a_batch_of_no_reads_is_refused() {
    let mut r = run(vec![vec![]], vec![]).unwrap();
    assert_eq!(r.poll().unwrap_err(), "internal: a batch of no reads");
}

#[test]
fn a_read_outside_its_object_is_refused() {
    let mut r = run(vec![vec![(2, 1, 3)]], vec![]).unwrap();
    assert_eq!(r.poll().unwrap_err(), "b: a read of [1, 4) outside its 3 bytes");
}

#[test]
fn a_read_of_no_listed_object_is_refused() {
    let mut r = run(vec![vec![(3, 0, 1)]], vec![]).unwrap();
    assert_eq!(r.poll().unwrap_err(), "internal: a read of object 3, which the listing has not");
}

#[test]
fn bytes_other_than_the_spans_asked_are_refused() {
    let mut r = run(vec![vec![(0, 0, 10)]], vec![]).unwrap();
    r.poll().unwrap();
    assert_eq!(r.complete(0, vec![vec![0; 9]]).unwrap_err(), "a/.zarray: the bytes given are not those of the spans asked");
}

#[test]
fn the_ignored_keys_reach_the_parser_and_the_facts() {
    // the folders, before the parser's first step; the recorded keys, in the order of their
    // UTF-8 bytes, in the facts the projection records them from
    let mut r = run(vec![vec![(2, 0, 3)]], vec![]).unwrap();
    r.ignore(vec!["z//".into(), "\u{e9}/.".into(), "a/../b".into()], vec!["d/".into()]).unwrap();
    drive(&mut r).unwrap();
    let (_, facts, _) = r.finish().unwrap();
    assert_eq!(facts["folders"], json!(["d/"]));
    assert_eq!(facts["ignored"], json!(["a/../b", "z//", "\u{e9}/."]));
}

#[test]
fn ignored_keys_after_the_first_poll_are_refused() {
    let mut r = run(vec![vec![(2, 0, 3)]], vec![]).unwrap();
    r.poll().unwrap();
    assert!(r.ignore(vec!["a//b".into()], vec![]).unwrap_err().contains("after the first poll"));
}

#[test]
fn a_poll_before_the_batch_completed_is_refused() {
    let mut r = run(vec![vec![(0, 0, 10)]], vec![]).unwrap();
    r.poll().unwrap();
    assert_eq!(r.poll().unwrap_err(), "poll before every request of the batch completed");
}

#[test]
fn a_held_object_not_read_whole_is_refused() {
    let mut r = run(vec![vec![(0, 0, 5)]], vec![0]).unwrap();
    drive(&mut r).unwrap();
    assert_eq!(r.finish().err().unwrap(), "internal: the held object \"a/.zarray\" was not read whole");
}

#[test]
fn a_listing_the_ir_cannot_hold_is_refused() {
    let e = StoreRun::new(StoreFormat::Zarr2, vec![("a/./b".into(), 1)], 4).err().unwrap();
    assert_eq!(e, "key \"a/./b\" has an empty, . or .. segment");
}
