//! Page-index lookup vs `__vz__/hash` lookup on the same keys, as a range-read workload.
//! `cargo run --release --example vzlookup -- 1000000 200000`
//!
//! Both lookups run against a `Remote` that serves byte ranges from memory and counts requests and
//! bytes. CPU time is measured; network time is MODELLED: each request costs rtt + bytes/bandwidth,
//! and a lookup's requests are sequential (the second depends on the first).
use refkv_phkv::ptrhash::{self, Params};
use refkv_vzip::pb::{decode_cd_index, Page, Range};
use refkv_vzip::{Layout, Options, Writer, RESERVED_PREFIX};
use std::cell::{Cell, RefCell};
use std::collections::{BTreeMap, HashMap};
use std::io::Read;
use std::time::Instant;

fn rd16(b: &[u8], o: usize) -> usize { u16::from_le_bytes(b[o..o + 2].try_into().unwrap()) as usize }
fn rd32(b: &[u8], o: usize) -> u32 { u32::from_le_bytes(b[o..o + 4].try_into().unwrap()) }
fn rd64(b: &[u8], o: usize) -> u64 { u64::from_le_bytes(b[o..o + 8].try_into().unwrap()) }

struct Remote<'a> { f: &'a [u8], reqs: Cell<u64>, bytes: Cell<u64> }
impl Remote<'_> {
    fn read(&self, off: usize, len: usize) -> &[u8] {
        let end = (off + len).min(self.f.len());
        self.reqs.set(self.reqs.get() + 1);
        self.bytes.set(self.bytes.get() + (end - off) as u64);
        &self.f[off..end]
    }
    fn reset(&self) -> (u64, u64) { let r = (self.reqs.replace(0), self.bytes.replace(0)); r }
}

fn cd_record_len(name: &str, payload: &Option<(u16, Vec<u8>)>) -> u32 {
    46 + name.len() as u32 + payload.as_ref().map_or(0, |(_, p)| 4 + p.len() as u32)
}

fn build_entry(layout: &Layout) -> Vec<u8> {
    let mut all: Vec<(&str, u32)> = layout.entries.iter().map(|e| (e.name.as_str(), cd_record_len(&e.name, &e.payload))).collect();
    all.push(("__vz__/hash", cd_record_len("__vz__/hash", &None)));
    let (mut fmt, mut all): (Vec<_>, Vec<_>) = all.into_iter().partition(|(n, _)| *n == refkv_vzip::SOURCES_KEY || *n == refkv_vzip::INDEX_KEY);
    all.sort_by(|a, b| a.0.as_bytes().cmp(b.0.as_bytes()));
    all.append(&mut fmt);
    let (mut keys, mut offs, mut o) = (Vec::new(), Vec::new(), 0u32);
    for (name, len) in &all {
        if !name.starts_with(RESERVED_PREFIX) { keys.push(name.as_bytes()); offs.push(o); }
        o += len;
    }
    let b = ptrhash::build(&keys).expect("hash");
    let p = b.params;
    let mut out = Vec::new();
    for v in [p.seed, p.parts, p.slots, p.buckets] { out.extend_from_slice(&v.to_le_bytes()); }
    out.extend_from_slice(&b.pilots);
    for s in &b.slots { out.extend_from_slice(&s.map_or(u32::MAX, |i| offs[i as usize]).to_le_bytes()); }
    out
}

/// (central directory offset, size, [(name, body offset, compressed size, method)])
fn parse_cd(f: &[u8]) -> (usize, Vec<(Vec<u8>, usize, usize, u16)>) {
    let eocd = if rd32(f, f.len() - 44) == 0x06054b50 { f.len() - 44 } else { f.len() - 60 };
    let z64 = rd64(f, eocd - 20 + 8) as usize;
    let (cd_size, cd_off) = (rd64(f, z64 + 40) as usize, rd64(f, z64 + 48) as usize);
    let (mut pos, mut v) = (0, Vec::new());
    while pos < cd_size {
        let r = &f[cd_off + pos..];
        let (nl, el) = (rd16(r, 28), rd16(r, 30));
        let lho = rd32(r, 42) as usize;
        v.push((r[46..46 + nl].to_vec(), lho + 30 + nl, rd32(r, 20) as usize, rd16(r, 10) as u16));
        pos += 46 + nl + el;
    }
    (cd_off, v)
}

