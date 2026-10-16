//! The host's side of the read planner (design/ARCHITECTURE.md §3.5
//! <https://github.com/d-v-b/refkv/blob/main/design/ARCHITECTURE.md#35-resources-and-io>): the I/O
//! of a file input, as python/src/refkv/ir/planner.py performs it. The policy (what to request:
//! coalescing by cost, the amplification cap, multi-range packing, the budget) is the Rust core's
//! (`refkv_ir::plan`); a transport only performs the requests a run asks for, and reports each
//! one's bytes and time.
//!
//! - `File`: a local file, mapped; its requests are served in the calling thread.
//! - `Http`: an http(s) object read under the reader policy (spec/refkv.md §6.7
//!   <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>): the request that
//!   opens it asks for `bytes=0-65535`; at most 5 redirects are followed, each target checked
//!   before it is requested; requests are retried on 429, 5xx and network errors with exponential
//!   backoff and jitter, waiting at least what `Retry-After` says. Several ranges go in one
//!   request (`multipart/byteranges`) when the run asks; a server that answers otherwise is closed
//!   before its body is read, and the run plans single ranges. Every response's ETag is recorded,
//!   for the source's `etag` pin.

use crate::http::{self, Client, Response, SendError};
use crate::Fail;
use std::collections::BTreeSet;
use std::sync::{Arc, Mutex};
use std::time::Instant;

/// The bytes the request that opens a remote source reads.
const OPEN: u64 = 1 << 16;
const RETRY: [u16; 5] = [429, 500, 502, 503, 504];

/// The ETag of every response for one object, for its `etag` pin (spec/virtualize.md §1.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize.md#12-input>).
#[derive(Default)]
pub struct EtagLog {
    seen: Mutex<BTreeSet<Option<String>>>,
}

impl EtagLog {
    /// Records a response's ETag; two strong ones mean the object changed while it was read.
    pub fn saw(&self, etag: Option<String>) -> Result<(), String> {
        let mut seen = self.seen.lock().unwrap();
        seen.insert(etag);
        let strong: Vec<&String> = seen.iter().flatten().filter(|e| !e.starts_with("W/")).collect();
        if strong.len() > 1 {
            return Err(format!("the object changed while it was read (ETags {strong:?})"));
        }
        Ok(())
    }

    /// The object's strong ETag, if every response gave the same one.
    pub fn pin(&self) -> Option<String> {
        let seen = self.seen.lock().unwrap();
        match (seen.len(), seen.iter().next()) {
            (1, Some(Some(e))) if refkv_vzip::pb::is_strong_etag(e) => Some(e.clone()),
            _ => None,
        }
    }
}

/// A local file: ranges are slices of its mapping.
pub struct FileTransport {
    map: Option<memmap2::Mmap>,
    size: u64,
}

impl FileTransport {
    pub fn open(path: &str) -> Result<FileTransport, Fail> {
        let f = std::fs::File::open(path).map_err(|e| Fail::Other(format!("{path}: {e}")))?;
        let size = f.metadata().map_err(|e| Fail::Other(format!("{path}: {e}")))?.len();
        // SAFETY: the mapping is read only; as for Python's mmap, a file changed while it is read
        // gives the bytes it then has
        let map = if size > 0 { Some(unsafe { memmap2::Mmap::map(&f) }.map_err(|e| Fail::Other(format!("{path}: {e}")))?) } else { None };
        Ok(FileTransport { map, size })
    }

    fn get(&self, start: u64, end: u64) -> Vec<u8> {
        let m: &[u8] = self.map.as_deref().unwrap_or(&[]);
        let (a, b) = ((start as usize).min(m.len()), (end as usize).min(m.len()));
        m[a..b.max(a)].to_vec()
    }
}

/// Why one attempt failed: for good (`Fatal`: the policy, or a response no retry mends), on a
/// status to retry (with its `Retry-After`), or on a network error.
enum Attempt {
    Fatal(Fail),
    Retry(u16, Option<String>),
    Network(String),
}

impl From<SendError> for Attempt {
    fn from(e: SendError) -> Attempt {
        match e {
            SendError::Refused(m) => Attempt::Fatal(Fail::Refused(m)),
            SendError::Network(m) => Attempt::Network(m),
        }
    }
}

