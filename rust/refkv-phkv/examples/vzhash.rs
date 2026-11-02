//! Prototype: a hash index entry `__vz__/hash` for a vzip archive, in place of the page index.
//! `cargo run --release --example vzhash -- 100000`
//!
//! Entry body: seed, parts, slots, buckets (u64 each), pilots (u8 per bucket), then a slot table of
//! u32 central-directory-relative record offsets (u32::MAX = empty slot). The central directory
//! stays sorted, so the non-hidden records come first and list(prefix) still works by bisection.
use refkv_phkv::ptrhash::{self, Params};
use refkv_vzip::pb::Range;
use refkv_vzip::{Layout, Options, Writer, RESERVED_PREFIX};
use std::time::Instant;

fn rd16(b: &[u8], o: usize) -> usize { u16::from_le_bytes(b[o..o + 2].try_into().unwrap()) as usize }
fn rd32(b: &[u8], o: usize) -> u32 { u32::from_le_bytes(b[o..o + 4].try_into().unwrap()) }
fn rd64(b: &[u8], o: usize) -> u64 { u64::from_le_bytes(b[o..o + 8].try_into().unwrap()) }

/// size of a central directory record as the writer emits it (no ZIP64 block below 4 GiB)
fn cd_record_len(name: &str, payload: &Option<(u16, Vec<u8>)>) -> u32 {
    46 + name.len() as u32 + payload.as_ref().map_or(0, |(_, p)| 4 + p.len() as u32)
}

fn build_entry(layout: &Layout) -> Vec<u8> {
    // the central directory is sorted by name bytes, hidden records included (`__vz__/` sorts first),
    // and the trailer's own record (no extra field) is among them
    let mut all: Vec<(&str, u32)> = layout.entries.iter().map(|e| (e.name.as_str(), cd_record_len(&e.name, &e.payload))).collect();
    all.push(("__vz__/hash", cd_record_len("__vz__/hash", &None)));
    // the writer sorts the body entries (the trailer among them) and puts the format entries last
    let (mut fmt, mut all): (Vec<_>, Vec<_>) = all.into_iter().partition(|(n, _)| *n == refkv_vzip::SOURCES_KEY || *n == refkv_vzip::INDEX_KEY);
    all.sort_by(|a, b| a.0.as_bytes().cmp(b.0.as_bytes()));
    all.append(&mut fmt);
    let (mut vis, mut offs, mut o) = (Vec::new(), Vec::new(), 0u32);
    for (name, len) in &all {
        if !name.starts_with(RESERVED_PREFIX) {
            vis.push(*name);
            offs.push(o);
        }
        o += len;
    }
    let keys: Vec<&[u8]> = vis.iter().map(|n| n.as_bytes()).collect();
    let b = ptrhash::build(&keys).expect("hash");
    let p = b.params;
    let mut out = Vec::new();
    for v in [p.seed, p.parts, p.slots, p.buckets] { out.extend_from_slice(&v.to_le_bytes()); }
    out.extend_from_slice(&b.pilots);
    for s in &b.slots {
        out.extend_from_slice(&s.map_or(u32::MAX, |i| offs[i as usize]).to_le_bytes());
    }
    out
}

fn main() {
    let n: u64 = std::env::args().nth(1).and_then(|a| a.parse().ok()).unwrap_or(100_000);
    let key = |i: u64| format!("arr/c/{}/{}/{}", i / 10000, (i / 100) % 100, i % 100);
    let fill = |w: &mut Writer<Vec<u8>>| {
        let s = w.url("data.nc").unwrap();
        for i in 0..n { w.add_ranges(&key(i), vec![Range::of(s, i * 4096, 4096)]).unwrap(); }
    };
    let opts = |page| Options { mirror_refs: true, page_size: page };
    let mut a = Writer::new(Vec::new(), opts(Some(65536))); fill(&mut a);
    let paged = a.close().unwrap();
    let mut a = Writer::new(Vec::new(), opts(None)); fill(&mut a);
    let plain = a.close().unwrap();
    let mut a = Writer::new(Vec::new(), opts(None)); fill(&mut a);
    let t = Instant::now();
    let mut build = |l: &Layout| Ok(build_entry(l));
    let hashed = a.close_with_trailer("hash", &mut build).unwrap();
    let build_t = t.elapsed();

    // read the hash entry back through the central directory and check every key
    let f = &hashed;
    let eocd = f.len() - 60; // 38-byte comment? no page index => 22-byte comment
    let eocd = if rd32(f, f.len() - 44) == 0x06054b50 { f.len() - 44 } else { eocd };
    let z64 = rd64(f, eocd - 20 + 8) as usize;
    let (cd_size, cd_off) = (rd64(f, z64 + 40) as usize, rd64(f, z64 + 48) as usize);
    let cd = &f[cd_off..cd_off + cd_size];
    // walk the CD; find the hash entry and a name->offset map to check against
    let (mut pos, mut hash_body, mut recs) = (0usize, None, Vec::new());
    while pos < cd.len() {
        assert_eq!(rd32(cd, pos), 0x02014b50);
        let (nl, el) = (rd16(cd, pos + 28), rd16(cd, pos + 30));
        let name = &cd[pos + 46..pos + 46 + nl];
        if name == b"__vz__/hash" {
            let (lho, csize) = (rd32(cd, pos + 42) as usize, rd32(cd, pos + 20) as usize);
            hash_body = Some(&f[lho + 30 + nl..lho + 30 + nl + csize]);
        }
        recs.push((pos, name.to_vec()));
        pos += 46 + nl + el;
    }
    let h = hash_body.expect("hash entry");
    let params = Params { seed: rd64(h, 0), parts: rd64(h, 8), slots: rd64(h, 16), buckets: rd64(h, 24) };
    let pilots = &h[32..32 + params.total_buckets() as usize];
    let table = &h[32 + pilots.len()..];
    let (mut ok, mut bytes) = (0u64, 0u64);
    let t = Instant::now();
    for (rpos, name) in recs.iter().filter(|(_, n)| !n.starts_with(RESERVED_PREFIX.as_bytes())) {
        let s = ptrhash::slot(&params, pilots, ptrhash::hash(name, params.seed)) as usize;
        let off = rd32(table, 4 * s) as usize;
        assert_eq!(off, *rpos, "slot offset");
        let (nl, el) = (rd16(cd, off + 28), rd16(cd, off + 30));
        assert_eq!(&cd[off + 46..off + 46 + nl], name.as_slice());
        bytes += 4 + (46 + nl + el) as u64;
        ok += 1;
    }
    let look = t.elapsed();
    println!("verified {ok} keys; build {build_t:.2?}, in-memory lookup {:.0} ns/key", look.as_nanos() as f64 / ok as f64);
    let pad = |v: usize| v as f64 / n as f64;
    println!("size B/key: vzip paged {:.2} | vzip no index {:.2} | vzip + hash {:.2}", pad(paged.len()), pad(plain.len()), pad(hashed.len()));
    println!("hash entry: {:.2} B/key = pilots {:.2} + slots {:.2} (slot overhead {:.1}%)", pad(h.len()), pad(pilots.len()), pad(table.len()), 100.0 * (params.total_slots() as f64 / n as f64 - 1.0));
    println!("bytes read per lookup (excl. cached pilots): hash {:.0} (4 slot + record) vs page index ~65536", bytes as f64 / ok as f64);
}
