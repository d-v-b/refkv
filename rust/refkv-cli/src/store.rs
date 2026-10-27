//! Store inputs (spec/zarrify.md §1.4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>, §1.5
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>): the host's side
//! of a store run (rust/refkv-ir/src/store.rs), as python/src/refkv/ir/store.py and
//! python/src/refkv/zarrify/store/stores.py have it. The core is sans-IO: the host lists the store
//! (`DirStore`: a local directory; `HttpStore`: S3 `ListObjectsV2`, every request under the reader
//! policy), gives the listing to a run, and performs the object reads the run asks for, at most
//! the store's workers at a time.

use crate::http::{self, Client};
use crate::xml::parse_listing;
use crate::Fail;
use refkv_ir::store::{StoreFormat, StoreRun};
use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::Arc;

/// Concurrent reads of a remote store, by default (`--workers`).
pub const WORKERS: usize = 16;

fn rejected<T>(m: impl Into<String>) -> Result<T, Fail> {
    Err(Fail::Rejected(m.into()))
}

fn pct(data: &[u8], safe: impl Fn(u8) -> bool) -> String {
    let mut s = String::with_capacity(data.len());
    for &b in data {
        if safe(b) {
            s.push(b as char);
        } else {
            s += &format!("%{b:02X}");
        }
    }
    s
}

fn unreserved(b: u8) -> bool {
    b.is_ascii_alphanumeric() || matches!(b, b'-' | b'.' | b'_' | b'~')
}

/// The URL of the object with relative key `key` (spec/zarrify.md §1.4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>).
pub fn object_url(store_url: &str, key: &str) -> String {
    store_url.to_string() + &pct(key.as_bytes(), |b| unreserved(b) || b"!$&'()*+,;=:@/".contains(&b))
}

/// `enc` of spec/zarrify.md §1.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>: every byte but the unreserved ones percent-encoded.
pub fn query_encode(s: &str) -> String {
    pct(s.as_bytes(), unreserved)
}

/// (scheme, authority, path) of a valid store URL (spec/zarrify.md §1.4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>), or a rejection.
pub fn check_store_url(url: &str) -> Result<(String, String, String), Fail> {
    if !refkv_vzip::uri::is_uri_reference(url) {
        return rejected(format!("{url:?} is not a valid URI"));
    }
    let p = refkv_vzip::uri::split(url);
    match p.scheme {
        Some(s) if s.eq_ignore_ascii_case("http") || s.eq_ignore_ascii_case("https") => {}
        _ => return rejected(format!("store URL {url:?} is not http or https")),
    }
    let Some(authority) = p.authority.filter(|a| !a.contains('@')) else {
        return rejected(format!("store URL {url:?} has no authority, or has userinfo"));
    };
    if p.query.is_some() || p.fragment.is_some() {
        return rejected(format!("store URL {url:?} has a query or a fragment"));
    }
    if !p.path.ends_with('/') {
        return rejected(format!("store URL {url:?} does not end in /"));
    }
    Ok((p.scheme.unwrap().to_string(), authority.to_string(), p.path.to_string()))
}

pub fn percent_decode(s: &str) -> Vec<u8> {
    let b = s.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' && i + 2 < b.len() {
            out.push(u8::from_str_radix(&s[i + 1..i + 3], 16).unwrap_or(0));
            i += 3;
        } else {
            out.push(b[i]);
            i += 1;
        }
    }
    out
}

