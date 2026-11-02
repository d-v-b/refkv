//! Page-index lookup vs `__vz__/hash` lookup on the same keys, as a range-read workload.
//! `cargo run --release --example vzlookup -- 1000000 200000`
//!
//! Both lookups run against a `Remote` that serves byte ranges from memory and counts requests and
//! bytes. CPU time is measured; network time is MODELLED: each request costs rtt + bytes/bandwidth,
//! and a lookup's requests are sequential (the second depends on the first).
use refkv_phkv::ptrhash::{self, Params};
use refkv_vzip::pb::{decode_cd_index, Page, Range};
use refkv_vzip::{Layout, Options, Writer, RESERVED_PREFIX};
use std::cell::Cell;
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

const REC_READ: usize = 128;

fn main() {
    let mut args = std::env::args().skip(1);
    let n: u64 = args.next().and_then(|a| a.parse().ok()).unwrap_or(1_000_000);
    let lookups: usize = args.next().and_then(|a| a.parse().ok()).unwrap_or(200_000);
    let key = |i: u64| format!("arr/c/{}/{}/{}", i / 10000, (i / 100) % 100, i % 100);
    let fill = |w: &mut Writer<Vec<u8>>| {
        let s = w.url("data.nc").unwrap();
        for i in 0..n { w.add_ranges(&key(i), vec![Range::of(s, i * 4096, 4096)]).unwrap(); }
    };
    let mut a = Writer::new(Vec::new(), Options { mirror_refs: true, page_size: None }); fill(&mut a);
    let hashed = a.close_with_trailer("hash", &mut |l: &Layout| Ok(build_entry(l))).unwrap();

    // lookup order: a fixed pseudo-random permutation of the keys
    let order: Vec<u64> = (0..lookups as u64).map(|i| ptrhash::fmix64(i + 1) % n).collect();
    let names: Vec<String> = order.iter().map(|&i| key(i)).collect();

    struct Row { name: String, open_bytes: usize, reqs: f64, bytes: f64, cpu_us: f64 }
    let mut rows: Vec<Row> = Vec::new();

    // ---- hash side
    let (cd_off, recs) = parse_cd(&hashed);
    let hb = recs.iter().find(|r| r.0 == b"__vz__/hash").unwrap();
    let h = &hashed[hb.1..hb.1 + hb.2];
    let params = Params { seed: rd64(h, 0), parts: rd64(h, 8), slots: rd64(h, 16), buckets: rd64(h, 24) };
    let pil_len = params.total_buckets() as usize;
    let pilots = &h[32..32 + pil_len];
    let table_off = hb.1 + 32 + pil_len;
    let table_len = h.len() - 32 - pil_len;
    for cached in [false, true] {
        let r = Remote { f: &hashed, reqs: Cell::new(0), bytes: Cell::new(0) };
        let t = Instant::now();
        for name in &names {
            let k = name.as_bytes();
            let sl = ptrhash::slot(&params, pilots, ptrhash::hash(k, params.seed)) as usize;
            let off = if cached { rd32(&h[32 + pil_len..], 4 * sl) } else { rd32(r.read(table_off + 4 * sl, 4), 0) } as usize;
            assert!(scan(r.read(cd_off + off, REC_READ), k), "record fits in {REC_READ} B");
        }
        let el = t.elapsed();
        let (q, b) = r.reset();
        let l = lookups as f64;
        rows.push(Row {
            name: format!("hash, slot table {}", if cached { "cached at open" } else { "read per lookup" }),
            open_bytes: 32 + pil_len + if cached { table_len } else { 0 },
            reqs: q as f64 / l, bytes: b as f64 / l, cpu_us: el.as_secs_f64() * 1e6 / l,
        });
    }

    // ---- page side
    for page_size in [4096u64, 16384, 65536] {
        let mut a = Writer::new(Vec::new(), Options { mirror_refs: true, page_size: Some(page_size) }); fill(&mut a);
        let paged = a.close().unwrap();
        let (cd_off, recs) = parse_cd(&paged);
        let ir = recs.iter().find(|r| r.0 == b"__vz__/index").unwrap();
        let mut raw = Vec::new();
        assert_eq!(ir.3, 8);
        flate2::read::DeflateDecoder::new(&paged[ir.1..ir.1 + ir.2]).read_to_end(&mut raw).unwrap();
        let pages: Vec<Page> = decode_cd_index(&raw).unwrap().pages;
        let r = Remote { f: &paged, reqs: Cell::new(0), bytes: Cell::new(0) };
        let t = Instant::now();
        for name in &names {
            let k = name.as_bytes();
            let pi = pages.partition_point(|p| p.first_key.as_bytes() <= k).saturating_sub(1);
            let pg = &pages[pi];
            assert!(scan(r.read(cd_off + pg.offset as usize, pg.length as usize), k), "key in its page");
        }
        let el = t.elapsed();
        let (q, b) = r.reset();
        let l = lookups as f64;
        rows.push(Row { name: format!("paged, {} KiB pages", page_size / 1024), open_bytes: ir.2, reqs: q as f64 / l, bytes: b as f64 / l, cpu_us: el.as_secs_f64() * 1e6 / l });
    }

    println!("n={n} keys, {lookups} random lookups (CPU measured; network modelled)\n");
    println!("{:<34} {:>11} {:>5} {:>9} {:>8}", "", "open bytes", "reqs", "B/lookup", "CPU µs");
    for r in &rows {
        println!("{:<34} {:>11} {:>5.2} {:>9.0} {:>8.2}", r.name, r.open_bytes, r.reqs, r.bytes, r.cpu_us);
    }
    for (rtt_ms, bw_mb, label) in [(0.5, 1000.0, "same-region, fast (0.5 ms, 1 GB/s)"), (10.0, 100.0, "object store (10 ms, 100 MB/s)"), (50.0, 100.0, "far/slow (50 ms, 100 MB/s)"), (10.0, 10.0, "object store, thin link (10 ms, 10 MB/s)")] {
        println!("\n{label}: ms per lookup");
        for r in &rows {
            let ms = r.cpu_us / 1e3 + r.reqs * rtt_ms + r.bytes / (bw_mb * 1e3);
            println!("  {:<34} {:>8.3}", r.name, ms);
        }
    }
}
