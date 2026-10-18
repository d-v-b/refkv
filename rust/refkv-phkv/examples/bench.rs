//! The benchmark's Rust half (bench/README.md): it writes the test files and times the phkv
//! reader on them, over a local file and over HTTP, counting the reads it makes.
//!
//! ```text
//! bench build <dir> <n> <refs|bytes> <value_bytes>                  writes n.phkv, n.hybrid, n.vzip, n.paged.vzip
//! bench build <dir> <n> <refs|bytes> <value_bytes> <tag> <page_size> <inline_max>
//!                                                                    writes only n.<tag>.phkv and n.<tag>.hybrid
//! bench read <path-or-url> <refs|bytes> <n> <reps> [<tail> <record_read>]
//!                                                                    times the reader, prints one JSON object
//! ```
use refkv_phkv::{HybridWriter, Limits, NoUrls, Options, Request, Store, Writer, DEFAULT_TAIL};
use refkv_vzip::pb::Range;
use std::cell::OnceCell;
use std::fs::File;
use std::io::{self, BufWriter};
use std::sync::atomic::{AtomicU64, Ordering::Relaxed};
use std::time::Instant;

const CHUNK: u64 = 4096;

/// `arr/c/a/b/c`: the chunk keys of a 3-D array, 100 keys under each `arr/c/a/b/`.
fn key(i: u64) -> String {
    format!("arr/c/{}/{}/{}", i / 10000, (i / 100) % 100, i % 100)
}

/// A value of `n` bytes that does not compress (a counter run through a mixing function).
fn value(i: u64, n: usize) -> Vec<u8> {
    let mut x = i.wrapping_mul(0x9E37_79B9_7F4A_7C15) | 1;
    (0..n)
        .map(|_| {
            x ^= x << 13;
            x ^= x >> 7;
            x ^= x << 17;
            (x >> 24) as u8
        })
        .collect()
}

fn build(dir: &str, n: u64, workload: &str, vsize: usize, variant: Option<(&str, u64, u64)>) {
    std::fs::create_dir_all(dir).unwrap();
    let tag = variant.map_or(String::new(), |v| format!("{}.", v.0));
    let out = |name: &str| BufWriter::with_capacity(1 << 20, File::create(format!("{dir}/{n}.{tag}{name}")).unwrap());
    let opts = variant.map_or(Options::default(), |(_, page_size, inline_max)| Options { page_size, inline_max, ..Options::default() });
    let refs = workload == "refs";
    let zopts = |page_size| refkv_vzip::Options { mirror_refs: true, page_size };
    let mut vzips: Vec<_> = [(out("vzip"), None), (out("paged.vzip"), Some(4096))].into_iter().filter(|_| variant.is_none()).map(|(w, ps)| {
        let mut z = refkv_vzip::Writer::new(w, zopts(ps));
        let s = z.url("data.nc").unwrap();
        (z, s)
    }).collect();
    let mut p = Writer::new(out("phkv"), opts);
    let ps = p.url("data.nc").unwrap();
    let mut h = HybridWriter::new(out("hybrid"), zopts(Some(4096)), opts).unwrap();
    let hs = h.url("data.nc").unwrap();
    for i in 0..n {
        let k = key(i);
        if refs {
            let r = |s| Range::of(s, i * CHUNK, CHUNK);
            for (z, s) in &mut vzips {
                z.add_ranges(&k, vec![r(*s)]).unwrap();
            }
            p.add_ranges(&k, &[r(ps)]).unwrap();
            h.add_ranges(&k, vec![r(hs)]).unwrap();
        } else {
            let v = value(i, vsize);
            for (z, _) in &mut vzips {
                z.add_bytes(&k, &v, false, false).unwrap();
            }
            p.add_bytes(&k, &v).unwrap();
            h.add_bytes(&k, &v, false, false).unwrap();
        }
    }
    for (z, _) in vzips {
        z.close().unwrap();
    }
    p.close().unwrap();
    h.close().unwrap();
}

/// Reads counted: the number of reads and the bytes they returned.
#[derive(Default)]
struct Counts {
    reads: AtomicU64,
    bytes: AtomicU64,
    /// each read's offset and length, in order
    log: std::sync::Mutex<Vec<(u64, u64)>>,
}

impl Counts {
    fn get(&self) -> (u64, u64) {
        (self.reads.load(Relaxed), self.bytes.load(Relaxed))
    }
}

/// A local file whose reads are counted.
struct Local<'a>(File, &'a Counts);

impl refkv_phkv::ReadAt for Local<'_> {
    fn size(&self) -> io::Result<u64> {
        self.0.size()
    }

    fn read_at(&self, offset: u64, buf: &mut [u8]) -> io::Result<()> {
        self.1.reads.fetch_add(1, Relaxed);
        self.1.bytes.fetch_add(buf.len() as u64, Relaxed);
        self.1.log.lock().unwrap().push((offset, buf.len() as u64));
        self.0.read_at(offset, buf)
    }
}