/// (endpoint, prefix) of a store URL (spec/zarrify.md §1.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>): virtual-hosted style for an S3
/// host, path style (the first segment the bucket) for any other.
pub fn listing_endpoint(url: &str) -> Result<(String, String), Fail> {
    let (scheme, authority, path) = check_store_url(url)?;
    let host = if authority.starts_with('[') {
        authority[..authority.find(']').map_or(authority.len(), |i| i + 1)].to_string()
    } else {
        authority.rsplit_once(':').map_or(authority.as_str(), |(h, _)| h).to_string()
    }
    .to_ascii_lowercase();
    let labels: Vec<&str> = host.split('.').collect();
    let virtual_ = host.ends_with(".amazonaws.com") && labels[1..].iter().any(|l| *l == "s3" || l.starts_with("s3-"));
    let (endpoint, prefix_path) = if virtual_ {
        (format!("{scheme}://{authority}/"), path[1..].to_string())
    } else {
        if path == "/" {
            return rejected(format!("path-style store URL {url:?} names no bucket"));
        }
        let (bucket, rest) = path[1..].split_once('/').unwrap_or((&path[1..], ""));
        if bucket.is_empty() {
            return rejected(format!("path-style store URL {url:?} has an empty bucket"));
        }
        (format!("{scheme}://{authority}/{bucket}/"), rest.to_string())
    };
    let prefix = String::from_utf8(percent_decode(&prefix_path)).or_else(|_| rejected(format!("the key prefix of {url:?} is not UTF-8")))?;
    Ok((endpoint, prefix))
}

/// An ignored key (spec/zarrify.md §1.4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>): empty, ending in `/`, or with an empty, `.` or `..`
/// segment.
fn ignored(rel: &str) -> bool {
    rel.is_empty() || rel.ends_with('/') || rel.split('/').any(|s| s.is_empty() || s == "." || s == "..")
}

/// An ignored object whose key is not recorded: an empty one whose relative key is empty or ends
/// in `/`.
fn folder_marker(rel: &str, size: u64) -> bool {
    size == 0 && (rel.is_empty() || rel.ends_with('/'))
}

/// A listed store: `objects` maps each relative key to its size, `ignored` holds the relative keys
/// of the ignored objects that are recorded, and `folders` those of the empty objects whose keys
/// end in `/` (directories, to the SAFE profile).
pub struct Listing {
    /// the store's name in the output (`--url`)
    pub url: String,
    pub objects: HashMap<String, u64>,
    pub ignored: Vec<String>,
    pub folders: Vec<String>,
    pub listed: usize,
    /// the listing's requests
    pub requests: u64,
    /// concurrent object reads
    pub workers: usize,
    seen: HashSet<String>,
}

impl Listing {
    fn new(url: &str, workers: usize) -> Listing {
        Listing {
            url: url.into(),
            objects: HashMap::new(),
            ignored: Vec::new(),
            folders: Vec::new(),
            listed: 0,
            requests: 0,
            workers,
            seen: HashSet::new(),
        }
    }

    fn add(&mut self, listed: Vec<(String, u64)>, prefix: &str) -> Result<(), Fail> {
        for (key, size) in listed {
            if !self.seen.insert(key.clone()) {
                return rejected(format!("key {key:?} is listed twice"));
            }
            let rel = &key[prefix.len()..];
            if !ignored(rel) {
                self.objects.insert(rel.to_string(), size);
            } else if !folder_marker(rel, size) {
                self.ignored.push(rel.to_string());
            } else if !rel.is_empty() && !ignored(&rel[..rel.len() - 1]) {
                self.folders.push(rel.to_string());
            }
        }
        self.listed = self.seen.len();
        Ok(())
    }

    /// The objects as (key, size), keys in the order of their UTF-8 bytes.
    pub fn sorted(&self) -> Vec<(String, u64)> {
        let mut v: Vec<(String, u64)> = self.objects.iter().map(|(k, n)| (k.clone(), *n)).collect();
        v.sort_unstable_by(|a, b| a.0.as_bytes().cmp(b.0.as_bytes()));
        v
    }
}

/// A local directory, read as the store at `url`.
pub struct DirStore {
    root: PathBuf,
    pub listing: Listing,
}