impl From<Fail> for Attempt {
    fn from(e: Fail) -> Attempt {
        Attempt::Fatal(e)
    }
}

/// An http(s) object, read under the reader policy.
pub struct HttpTransport {
    client: Arc<Client>,
    /// where requests go: the input's URL, then where its redirects led
    url: Mutex<String>,
    attempts: u32,
    base_delay: f64,
    etags: EtagLog,
    size: u64,
    head: Vec<u8>,
    head_seconds: f64,
}

impl HttpTransport {
    pub fn open(url: &str, client: Arc<Client>) -> Result<HttpTransport, Fail> {
        http::check_http_url(url)?;
        client.policy.check(url, url).map_err(Fail::Refused)?;
        let mut t = HttpTransport {
            client,
            url: Mutex::new(url.to_string()),
            attempts: 6,
            base_delay: 0.5,
            etags: EtagLog::default(),
            size: 0,
            head: Vec::new(),
            head_seconds: 0.0,
        };
        // the request that opens the source: its size, its first bytes, and a time for the cost model
        let t0 = Instant::now();
        let r = t.retry(|| t.request(&format!("bytes=0-{}", OPEN - 1), false))?;
        let dt = t0.elapsed().as_secs_f64();
        let range = r.header("content-range").unwrap_or_default();
        match (r.status, content_range(&range)) {
            (206, Some((0, last, size))) if r.body.len() as u64 == last + 1 => {
                t.size = size;
                t.head = r.body;
            }
            (416, _) => {} // an empty object
            // a 200 (whose body is not read): the server does not serve ranges; or an error
            _ => return Err(Fail::Other(format!("{url}: HTTP {} for a range of its first bytes", r.status))),
        }
        t.head.truncate(OPEN as usize);
        t.head_seconds = dt;
        Ok(t)
    }

    /// One GET of the object with the header `Range: <range>`, redirects followed and checked. A
    /// body that is not a 206 (or, with `multipart`, not multipart/byteranges) is not read.
    fn request(&self, range: &str, multipart: bool) -> Result<Response, Attempt> {
        let start = self.url.lock().unwrap().clone();
        let mut url = start.clone();
        let mut hop = 0;
        let r = loop {
            let r = self.client.get(&url, &[("Range", range)], |status, h| {
                status == 206 && (!multipart || h.get("content-type").is_some_and(|v| String::from_utf8_lossy(v.as_bytes()).contains("multipart/byteranges")))
            })?;
            if !http::REDIRECTS.contains(&r.status) {
                break r;
            }
            let new = http::redirect(&start, &url, &r, &self.client.policy)?;
            if hop == 5 {
                return Err(Attempt::Fatal(Fail::Refused(format!("{start}: more than 5 redirects"))));
            }
            hop += 1;
            url = new;
        };
        *self.url.lock().unwrap() = url; // later requests go where the redirects led
        if RETRY.contains(&r.status) {
            return Err(Attempt::Retry(r.status, r.header("retry-after")));
        }
        if r.status == 200 || r.status == 206 {
            self.etags.saw(r.header("etag")).map_err(Attempt::Network)?;
        }
        Ok(r)
    }

    fn wait(&self, attempt: u32, retry_after: Option<&str>) {
        let mut delay = self.base_delay * 2f64.powi(attempt as i32) * http::jitter();
        if let Some(s) = retry_after.and_then(http::retry_after) {
            delay = delay.max(s);
        }
        std::thread::sleep(std::time::Duration::from_secs_f64(delay.clamp(0.0, 120.0)));
    }

    fn retry<T>(&self, f: impl Fn() -> Result<T, Attempt>) -> Result<T, Fail> {
        let url = || self.url.lock().unwrap().clone();
        for attempt in 0..self.attempts {
            let last = attempt + 1 == self.attempts;
            match f() {
                Ok(v) => return Ok(v),
                Err(Attempt::Fatal(e)) => return Err(e),
                Err(Attempt::Retry(status, _)) if last => {
                    return Err(Fail::Other(format!("{}: HTTP {status} after {} attempts", url(), self.attempts)));
                }
                Err(Attempt::Retry(_, ra)) => self.wait(attempt, ra.as_deref()),
                Err(Attempt::Network(m)) if last => return Err(Fail::Other(m)),
                Err(Attempt::Network(_)) => self.wait(attempt, None),
            }
        }
        unreachable!("attempts > 0")
    }