/// An object over HTTP, one `Range` request per read. `size` is one request, for the object's
/// last [`TAIL`] bytes, whose `Content-Range` gives the size; reads inside those bytes are
/// served from them, as the reader's own first read would be.
struct Http<'a> {
    agent: ureq::Agent,
    url: &'a str,
    counts: &'a Counts,
    tail: OnceCell<(u64, Vec<u8>)>,
    /// the bytes of the end of the object the first request asks for
    prime: u64,
}

impl Http<'_> {
    fn fetch(&self, range: String) -> io::Result<(Vec<u8>, Option<u64>)> {
        let other = |e: ureq::Error| io::Error::other(e.to_string());
        let mut resp = self.agent.get(self.url).header("Range", format!("bytes={range}")).call().map_err(other)?;
        let total = resp.headers().get("content-range").and_then(|v| v.to_str().ok()).and_then(|v| v.rsplit('/').next()?.parse().ok());
        let body = resp.body_mut().with_config().limit(u64::MAX).read_to_vec().map_err(other)?;
        self.counts.reads.fetch_add(1, Relaxed);
        self.counts.bytes.fetch_add(body.len() as u64, Relaxed);
        Ok((body, total))
    }
}

impl refkv_phkv::ReadAt for Http<'_> {
    fn size(&self) -> io::Result<u64> {
        if let Some((n, _)) = self.tail.get() {
            return Ok(*n);
        }
        let (body, total) = self.fetch(format!("-{}", self.prime))?;
        let n = total.ok_or_else(|| io::Error::other("no Content-Range"))?;
        self.counts.log.lock().unwrap().push((n - body.len() as u64, body.len() as u64));
        Ok(self.tail.get_or_init(|| (n, body)).0)
    }

    fn read_at(&self, offset: u64, buf: &mut [u8]) -> io::Result<()> {
        if let Some((n, t)) = self.tail.get() {
            let at = n - t.len() as u64;
            if offset >= at {
                let s = (offset - at) as usize;
                buf.copy_from_slice(&t[s..s + buf.len()]);
                return Ok(());
            }
        }
        let (body, _) = self.fetch(format!("{}-{}", offset, offset + buf.len() as u64 - 1))?;
        self.counts.log.lock().unwrap().push((offset, buf.len() as u64));
        buf.copy_from_slice(&body);
        Ok(())
    }
}

/// A transport the store reads: it is made again for each cold open.
trait Open {
    fn counts(&self) -> &Counts;
    fn open(&self) -> Store<Dyn<'_>>;
}

struct LocalOpen(String, Counts, Limits);
struct HttpOpen(String, Counts, Limits);

/// A transport behind a pointer.
struct Dyn<'a>(Box<dyn refkv_phkv::ReadAt + 'a>);

impl refkv_phkv::ReadAt for Dyn<'_> {
    fn size(&self) -> io::Result<u64> {
        self.0.size()
    }

    fn read_at(&self, offset: u64, buf: &mut [u8]) -> io::Result<()> {
        self.0.read_at(offset, buf)
    }
}

impl Open for LocalOpen {
    fn counts(&self) -> &Counts {
        &self.1
    }

    fn open(&self) -> Store<Dyn<'_>> {
        Store::open_with(Dyn(Box::new(Local(File::open(&self.0).unwrap(), &self.1))), self.2).unwrap()
    }
}

impl Open for HttpOpen {
    fn counts(&self) -> &Counts {
        &self.1
    }

    fn open(&self) -> Store<Dyn<'_>> {
        let agent: ureq::Agent = ureq::Agent::config_builder().http_status_as_error(true).proxy(None).build().into();
        Store::open_with(Dyn(Box::new(Http { agent, url: &self.0, counts: &self.1, tail: OnceCell::new(), prime: self.2.tail })), self.2).unwrap()
    }
}

fn lookup(s: &Store<Dyn<'_>>, refs: bool, k: &str) {
    let v = if refs { s.raw(k).unwrap() } else { s.get(k, Request::Whole, &NoUrls).unwrap() };
    assert!(v.is_some(), "{k} is missing");
}

fn ms(t: Instant) -> f64 {
    t.elapsed().as_secs_f64() * 1e3
}

fn median(v: &mut [f64]) -> f64 {
    v.sort_by(|a, b| a.partial_cmp(b).unwrap());
    v[v.len() / 2]
}