impl DirStore {
    /// The directory's regular files (symbolic links, and on Windows junctions, are not followed),
    /// with UTF-8 names: each one's `/`-separated path is its key.
    pub fn open(path: &str, url: &str) -> Result<DirStore, Fail> {
        check_store_url(url)?;
        let mut listed = Vec::new();
        let mut dirs = vec![(PathBuf::from(path), String::new())];
        while let Some((dir, rel)) = dirs.pop() {
            let Ok(entries) = std::fs::read_dir(&dir) else { continue };
            for e in entries.flatten() {
                let Ok(t) = e.file_type() else { continue };
                let Some(name) = e.file_name().to_str().map(str::to_string) else {
                    continue; // a name that is not UTF-8
                };
                let key = if rel.is_empty() { name } else { format!("{rel}/{name}") };
                if t.is_dir() {
                    dirs.push((e.path(), key));
                } else if t.is_file()
                    && let Ok(md) = std::fs::symlink_metadata(e.path()) {
                        listed.push((key, md.len()));
                    }
            }
        }
        let mut listing = Listing::new(url, 1);
        listing.add(listed, "")?;
        Ok(DirStore { root: PathBuf::from(path), listing })
    }

    fn file(&self, key: &str) -> PathBuf {
        key.split('/').fold(self.root.clone(), |p, s| p.join(s))
    }

    fn read(&self, key: &str) -> Result<Vec<u8>, Fail> {
        let data = std::fs::read(self.file(key)).map_err(|e| Fail::Other(format!("{key}: {e}")))?;
        if data.len() as u64 != self.listing.objects[key] {
            return Err(Fail::Other(format!("{key}: the file changed while it was read")));
        }
        Ok(data)
    }

    fn read_range(&self, key: &str, offset: u64, length: u64) -> Result<Vec<u8>, Fail> {
        use std::io::{Read, Seek, SeekFrom};
        let io = |e: std::io::Error| Fail::Other(format!("{key}: {e}"));
        let mut f = std::fs::File::open(self.file(key)).map_err(io)?;
        f.seek(SeekFrom::Start(offset)).map_err(io)?;
        let mut data = Vec::with_capacity(length as usize);
        f.take(length).read_to_end(&mut data).map_err(io)?;
        if data.len() as u64 != length {
            return Err(Fail::Other(format!("{key}: the file changed while it was read")));
        }
        Ok(data)
    }
}

/// A store listed by S3 ListObjectsV2 (spec/zarrify.md §1.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>), every request under the reader
/// policy: the listing's and each object read's, and each redirect target's, which is checked
/// before it is requested.
pub struct HttpStore {
    client: Arc<Client>,
    /// where the store is listed and read; `listing.url`, its name in the output, may differ
    location: String,
    pub listing: Listing,
}

/// A request's failure, for the retries of python/src/refkv/zarrify/common.py's `_retry`.
enum Failure {
    Status(u16),
    Network(String),
    Fatal(Fail),
}

impl HttpStore {
    pub fn open(url: &str, client: Arc<Client>, workers: usize, max_objects: Option<usize>) -> Result<HttpStore, Fail> {
        let (endpoint, prefix) = listing_endpoint(url)?;
        let mut store = HttpStore { client, location: url.to_string(), listing: Listing::new(url, workers) };
        let mut tokens = HashSet::new();
        let mut token: Option<String> = None;
        loop {
            let mut q = format!("?list-type=2&prefix={}", query_encode(&prefix));
            if let Some(t) = &token {
                q += &format!("&continuation-token={}", query_encode(t));
            }
            let body = store.get_listing(&format!("{endpoint}{q}"))?;
            let (listed, next) = parse_listing(&body, &prefix).map_err(Fail::Rejected)?;
            store.listing.add(listed, &prefix)?;
            if max_objects.is_some_and(|m| store.listing.listed > m) {
                return Err(Fail::Other(format!("the store lists more than {} objects", max_objects.unwrap())));
            }
            let Some(t) = next else { break };
            // the listing would repeat itself (spec/zarrify.md §1.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>)
            if !tokens.insert(t.clone()) {
                return rejected(format!("the listing gives the continuation token {:?} again", t.chars().take(80).collect::<String>()));
            }
            token = Some(t);
        }
        Ok(store)
    }