    pub fn get(&self, start: u64, end: u64) -> Result<Vec<u8>, Fail> {
        self.retry(|| {
            let r = self.request(&format!("bytes={start}-{}", end as i128 - 1), false)?;
            if r.status != 206 || r.body.len() as u64 != end - start {
                return Err(Attempt::Fatal(Fail::Other(format!(
                    "{}: HTTP {}, {} bytes for [{start}, {end})",
                    self.url.lock().unwrap(),
                    r.status,
                    r.body.len()
                ))));
            }
            Ok(r.body)
        })
    }

    /// Several ranges in one request (multipart/byteranges), or None when the server does not
    /// answer with them, or fails on the request: tried once, as the run then plans single ranges
    /// (which retry).
    fn get_many(&self, spans: &[(u64, u64)]) -> Result<Option<Vec<Vec<u8>>>, Fail> {
        let header = format!("bytes={}", spans.iter().map(|(a, b)| format!("{a}-{}", b - 1)).collect::<Vec<_>>().join(","));
        let r = match self.request(&header, true) {
            Ok(r) => r,
            Err(Attempt::Fatal(Fail::Refused(m))) => return Err(Fail::Refused(m)),
            Err(_) => return Ok(None),
        };
        let ctype = r.header("content-type").unwrap_or_default();
        if r.status != 206 || !ctype.contains("multipart/byteranges") {
            return Ok(None);
        }
        let got = parts(&r.body, &ctype);
        let mut out = Vec::with_capacity(spans.len());
        for &(a, b) in spans {
            let Some(p) = got.iter().find(|(s, _)| *s == (a, b)) else {
                return Err(Fail::Other(format!("{}: the multipart answer lacks [{a}, {b})", self.url.lock().unwrap())));
            };
            out.push(p.1.to_vec());
        }
        Ok(Some(out))
    }
}

/// `bytes <first>-<last>/<size>` (the unit in any case): (first, last, size).
fn content_range(v: &str) -> Option<(u64, u64, u64)> {
    let v = v.trim();
    let (unit, rest) = v.split_once(' ')?;
    if !unit.eq_ignore_ascii_case("bytes") {
        return None;
    }
    let (span, size) = rest.split_once('/')?;
    let (a, b) = span.split_once('-')?;
    let num = |s: &str| (!s.is_empty() && s.bytes().all(|c| c.is_ascii_digit())).then(|| s.parse::<u64>().ok()).flatten();
    Some((num(a)?, num(b)?, num(size)?))
}

/// The parts of a multipart/byteranges body, by (start, end).
fn parts<'a>(body: &'a [u8], ctype: &str) -> Vec<((u64, u64), &'a [u8])> {
    let boundary = ctype.split_once("boundary=").map_or("", |(_, b)| b).trim().trim_matches('"');
    let sep = [b"--".as_slice(), boundary.as_bytes()].concat();
    let find = |hay: &[u8], needle: &[u8], from: usize| -> Option<usize> {
        if from > hay.len() {
            return None;
        }
        hay[from..].windows(needle.len().max(1)).position(|w| w == needle).map(|i| i + from)
    };
    let mut out = Vec::new();
    let mut pos = 0;
    loop {
        let Some(k) = find(body, &sep, pos) else { return out };
        if body.get(k + sep.len()..k + sep.len() + 2) == Some(b"--") {
            return out;
        }
        let Some(head_end) = find(body, b"\r\n\r\n", k) else { return out };
        let mut span = None;
        for line in body[k + sep.len()..head_end].split(|&c| c == b'\n') {
            let line = String::from_utf8_lossy(line);
            let line = line.trim_end_matches('\r');
            let (name, value) = line.split_once(':').unwrap_or((line, ""));
            if name.trim().eq_ignore_ascii_case("content-range") {
                let rng = value.split_whitespace().nth(1).unwrap_or("").split('/').next().unwrap_or("");
                if let Some((a, b)) = rng.split_once('-')
                    && let (Ok(a), Ok(b)) = (a.parse::<u64>(), b.parse::<u64>()) {
                        span = Some((a, b + 1));
                    }
            }
        }
        let Some((a, b)) = span else { return out };
        let start = head_end + 4;
        let end = (start + (b - a) as usize).min(body.len());
        out.push(((a, b), &body[start..end]));
        pos = end;
    }
}