fn read(t: &dyn Open, refs: bool, n: u64, reps: usize) {
    let mut rng = 0x1234_5678_9abc_def0u64;
    let mut next = move || {
        rng ^= rng << 13;
        rng ^= rng >> 7;
        rng ^= rng << 17;
        rng % n
    };
    let c = t.counts();
    // cold: a fresh store, one lookup. Open and lookup are timed and counted apart.
    let (mut open_ms, mut get_ms, mut open_r, mut open_b, mut get_r, mut get_b) = (vec![], vec![], 0, 0, 0, 0);
    for _ in 0..reps {
        let k = key(next());
        let (r0, b0) = c.get();
        let t0 = Instant::now();
        let s = t.open();
        open_ms.push(ms(t0));
        let (r1, b1) = c.get();
        let t1 = Instant::now();
        lookup(&s, refs, &k);
        get_ms.push(ms(t1));
        let (r2, b2) = c.get();
        (open_r, open_b, get_r, get_b) = (r1 - r0, b1 - b0, r2 - r1, b2 - b1);
    }
    // warm: one store, many lookups
    let s = t.open();
    let (mut lat, mut r, mut b) = (vec![], 0, 0);
    let lookups = 200;
    for _ in 0..lookups {
        let k = key(next());
        let (r0, b0) = c.get();
        let t0 = Instant::now();
        lookup(&s, refs, &k);
        lat.push(ms(t0));
        let (r1, b1) = c.get();
        r += r1 - r0;
        b += b1 - b0;
    }
    lat.sort_by(|a, b| a.partial_cmp(b).unwrap());
    // list: the 100 keys under one prefix, on an opened store
    let a = next() / 100;
    let prefix = format!("arr/c/{}/{}/", a / 100, a % 100);
    let mut list_ms = vec![];
    let (mut list_r, mut list_b) = (0, 0);
    for _ in 0..reps {
        let s = t.open();
        let (r0, b0) = c.get();
        let t0 = Instant::now();
        let keys = s.list(&prefix).unwrap();
        list_ms.push(ms(t0));
        let (r1, b1) = c.get();
        assert_eq!(keys.len() as u64, 100.min(n - a * 100), "{prefix}");
        (list_r, list_b) = (r1 - r0, b1 - b0);
    }
    println!(
        "{{\"open_ms\":{:.3},\"open_requests\":{open_r},\"open_bytes\":{open_b},\"first_get_ms\":{:.3},\"first_get_requests\":{get_r},\"first_get_bytes\":{get_b},\
\"warm_ms_median\":{:.3},\"warm_ms_p95\":{:.3},\"warm_requests_per_get\":{:.3},\"warm_bytes_per_get\":{:.1},\
\"list_ms\":{:.3},\"list_requests\":{list_r},\"list_bytes\":{list_b}}}",
        median(&mut open_ms),
        median(&mut get_ms),
        lat[lat.len() / 2],
        lat[lat.len() * 95 / 100],
        r as f64 / lookups as f64,
        b as f64 / lookups as f64,
        median(&mut list_ms),
    );
}

/// Opens a store and looks up each key in turn, printing the reads made by the open and by each
/// lookup as JSON: `{"size": n, "steps": [["open", [[offset, length], ...]], [key, [...]], ...]}`.
fn trace(t: &dyn Open, refs: bool, keys: &[String]) {
    let c = t.counts();
    let mut steps = Vec::new();
    let take = || std::mem::take(&mut *c.log.lock().unwrap());
    let fmt = |l: Vec<(u64, u64)>| l.iter().map(|(o, n)| format!("[{o},{n}]")).collect::<Vec<_>>().join(",");
    let s = t.open();
    steps.push(format!("[\"open\",[{}]]", fmt(take())));
    for k in keys {
        lookup(&s, refs, k);
        steps.push(format!("[\"{k}\",[{}]]", fmt(take())));
    }
    println!("{{\"steps\":[{}]}}", steps.join(","));
}

fn main() {
    let a: Vec<String> = std::env::args().collect();
    match a.get(1).map(String::as_str) {
        Some("build") => {
            let variant = a.get(6).map(|tag| (tag.as_str(), a[7].parse().unwrap(), a[8].parse().unwrap()));
            build(&a[2], a[3].parse().unwrap(), &a[4], a[5].parse().unwrap(), variant)
        }
        Some("read") => {
            let defaults = Limits::default();
            let limits = Limits { tail: a.get(6).map_or(DEFAULT_TAIL, |v| v.parse().unwrap()), record_read: a.get(7).map_or(defaults.record_read, |v| v.parse().unwrap()), ..defaults };
            let t: Box<dyn Open> = if a[2].starts_with("http://") { Box::new(HttpOpen(a[2].clone(), Counts::default(), limits)) } else { Box::new(LocalOpen(a[2].clone(), Counts::default(), limits)) };
            read(&*t, a[3] == "refs", a[4].parse().unwrap(), a[5].parse().unwrap());
        }
        Some("trace") => {
            // bench trace <url> <refs|bytes> <key>...
            let t = HttpOpen(a[2].clone(), Counts::default(), Limits::default());
            trace(&t, a[3] == "refs", &a[4..]);
        }
        _ => eprintln!("usage: see the top of this file"),
    }
}