    /// (status, body) of a GET of `url`, following at most 5 redirects: the policy checks `url`,
    /// and each redirect's target before it is requested.
    fn get(&self, url: &str, headers: &[(&str, &str)]) -> Result<(u16, Vec<u8>), Failure> {
        let mut url = url.to_string();
        for hop in 0..6 {
            self.client.policy.check(&self.location, &url).map_err(|e| Failure::Fatal(Fail::Refused(e)))?;
            let r = self.client.get(&url, headers, |_, _| true).map_err(|e| match e {
                http::SendError::Refused(m) => Failure::Fatal(Fail::Refused(m)),
                http::SendError::Network(m) => Failure::Network(m),
            })?;
            if !http::REDIRECTS.contains(&r.status) {
                return Ok((r.status, r.body));
            }
            let new = http::redirect(&self.location, &url, &r, &self.client.policy).map_err(Failure::Fatal)?;
            if hop == 5 {
                return Err(Failure::Fatal(Fail::Refused(format!("{}: more than 5 redirects", self.location))));
            }
            url = new;
        }
        unreachable!()
    }

    /// `f`, retried on 429 and 5xx statuses and on network errors: 5 attempts, 1, 2, 4 and 8 s apart.
    fn retry<T>(f: impl Fn() -> Result<T, Failure>) -> Result<T, Failure> {
        for attempt in 0..5 {
            match f() {
                Err(Failure::Status(s)) if [429, 500, 502, 503, 504].contains(&s) && attempt < 4 => {}
                Err(Failure::Network(_)) if attempt < 4 => {}
                r => return r,
            }
            std::thread::sleep(std::time::Duration::from_secs(1 << attempt));
        }
        unreachable!()
    }

    fn get_listing(&mut self, url: &str) -> Result<Vec<u8>, Fail> {
        self.listing.requests += 1;
        let r = Self::retry(|| match self.get(url, &[])? {
            (200, body) => Ok(body),
            (s, _) => Err(Failure::Status(s)),
        });
        match r {
            Ok(b) => Ok(b),
            Err(Failure::Status(s)) if s == 408 || s == 429 || s >= 500 => Err(Fail::Other(format!("{url}: HTTP {s}"))),
            Err(Failure::Status(s)) => rejected(format!("the store has no listing: {url} answered HTTP {s}")),
            Err(Failure::Network(m)) => Err(Fail::Other(m)),
            Err(Failure::Fatal(e)) => Err(e),
        }
    }

    /// The bytes [start, end) of the object at `url`.
    fn get_range(&self, url: &str, start: u64, end: u64) -> Result<Vec<u8>, Fail> {
        let range = format!("bytes={start}-{}", end - 1);
        let r = Self::retry(|| match self.get(url, &[("Range", &range)])? {
            (200 | 206, body) => Ok(body),
            (s, _) => Err(Failure::Status(s)),
        });
        r.map_err(|e| match e {
            Failure::Status(s) => Fail::Other(format!("{url}: HTTP {s}")),
            Failure::Network(m) => Fail::Other(m),
            Failure::Fatal(e) => e,
        })
    }

    fn read(&self, key: &str) -> Result<Vec<u8>, Fail> {
        let size = self.listing.objects[key];
        if size == 0 {
            return Ok(Vec::new());
        }
        let url = object_url(&self.location, key);
        let data = self.get_range(&url, 0, size)?;
        if data.len() as u64 != size {
            return Err(Fail::Other(format!("{url}: read {} bytes, the listing says {size}", data.len())));
        }
        Ok(data)
    }

    fn read_range(&self, key: &str, offset: u64, length: u64) -> Result<Vec<u8>, Fail> {
        if length == 0 {
            return Ok(Vec::new());
        }
        let url = object_url(&self.location, key);
        let data = self.get_range(&url, offset, offset + length)?;
        if data.len() as u64 != length {
            return Err(Fail::Other(format!("{url}: read {} bytes at {offset}, not {length}", data.len())));
        }
        Ok(data)
    }
}

/// A store input.
pub enum Store {
    Dir(DirStore),
    Http(HttpStore),
}