/// What one request of a run read: each span's bytes, or None, and the seconds it took.
pub type Fetched = (Option<Vec<Vec<u8>>>, f64);

/// A file input's transport.
pub enum Transport {
    File(FileTransport),
    Http(HttpTransport),
}

impl Transport {
    pub fn open(location: &str, client: &Arc<Client>) -> Result<Transport, Fail> {
        if location.starts_with("http://") || location.starts_with("https://") {
            Ok(Transport::Http(HttpTransport::open(location, client.clone())?))
        } else {
            Ok(Transport::File(FileTransport::open(location)?))
        }
    }

    pub fn remote(&self) -> bool {
        matches!(self, Transport::Http(_))
    }

    pub fn size(&self) -> u64 {
        match self {
            Transport::File(f) => f.size,
            Transport::Http(h) => h.size,
        }
    }

    /// The bytes the transport opened the source with (from offset 0), and the seconds it took.
    pub fn head(&self) -> (&[u8], f64) {
        match self {
            Transport::File(_) => (&[], 0.0),
            Transport::Http(h) => (&h.head, h.head_seconds),
        }
    }

    pub fn get(&self, start: u64, end: u64) -> Result<Vec<u8>, Fail> {
        match self {
            Transport::File(f) => Ok(f.get(start, end)),
            Transport::Http(h) => h.get(start, end),
        }
    }

    /// One request of a run: each span's bytes (None: a multi-range request the server did not
    /// answer with its ranges), and the seconds it took.
    pub fn fetch(&self, spans: &[(u64, u64)]) -> Result<Fetched, Fail> {
        match self {
            Transport::File(f) => Ok((Some(spans.iter().map(|&(a, b)| f.get(a, b)).collect()), 0.0)),
            Transport::Http(h) => {
                let t = Instant::now();
                let got = if spans.len() == 1 { Some(vec![h.get(spans[0].0, spans[0].1)?]) } else { h.get_many(spans)? };
                Ok((got, t.elapsed().as_secs_f64()))
            }
        }
    }

    /// The source's strong ETag, if every response gave the same one.
    pub fn etag(&self) -> Option<String> {
        match self {
            Transport::File(_) => None,
            Transport::Http(h) => h.etags.pin(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn etags() {
        let log = EtagLog::default();
        assert_eq!(log.pin(), None);
        log.saw(Some("\"a\"".into())).unwrap();
        log.saw(Some("\"a\"".into())).unwrap();
        assert_eq!(log.pin(), Some("\"a\"".into()));
        log.saw(Some("W/\"b\"".into())).unwrap();
        assert_eq!(log.pin(), None);
        let missing = EtagLog::default();
        missing.saw(None).unwrap();
        assert_eq!(missing.pin(), None);
    }

    #[test]
    fn refuses_two_strong_etags() {
        let log = EtagLog::default();
        log.saw(Some("\"a\"".into())).unwrap();
        assert!(log.saw(Some("\"b\"".into())).is_err());
    }

    #[test]
    fn content_ranges() {
        assert_eq!(content_range(" BYTES 0-9/100 "), Some((0, 9, 100)));
        assert_eq!(content_range("bytes 5-5/6"), Some((5, 5, 6)));
        assert_eq!(content_range("bytes */6"), None);
        assert_eq!(content_range("items 0-1/2"), None);
        assert_eq!(content_range("bytes 0-1/*"), None);
    }

    #[test]
    fn multipart_bodies() {
        let body = b"--B\r\nContent-Type: x\r\nContent-Range: bytes 0-2/10\r\n\r\nabc\r\n--B\r\nContent-Range: bytes 7-8/10\r\n\r\nhi\r\n--B--\r\n";
        let got = parts(body, "multipart/byteranges; boundary=\"B\"");
        assert_eq!(got, vec![((0, 3), b"abc".as_slice()), ((7, 9), b"hi".as_slice())]);
        assert!(parts(b"--B\r\nno head end", "multipart/byteranges; boundary=B").is_empty());
        assert!(parts(b"--B\r\nX: y\r\n\r\nabc", "multipart/byteranges; boundary=B").is_empty());
    }
}