/// scans a run of central directory records for `key`; the record's start on a hit
fn scan(page: &[u8], key: &[u8]) -> bool {
    let mut pos = 0;
    while pos + 46 <= page.len() {
        let (nl, el) = (rd16(page, pos + 28), rd16(page, pos + 30));
        if page.len() < pos + 46 + nl { return false; }
        if &page[pos + 46..pos + 46 + nl] == key { return true; }
        pos += 46 + nl + el;
    }
    false
}

/// An LRU cache of byte ranges read, by exact (offset, length), bounded in bytes.
struct Cache { cap: u64, used: u64, tick: u64, map: HashMap<(usize, usize), u64>, order: BTreeMap<u64, (usize, usize)> }
impl Cache {
    fn new(cap: u64) -> Cache { Cache { cap, used: 0, tick: 0, map: HashMap::new(), order: BTreeMap::new() } }
    /// true on a hit; a miss is recorded (and evicts) so the next read of it hits
    fn touch(&mut self, k: (usize, usize)) -> bool {
        self.tick += 1;
        if let Some(t) = self.map.insert(k, self.tick) {
            self.order.remove(&t);
            self.order.insert(self.tick, k);
            return true;
        }
        self.order.insert(self.tick, k);
        self.used += k.1 as u64;
        while self.used > self.cap {
            let (&t, &old) = self.order.iter().next().unwrap();
            self.order.remove(&t);
            self.map.remove(&old);
            self.used -= old.1 as u64;
        }
        false
    }
}

impl Remote<'_> {
    /// a read through the cache: a hit costs no request
    fn cached(&self, c: &RefCell<Cache>, off: usize, len: usize) -> &[u8] {
        let end = (off + len).min(self.f.len());
        if c.borrow_mut().touch((off, len)) { &self.f[off..end] } else { self.read(off, len) }
    }
}

const REC_READ: usize = 128;
const BLOCK: usize = 4096;
const CACHE_BYTES: u64 = 64 << 20;