impl Store {
    /// The store at an http(s) URL ending in `/` (listed and read there under the client's policy,
    /// and named `url` in the output if given), or a local directory served at `url`.
    pub fn open(location: &str, url: Option<&str>, client: &Arc<Client>, workers: usize) -> Result<Store, Fail> {
        if location.starts_with("http://") || location.starts_with("https://") {
            let mut s = HttpStore::open(location, client.clone(), workers, None)?;
            if let Some(u) = url {
                check_store_url(u)?;
                s.listing.url = u.to_string();
            }
            return Ok(Store::Http(s));
        }
        let Some(url) = url else {
            return Err(Fail::Usage("a local directory needs --url, the URL it is served from".into()));
        };
        Ok(Store::Dir(DirStore::open(location, url)?))
    }

    pub fn listing(&self) -> &Listing {
        match self {
            Store::Dir(s) => &s.listing,
            Store::Http(s) => &s.listing,
        }
    }

    pub fn read(&self, key: &str) -> Result<Vec<u8>, Fail> {
        match self {
            Store::Dir(s) => s.read(key),
            Store::Http(s) => s.read(key),
        }
    }

    pub fn read_range(&self, key: &str, offset: u64, length: u64) -> Result<Vec<u8>, Fail> {
        match self {
            Store::Dir(s) => s.read_range(key, offset, length),
            Store::Http(s) => s.read_range(key, offset, length),
        }
    }

    /// The store profile the root keys select (spec/zarrify.md §1.4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>).
    pub fn choose_profile(&self) -> Result<&'static str, Fail> {
        let o = &self.listing().objects;
        if o.contains_key(".zarray") || o.contains_key(".zgroup") {
            Ok("zarr2")
        } else if o.contains_key("attributes.json") {
            Ok("n5")
        } else if o.contains_key("manifest.safe") {
            Ok("safe")
        } else {
            rejected("not an N5, Zarr v2 or SAFE store: the root has no .zarray, .zgroup, attributes.json or manifest.safe")
        }
    }

    /// One request of a run: the object `key`, whole when its one span is all of it.
    fn fetch(&self, key: &str, spans: &[(u64, u64)]) -> Result<Vec<Vec<u8>>, Fail> {
        let size = self.listing().objects[key];
        if spans.len() == 1 && spans[0] == (0, size) {
            return Ok(vec![self.read(key)?]);
        }
        spans.iter().map(|&(a, b)| self.read_range(key, a, b - a)).collect()
    }
}

/// Performs the object reads a store run asks for until it is done, at most `workers` at a time.
fn drive(run: &mut StoreRun, store: &Store, workers: usize) -> Result<(), Fail> {
    while let Some(reqs) = run.poll().map_err(Fail::Rejected)? {
        let results = crate::run::map(&reqs, if reqs.len() == 1 { 1 } else { workers }, |q| store.fetch(&q.key, &q.spans));
        for (k, parts) in results.into_iter().enumerate() {
            run.complete(k, parts?).map_err(Fail::Rejected)?;
        }
    }
    Ok(())
}

/// The output of the listed store parsed by the core's store format `fmt`, the keys its listing
/// ignores given to the run, its objects read at most the store's workers at a time, and the
/// summary's `listingRequests` the store's requests.
pub fn run_store(store: &Store, fmt: &str) -> Result<refkv_ir::out::Out, Fail> {
    let l = store.listing();
    let workers = l.workers.max(1);
    let format = StoreFormat::from_name(fmt).ok_or_else(|| Fail::Other(format!("no store format {fmt:?}")))?;
    let mut run = StoreRun::new(format, l.sorted(), workers).map_err(Fail::Rejected)?;
    run.ignore(l.ignored.clone(), l.folders.clone()).map_err(Fail::Rejected)?;
    drive(&mut run, store, workers)?;
    let mut out = run.into_output(&l.url, true).map_err(Fail::Rejected)?;
    if let Some(m) = out.summary.as_object_mut() {
        m.insert("listingRequests".into(), l.requests.into());
    }
    Ok(out)
}

