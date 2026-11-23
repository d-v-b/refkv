//! Store inputs (spec/zarrify.md §1.4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>, §1.5
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>): the host's side
//! of a store run (rust/refkv-ir/src/store.rs), as python/src/refkv/ir/store.py and
//! python/src/refkv/host/stores.py have it. The core is sans-IO: the host lists the store
//! (`DirStore`: a local directory; `HttpStore`: S3 `ListObjectsV2`, or a Hugging Face Hub
//! repository's tree at a commit, every request under the reader policy), gives the listing to a
//! run, and performs the object reads the run asks for, at most the store's workers at a time.

//! The store rules (URLs, the listings' parsing, ignored keys) are the core's (`refkv_ir::host`).

use crate::http::{self, Client};
use crate::Fail;
use refkv_ir::host::{self, HubRepo};
use refkv_ir::store::{StoreFormat, StoreRun};
use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::sync::Arc;

/// Concurrent reads of a remote store, by default (`--workers`).
pub const WORKERS: usize = host::WORKERS;

fn rejected<T>(m: impl Into<String>) -> Result<T, Fail> {
    Err(Fail::Rejected(m.into()))
}

/// A listed store: `objects` maps each relative key to its size, `ignored` holds the relative keys
/// of the ignored objects that are recorded, and `folders` those of the empty objects whose keys
/// end in `/` (directories, to the SAFE profile): the core's `host::store_listing` of every object
/// listed.
pub struct Listing {
    /// the store's name in the output (`--url`)
    pub url: String,
    pub objects: HashMap<String, u64>,
    pub ignored: Vec<String>,
    pub folders: Vec<String>,
    /// the objects as (key, size), keys in the order of their UTF-8 bytes
    pub sorted: Vec<(String, u64)>,
    /// the objects listed, ignored ones included
    pub listed: usize,
    /// the listing's requests
    pub requests: u64,
    /// concurrent object reads
    pub workers: usize,
}

impl Listing {
    fn new(url: &str, workers: usize) -> Listing {
        Listing { url: url.into(), objects: HashMap::new(), ignored: Vec::new(), folders: Vec::new(), sorted: Vec::new(), listed: 0, requests: 0, workers }
    }