fn main() {
    let mut args = std::env::args().skip(1);
    let n: u64 = args.next().and_then(|a| a.parse().ok()).unwrap_or(1_000_000);
    let lookups: usize = args.next().and_then(|a| a.parse().ok()).unwrap_or(200_000);
    let key = |i: u64| format!("arr/c/{}/{}/{}", i / 10000, (i / 100) % 100, i % 100);
    let fill = |w: &mut Writer<Vec<u8>>| {
        let s = w.url("data.nc").unwrap();
        for i in 0..n { w.add_ranges(&key(i), vec![Range::of(s, i * 4096, 4096)]).unwrap(); }
    };

    // ---- workloads: random keys, and clustered keys (a random run of 100 neighbours, in order)
    let l = lookups;
    let random: Vec<u64> = (0..l as u64).map(|i| ptrhash::fmix64(i + 1) % n).collect();
    let clustered: Vec<u64> = (0..(l / 100) as u64).flat_map(|b| { let s = ptrhash::fmix64(b + 7) % (n / 100) * 100; s..s + 100 }).collect();
    let workloads = [("random", random), ("clustered (runs of 100)", clustered)];

    struct Row { name: String, open_bytes: usize, reqs: Vec<f64>, bytes: Vec<f64>, cpu_us: Vec<f64> }
    let mut rows: Vec<Row> = Vec::new();
    let nw = workloads.len();
    let names: Vec<Vec<String>> = workloads.iter().map(|(_, w)| w.iter().map(|&i| key(i)).collect()).collect();

    // ---- hash side: slot table in memory or read per lookup; records read as 128 B or as 4 KiB blocks
    let hashed = {
        let mut a = Writer::new(Vec::new(), Options { mirror_refs: true, page_size: None }); fill(&mut a);
        a.close_with_trailer("hash", &mut |l: &Layout| Ok(build_entry(l))).unwrap()
    };
    let (cd_off, recs) = parse_cd(&hashed);
    let hb = recs.iter().find(|r| r.0 == b"__vz__/hash").unwrap();
    let h = &hashed[hb.1..hb.1 + hb.2];
    let params = Params { seed: rd64(h, 0), parts: rd64(h, 8), slots: rd64(h, 16), buckets: rd64(h, 24) };
    let pil_len = params.total_buckets() as usize;
    let pilots = &h[32..32 + pil_len];
    let table_off = hb.1 + 32 + pil_len;
    let table_len = h.len() - 32 - pil_len;
    for (cached_slots, blocks) in [(false, false), (true, false), (true, true)] {
        let mut row = Row {
            name: format!("hash, slots {}, {}", if cached_slots { "in memory" } else { "per lookup" }, if blocks { "4 KiB record blocks cached" } else { "128 B records" }),
            open_bytes: 32 + pil_len + if cached_slots { table_len } else { 0 }, reqs: vec![], bytes: vec![], cpu_us: vec![],
        };
        for names in &names {
            let r = Remote { f: &hashed, reqs: Cell::new(0), bytes: Cell::new(0) };
            let cache = RefCell::new(Cache::new(CACHE_BYTES));
            let t = Instant::now();
            for name in names {
                let k = name.as_bytes();
                let sl = ptrhash::slot(&params, pilots, ptrhash::hash(k, params.seed)) as usize;
                let off = if cached_slots { rd32(&h[32 + pil_len..], 4 * sl) } else { rd32(r.read(table_off + 4 * sl, 4), 0) } as usize;
                let at = cd_off + off;
                let ok = if blocks {
                    let b = at & !(BLOCK - 1);
                    let first = scan(&r.cached(&cache, b, BLOCK)[at - b..], k);
                    // a record that crosses the block's end continues in the next one
                    first || { let nb = r.cached(&cache, b + BLOCK, BLOCK); let mut v = r.f[at..b + BLOCK].to_vec(); v.extend_from_slice(&nb[..REC_READ.min(nb.len())]); scan(&v, k) }
                } else {
                    scan(r.read(at, REC_READ), k)
                };
                assert!(ok, "record found");
            }
            let el = t.elapsed();
            let (q, b) = r.reset();
            let c = names.len() as f64;
            row.reqs.push(q as f64 / c); row.bytes.push(b as f64 / c); row.cpu_us.push(el.as_secs_f64() * 1e6 / c);
        }
        rows.push(row);
    }
    drop(hashed);

    // ---- page side: pages cached by (offset, length)
    for page_size in [4096u64, 16384, 65536] {
        let mut a = Writer::new(Vec::new(), Options { mirror_refs: true, page_size: Some(page_size) }); fill(&mut a);
        let paged = a.close().unwrap();
        let (cd_off, recs) = parse_cd(&paged);
        let ir = recs.iter().find(|r| r.0 == b"__vz__/index").unwrap();
        let mut raw = Vec::new();
        assert_eq!(ir.3, 8);
        flate2::read::DeflateDecoder::new(&paged[ir.1..ir.1 + ir.2]).read_to_end(&mut raw).unwrap();
        let pages: Vec<Page> = decode_cd_index(&raw).unwrap().pages;
        let mut row = Row { name: format!("paged, {} KiB pages, cached", page_size / 1024), open_bytes: ir.2, reqs: vec![], bytes: vec![], cpu_us: vec![] };
        for names in &names {
            let r = Remote { f: &paged, reqs: Cell::new(0), bytes: Cell::new(0) };
            let cache = RefCell::new(Cache::new(CACHE_BYTES));
            let t = Instant::now();
            for name in names {
                let k = name.as_bytes();
                let pi = pages.partition_point(|p| p.first_key.as_bytes() <= k).saturating_sub(1);
                let pg = &pages[pi];
                assert!(scan(r.cached(&cache, cd_off + pg.offset as usize, pg.length as usize), k), "key in its page");
            }
            let el = t.elapsed();
            let (q, b) = r.reset();
            let c = names.len() as f64;
            row.reqs.push(q as f64 / c); row.bytes.push(b as f64 / c); row.cpu_us.push(el.as_secs_f64() * 1e6 / c);
        }
        rows.push(row);
    }

    println!("n={n} keys, {} lookups per workload, {} MiB cache (CPU measured; network modelled)", lookups, CACHE_BYTES >> 20);
    for (w, (wname, _)) in workloads.iter().enumerate() {
        println!("\n== {wname}");
        println!("{:<52} {:>11} {:>6} {:>9} {:>8}", "", "open bytes", "reqs", "B/lookup", "CPU µs");
        for r in &rows {
            println!("{:<52} {:>11} {:>6.3} {:>9.0} {:>8.2}", r.name, r.open_bytes, r.reqs[w], r.bytes[w], r.cpu_us[w]);
        }
        for (rtt_ms, bw_mb, label) in [(0.5, 1000.0, "0.5 ms, 1 GB/s"), (10.0, 100.0, "10 ms, 100 MB/s"), (10.0, 10.0, "10 ms, 10 MB/s")] {
            println!("  ms per lookup at {label}:");
            for r in &rows {
                println!("    {:<50} {:>8.3}", r.name, r.cpu_us[w] / 1e3 + r.reqs[w] * rtt_ms + r.bytes[w] / (bw_mb * 1e3));
            }
        }
    }
    let _ = nw;
}