/// (format, output) of the store at `location`: the profile its root keys select, run by the core.
pub fn zarrify_store(store: &Store) -> Result<(String, refkv_ir::out::Out), Fail> {
    let fmt = store.choose_profile()?;
    let out = run_store(store, match fmt {
        "n5" => "n5",
        "safe" => "safe",
        _ => "zarr2",
    })?;
    // a Zarr v2 store the core found to be OME-Zarr's says so in its summary
    let fmt = if fmt == "zarr2" { out.summary["format"].as_str().unwrap_or(fmt).to_string() } else { fmt.to_string() };
    Ok((fmt, out))
}

/// Is `location` a store input: an http(s) URL ending in `/`, or a local directory?
pub fn is_store(location: &str) -> bool {
    if location.starts_with("http://") || location.starts_with("https://") {
        return location.split('?').next().unwrap_or("").split('#').next().unwrap_or("").ends_with('/');
    }
    Path::new(location).is_dir()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn urls() {
        assert_eq!(object_url("http://h/b/", "a b/c~d%é:@!"), "http://h/b/a%20b/c~d%25%C3%A9:@!");
        assert_eq!(query_encode("a/b c~"), "a%2Fb%20c~");
        assert_eq!(listing_endpoint("http://127.0.0.1:8000/f/n5/x/").unwrap(), ("http://127.0.0.1:8000/f/".into(), "n5/x/".into()));
        assert_eq!(listing_endpoint("https://b.s3.us-west-2.amazonaws.com/p/q/").unwrap(), ("https://b.s3.us-west-2.amazonaws.com/".into(), "p/q/".into()));
        assert_eq!(listing_endpoint("https://s3.amazonaws.com/b/").unwrap(), ("https://s3.amazonaws.com/b/".into(), "".into()));
        assert_eq!(listing_endpoint("http://[::1]:9/b/%C3%A9/").unwrap(), ("http://[::1]:9/b/".into(), "é/".into()));
        assert!(is_store("http://h/a/?x"));
        assert!(!is_store("http://h/a"));
    }

    #[test]
    fn rejects_an_invalid_store_url() {
        assert!(matches!(check_store_url("http://h/a b/"), Err(Fail::Rejected(_))));
    }

    #[test]
    fn rejects_another_scheme() {
        assert!(matches!(check_store_url("ftp://h/a/"), Err(Fail::Rejected(_))));
    }

    #[test]
    fn rejects_userinfo() {
        assert!(matches!(check_store_url("http://u@h/a/"), Err(Fail::Rejected(_))));
    }

    #[test]
    fn rejects_a_query() {
        assert!(matches!(check_store_url("http://h/a/?q"), Err(Fail::Rejected(_))));
    }

    #[test]
    fn rejects_no_trailing_slash() {
        assert!(matches!(check_store_url("http://h/a"), Err(Fail::Rejected(_))));
    }

    #[test]
    fn rejects_no_bucket() {
        assert!(matches!(listing_endpoint("http://h/"), Err(Fail::Rejected(_))));
    }

    #[test]
    fn rejects_an_empty_bucket() {
        assert!(matches!(listing_endpoint("http://h//a/"), Err(Fail::Rejected(_))));
    }

    #[test]
    fn rejects_a_prefix_that_is_not_utf8() {
        assert!(matches!(listing_endpoint("http://h/b/%FF/"), Err(Fail::Rejected(_))));
    }

    #[test]
    fn listings() {
        let mut l = Listing::new("http://h/b/", 1);
        l.add(vec![("p/a".into(), 1), ("p/".into(), 0), ("p/d/".into(), 0), ("p/x/./y".into(), 3), ("p/e/".into(), 2), ("p//f/".into(), 0)], "p/")
            .unwrap();
        assert_eq!(l.objects, HashMap::from([("a".to_string(), 1)]));
        assert_eq!(l.ignored, vec!["x/./y".to_string(), "e/".to_string()]);
        assert_eq!(l.folders, vec!["d/".to_string()]);
        assert_eq!(l.listed, 6);
    }

    #[test]
    fn rejects_a_key_listed_twice() {
        let mut l = Listing::new("http://h/b/", 1);
        assert!(matches!(l.add(vec![("a".into(), 1), ("a".into(), 1)], ""), Err(Fail::Rejected(_))));
    }
}