    /// The listing of the objects `listed` (key, size), every key under `prefix`.
    fn set(&mut self, listed: Vec<(String, u64)>, prefix: &str) -> Result<(), Fail> {
        self.listed = listed.len();
        let l = host::store_listing(listed, prefix)?;
        self.objects = l.objects.iter().cloned().collect();
        self.sorted = l.objects;
        self.ignored = l.ignored;
        self.folders = l.folders;
        Ok(())
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
        host::check_store_url(url)?;
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
        listing.set(listed, "")?;
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

/// A request's failure, for the retries of python/src/refkv/host/http.py's `_retry`.
enum Failure {
    Status(u16),
    Network(String),
    Fatal(Fail),
}

impl HttpStore {
    pub fn open(url: &str, client: Arc<Client>, workers: usize, max_objects: Option<usize>) -> Result<HttpStore, Fail> {
        let (endpoint, prefix) = host::listing_endpoint(url)?;
        let mut store = HttpStore { client, location: url.to_string(), listing: Listing::new(url, workers) };
        let mut tokens = HashSet::new();
        let mut token: Option<String> = None;
        let mut all = Vec::new();
        loop {
            let (_, _, body) = store.get_s3(&host::listing_url(&endpoint, &prefix, token.as_deref()))?;
            let (listed, next) = host::parse_listing(&body, &prefix)?;
            all.extend(listed);
            if max_objects.is_some_and(|m| all.len() > m) {
                return Err(Fail::Other(format!("the store lists more than {} objects", max_objects.unwrap())));
            }
            let Some(t) = next else { break };
            // the listing would repeat itself (spec/zarrify.md §1.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>)
            if !tokens.insert(t.clone()) {
                return rejected(format!("the listing gives the continuation token {:?} again", t.chars().take(80).collect::<String>()));
            }
            token = Some(t);
        }
        store.listing.set(all, &prefix)?;
        Ok(store)
    }

    /// A Hugging Face Hub repository at a revision, `repo` (spec/zarrify.md §1.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>): the revision
    /// resolved once to a commit, the tree listed at that commit, and the files read at the store's
    /// base (`location`, and `listing.url` unless renamed), which names the commit.
    pub fn open_hub(url: &str, repo: &HubRepo, client: Arc<Client>, workers: usize, max_objects: Option<usize>) -> Result<HttpStore, Fail> {
        let mut store = HttpStore { client, location: url.to_string(), listing: Listing::new(url, workers) };
        let (_, _, body) = store.get_hub(&repo.revision_url())?;
        let commit = host::parse_hub_revision(&body)?;
        let base = repo.base(&commit);
        store.location = base.clone();
        store.listing.url = base;
        let mut requested = HashSet::new();
        let mut link = Some(repo.tree_url(&commit));
        let mut all = Vec::new();
        while let Some(l) = link {
            requested.insert(l.clone());
            let (answered, fields, body) = store.get_hub(&l)?;
            let (listed, entries) = host::parse_hub_tree(&body, &repo.directory)?;
            all.extend(listed);
            if max_objects.is_some_and(|m| all.len() > m) {
                return Err(Fail::Other(format!("the store lists more than {} objects", max_objects.unwrap())));
            }
            link = host::next_link(&fields, &answered, &repo.endpoint)?;
            if link.is_some() && entries == 0 {
                // a page that lists nothing could be followed forever
                return rejected("a Hub tree page with a next link lists nothing");
            }
            if let Some(l) = link.as_ref().filter(|l| requested.contains(*l)) {
                return rejected(format!("the Hub tree's next link {:?} was requested already", l.chars().take(200).collect::<String>()));
            }
        }
        store.listing.set(all, &repo.directory)?;
        Ok(store)
    }

    /// (status, body) of a GET of `url`, following at most 5 redirects: the policy checks `url`,
    /// and each redirect's target before it is requested.
    fn get(&self, url: &str, headers: &[(&str, &str)]) -> Result<(u16, Vec<u8>), Failure> {
        self.request(url, headers).map(|(_, r)| (r.status, r.body))
    }

    /// (the URL that answered, its response) of `get`'s request.
    fn request(&self, url: &str, headers: &[(&str, &str)]) -> Result<(String, http::Response), Failure> {
        let mut url = url.to_string();
        for hop in 0..=host::MAX_REDIRECTS {
            self.client.policy.check(&self.location, &url).map_err(|e| Failure::Fatal(Fail::Refused(e)))?;
            let r = self.client.get(&url, headers, |_, _| true).map_err(|e| match e {
                http::SendError::Refused(m) => Failure::Fatal(Fail::Refused(m)),
                http::SendError::Network(m) => Failure::Network(m),
            })?;
            if !http::REDIRECTS.contains(&r.status) {
                return Ok((url, r));
            }
            let new = http::redirect(&self.location, &url, &r, &self.client.policy).map_err(Failure::Fatal)?;
            if hop == host::MAX_REDIRECTS {
                return Err(Failure::Fatal(Fail::Refused(format!("{}: more than {} redirects", self.location, host::MAX_REDIRECTS))));
            }
            url = new;
        }
        unreachable!()
    }

    /// `f`, retried on 429 and 5xx statuses and on network errors: 5 attempts, 1, 2, 4 and 8 s apart.
    fn retry<T>(f: impl Fn() -> Result<T, Failure>) -> Result<T, Failure> {
        let last = host::ATTEMPTS - 1;
        for attempt in 0..host::ATTEMPTS {
            match f() {
                Err(Failure::Status(s)) if host::RETRY.contains(&s) && attempt < last => {}
                Err(Failure::Network(_)) if attempt < last => {}
                r => return r,
            }
            std::thread::sleep(std::time::Duration::from_secs(1 << attempt));
        }
        unreachable!()
    }

    /// (the URL that answered, its `Link` fields, body) of a listing request's 200 response; a
    /// status that rejects the input is `no_listing`'s rejection.
    fn get_listing(&mut self, url: &str, no_listing: impl Fn(u16) -> String) -> Result<(String, Vec<String>, Vec<u8>), Fail> {
        self.listing.requests += 1;
        let r = Self::retry(|| match self.request(url, &[])? {
            (answered, r) if r.status == 200 => Ok((answered, r.all("link"), r.body)),
            (_, r) => Err(Failure::Status(r.status)),
        });
        match r {
            Ok(b) => Ok(b),
            Err(Failure::Status(s)) if s == 408 || s == 429 || s >= 500 => Err(Fail::Other(format!("{url}: HTTP {s}"))),
            Err(Failure::Status(s)) => Err(Fail::Rejected(no_listing(s))),
            Err(Failure::Network(m)) => Err(Fail::Other(m)),
            Err(Failure::Fatal(e)) => Err(e),
        }
    }

    /// `get_listing` of an S3 listing request.
    fn get_s3(&mut self, url: &str) -> Result<(String, Vec<String>, Vec<u8>), Fail> {
        self.get_listing(url, |s| format!("the store has no listing: {url} answered HTTP {s}"))
    }

    /// `get_listing` of a Hub API request.
    fn get_hub(&mut self, url: &str) -> Result<(String, Vec<String>, Vec<u8>), Fail> {
        self.get_listing(url, |s| {
            let why = match s {
                401 => " (a repository that does not exist, or a private one)",
                404 => " (a revision or a directory the repository does not have)",
                _ => "",
            };
            format!("the Hub answered HTTP {s} for {url}{why}")
        })
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
        let url = host::object_url(&self.location, key);
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
        let url = host::object_url(&self.location, key);
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
    /// and named `url` in the output if given): a Hub store when it is under the Hub's endpoint
    /// `hub` (`host::hub_endpoint`), else an S3-style one. Or a local directory served at `url`.
    pub fn open(location: &str, url: Option<&str>, client: &Arc<Client>, workers: usize, hub: &str) -> Result<Store, Fail> {
        if host::url_scheme(location).is_some_and(|s| s == "http" || s == "https") {
            let mut s = match host::hub_repo(location, hub)? {
                Some(repo) => HttpStore::open_hub(location, &repo, client.clone(), workers, None)?,
                None => HttpStore::open(location, client.clone(), workers, None)?,
            };
            if let Some(u) = url {
                host::check_store_url(u)?;
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
        } else if o.contains_key("info") {
            Ok("precomputed")
        } else if o.contains_key("zarr.json") {
            Ok("zarr3")
        } else if o.keys().any(|k| k.ends_with(".safetensors")) {
            Ok("safetensors")
        } else {
            rejected("not a Zarr v3, Zarr v2, N5, SAFE, Neuroglancer precomputed or safetensors store: the root has no zarr.json, .zarray, .zgroup, attributes.json, manifest.safe or info, and no key ends in .safetensors")
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
    while let Some(reqs) = run.poll().map_err(Fail::from)? {
        let results = crate::run::map(&reqs, if reqs.len() == 1 { 1 } else { workers }, |q| store.fetch(&q.key, &q.spans));
        for (k, parts) in results.into_iter().enumerate() {
            run.complete(k, parts?).map_err(Fail::from)?;
        }
    }
    Ok(())
}

/// The output of the listed store parsed by the core's store format `fmt`, the keys its listing
/// ignores given to the run, its objects read at most the store's workers at a time, and the
/// summary's `listingRequests` the store's requests; sealed for ranges that carry checksums when
/// `checksums`.
pub fn run_store(store: &Store, fmt: &str, checksums: bool) -> Result<refkv_ir::out::Out, Fail> {
    let l = store.listing();
    let workers = l.workers.max(1);
    let format = StoreFormat::from_name(fmt).ok_or_else(|| Fail::Other(format!("no store format {fmt:?}")))?;
    let mut run = StoreRun::new(format, l.sorted.clone(), workers).map_err(Fail::from)?;
    run.ignore(l.ignored.clone(), l.folders.clone()).map_err(Fail::from)?;
    drive(&mut run, store, workers)?;
    let mut out = run.into_output(&l.url, true, checksums)?;
    if let Some(m) = out.summary.as_object_mut() {
        m.insert("listingRequests".into(), l.requests.into());
    }
    Ok(out)
}

/// (format, output) of the store at `location`: the profile its root keys select, run by the core
/// (`run_store`).
pub fn zarrify_store(store: &Store, checksums: bool) -> Result<(String, refkv_ir::out::Out), Fail> {
    let fmt = store.choose_profile()?;
    let out = run_store(store, match fmt {
        "zarr3" => "zarr3",
        "n5" => "n5",
        "safe" => "safe",
        "safetensors" => "safetensors",
        "precomputed" => "precomputed",
        _ => "zarr2",
    }, checksums)?;
    // a Zarr v2 store the core found to be OME-Zarr's says so in its summary
    let fmt = if fmt == "zarr2" { out.summary["format"].as_str().unwrap_or(fmt).to_string() } else { fmt.to_string() };
    Ok((fmt, out))
}

/// The location an input names (`host::input`): a URL, as it is; or a local path, a `file:` URL's
/// (spec/zarrify.md §1.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>), and
/// whether it is a store input: a URL ending in `/`, or a local directory.
pub fn input(location: &str) -> Result<(String, bool), Fail> {
    Ok(match host::input(location, cfg!(windows), false)? {
        host::Input::Url { store } => {
            // a URL of another scheme names a store the application registers (spec/zarrify.md
            // §1.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>): this CLI
            // registers none
            match host::url_scheme(location).as_deref() {
                Some("http" | "https") => (location.to_string(), store),
                s => return Err(Fail::Other(format!("{location}: the CLI reads no {} URL, only http(s) ones and local paths", s.unwrap_or("")))),
            }
        }
        host::Input::Local { path } => {
            let dir = std::path::Path::new(&path).is_dir();
            (path, dir)
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn inputs() {
        assert_eq!(input("https://h/s/").unwrap(), ("https://h/s/".to_string(), true));
        assert_eq!(input("HTTP://h/x.tif").unwrap(), ("HTTP://h/x.tif".to_string(), false));
        let (path, dir) = input(&refkv_vzip::uri::file_uri(&std::env::temp_dir()).unwrap()).unwrap();
        assert!(dir && std::path::Path::new(&path).is_dir());
    }

    #[test]
    fn refuses_a_url_of_another_scheme() {
        assert!(matches!(input("s3://bucket/x.tif"), Err(Fail::Other(_))));
    }
}
