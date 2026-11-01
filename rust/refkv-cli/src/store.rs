//! Store inputs (spec/zarrify.md §1.4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>, §1.5
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>): the host's side
//! of a store run (rust/refkv-ir/src/store.rs), as python/src/refkv/ir/store.py and
//! python/src/refkv/zarrify/store/stores.py have it. The core is sans-IO: the host lists the store
//! (`DirStore`: a local directory; `HttpStore`: S3 `ListObjectsV2`, or a Hugging Face Hub
//! repository's tree at a commit, every request under the reader policy), gives the listing to a
//! run, and performs the object reads the run asks for, at most the store's workers at a time.

use crate::http::{self, Client};
use crate::xml::parse_listing;
use crate::Fail;
use refkv_ir::json::Jv;
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

/// The Hugging Face Hub's endpoint, unless `HF_ENDPOINT` gives another.
pub const HUB: &str = "https://huggingface.co";

/// The Hub's endpoint `E` (spec/zarrify.md §1.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>): `endpoint` (the environment's
/// `HF_ENDPOINT`), else [`HUB`], without a trailing `/` (as `huggingface_hub` reads it); an error
/// if it is not an http(s) URL with an authority, without userinfo, a query or a fragment.
pub fn hub_endpoint(endpoint: Option<&str>) -> Result<String, Fail> {
    let e = endpoint.filter(|e| !e.is_empty()).unwrap_or(HUB).trim_end_matches('/').to_string();
    let p = refkv_vzip::uri::split(&e);
    let http = p.scheme.is_some_and(|s| s.eq_ignore_ascii_case("http") || s.eq_ignore_ascii_case("https"));
    if !refkv_vzip::uri::is_uri_reference(&e)
        || !http
        || p.authority.is_none_or(|a| a.is_empty() || a.contains('@'))
        || p.query.is_some()
        || p.fragment.is_some()
    {
        return Err(Fail::Other(format!("the Hub endpoint {e:?} is not an http(s) URL without userinfo, query or fragment")));
    }
    Ok(e)
}

/// A Hub store (spec/zarrify.md §1.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>): its endpoint, kind (`models` or `datasets`), owner,
/// name, revision `r` and directory `D` (empty, or ending in `/`), the last two percent-decoded.
#[derive(Debug, PartialEq)]
pub struct HubRepo {
    pub endpoint: String,
    pub kind: &'static str,
    pub owner: String,
    pub name: String,
    pub revision: String,
    pub directory: String,
}

impl HubRepo {
    /// `A`: the repository's API URL.
    fn api(&self) -> String {
        format!("{}/api/{}/{}/{}", self.endpoint, self.kind, self.owner, self.name)
    }

    pub fn revision_url(&self) -> String {
        format!("{}/revision/{}?expand%5B%5D=sha&expand%5B%5D=gated", self.api(), query_encode(&self.revision))
    }

    pub fn tree_url(&self, commit: &str) -> String {
        let d = match self.directory.strip_suffix('/') {
            Some(d) => format!("/{}", object_url("", d)),
            None => String::new(),
        };
        format!("{}/tree/{commit}{d}?recursive=true", self.api())
    }

    /// The store's base (spec/zarrify.md §1.4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>) at `commit`.
    pub fn base(&self, commit: &str) -> String {
        let kind = if self.kind == "datasets" { "/datasets/" } else { "/" };
        format!("{}{kind}{}/{}/resolve/{commit}/{}", self.endpoint, self.owner, self.name, object_url("", &self.directory))
    }
}

/// The rest of `url`'s path after `endpoint`'s and a `/`, when `url` has `endpoint`'s scheme and
/// authority (in any case) and its path starts so.
fn under<'a>(url: &'a str, endpoint: &str) -> Option<&'a str> {
    let (u, e) = (refkv_vzip::uri::split(url), refkv_vzip::uri::split(endpoint));
    let same = |a: Option<&str>, b: Option<&str>| matches!((a, b), (Some(a), Some(b)) if a.eq_ignore_ascii_case(b));
    if !same(u.scheme, e.scheme) || !same(u.authority, e.authority) {
        return None;
    }
    u.path.strip_prefix(e.path)?.strip_prefix('/')
}

fn hub_name(n: &str) -> bool {
    !n.is_empty() && n != "." && n != ".." && n.bytes().all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-'))
}

/// The Hub store that the store URL `url` names under `endpoint` (spec/zarrify.md §1.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>), None
/// if `url` is not under it, or a rejection if it is but does not name one.
pub fn hub_repo(url: &str, endpoint: &str) -> Result<Option<HubRepo>, Fail> {
    let Some(rest) = under(url, endpoint) else { return Ok(None) };
    check_store_url(url)?;
    let mut segments: Vec<&str> = rest.split('/').collect();
    let mut kind = "models";
    if segments[0] == "datasets" {
        kind = "datasets";
        segments.remove(0);
    }
    // owner, name, "tree", rev, dir segments..., "" (the trailing /)
    let n = segments.len();
    if n < 5 || segments[2] != "tree" || !segments[n - 1].is_empty() || !segments[..2].iter().all(|s| hub_name(s)) || segments[3..n - 1].iter().any(|s| s.is_empty()) {
        return rejected(format!("Hub store URL {url:?} is not <owner>/<repo>/tree/<rev>/[<dir>/] (or that after datasets/)"));
    }
    let utf8 = |s: &str| String::from_utf8(percent_decode(s)).or_else(|_| rejected(format!("the revision or directory of {url:?} is not UTF-8")));
    let revision = utf8(segments[3])?;
    let mut directory = String::new();
    for s in &segments[4..n - 1] {
        directory += &utf8(s)?;
        directory.push('/');
    }
    if directory.split('/').any(|s| s == "." || s == "..") {
        return rejected(format!("the directory of {url:?} has a segment . or .."));
    }
    Ok(Some(HubRepo {
        endpoint: endpoint.to_string(),
        kind,
        owner: segments[0].to_string(),
        name: segments[1].to_string(),
        revision,
        directory,
    }))
}

/// Whether the file input `url` would be a Hub store if it ended in `/` (spec/zarrify.md §1.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>):
/// a web page, rejected without being read.
pub fn hub_page(url: &str, endpoint: &str) -> bool {
    let p = refkv_vzip::uri::split(url);
    if p.query.is_some() || p.fragment.is_some() || url.ends_with('/') {
        return false;
    }
    matches!(hub_repo(&format!("{url}/"), endpoint), Ok(Some(_)))
}

/// The commit `C` of a Hub revision's response (spec/zarrify.md §1.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>), or a rejection: a gated
/// repository's too.
pub fn parse_hub_revision(body: &[u8]) -> Result<String, Fail> {
    let doc = refkv_ir::json::parse(body).map_err(Fail::Rejected)?.value;
    if !doc.is_obj() {
        return rejected("the Hub revision is not a JSON object");
    }
    let sha = doc.get("sha").and_then(|v| match v {
        Jv::Str(s) => s.as_str(),
        _ => None,
    });
    let Some(sha) = sha.filter(|s| s.len() == 40 && s.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))) else {
        return rejected(format!("the Hub revision's sha {} is not 40 lowercase hex digits", doc.get("sha").map_or("None".into(), |v| v.short(80))));
    };
    match doc.get("gated") {
        None | Some(Jv::Bool(false)) => Ok(sha.to_string()),
        Some(g) => rejected(format!("the Hub repository is gated ({}): its files are not read anonymously", g.short(40))),
    }
}

/// The objects (path, size) of one page of a Hub tree listing (spec/zarrify.md §1.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>), its files
/// under the directory `prefix` (its directories list nothing), and its number of entries.
pub fn parse_hub_tree(body: &[u8], prefix: &str) -> Result<(Vec<(String, u64)>, usize), Fail> {
    let doc = refkv_ir::json::parse(body).map_err(Fail::Rejected)?.value;
    let Some(entries) = doc.items() else {
        return rejected("the Hub tree listing is not a JSON array");
    };
    let mut out = Vec::new();
    for e in entries {
        if !e.is_obj() {
            return rejected("a Hub tree entry is not a JSON object");
        }
        let kind = match e.get("type").and_then(Jv::as_str) {
            Some(k @ ("file" | "directory")) => k,
            _ => return rejected(format!("a Hub tree entry's type {} is neither file nor directory", e.get("type").map_or("None".into(), |v| v.short(40)))),
        };
        let path = match e.get("path") {
            Some(Jv::Str(s)) => s,
            _ => return rejected(format!("the Hub tree entry {} is not under {prefix:?}", e.get("path").map_or("None".into(), |v| v.short(200)))),
        };
        let Some(path) = path.as_str() else {
            return rejected(format!("the Hub tree entry {} is not Unicode", path.raw.chars().take(200).collect::<String>()));
        };
        if !path.starts_with(prefix) || path.len() == prefix.len() {
            return rejected(format!("the Hub tree entry {:?} is not under {prefix:?}", path.chars().take(200).collect::<String>()));
        }
        if kind == "file" {
            let Some(size) = e.get("size").and_then(|v| v.int(0, (1 << 53) - 1)) else {
                return rejected(format!("the Hub tree file {path:?} has no size from 0 to 2^53 - 1"));
            };
            out.push((path.to_string(), size as u64));
        }
    }
    Ok((out, entries.len()))
}

fn link_ws(b: u8) -> bool {
    matches!(b, b' ' | b'\t' | b'\r' | b'\n')
}

/// A link: its URI reference, and its parameters (name, value).
type Link = (String, Vec<(String, String)>);

/// The links of a `Link` field value (spec/zarrify.md §1.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>): (URI reference, parameters), or a
/// rejection.
fn links(value: &str) -> Result<Vec<Link>, Fail> {
    let b = value.as_bytes();
    let n = b.len();
    let short: String = value.chars().take(200).collect();
    let bad = |why: &str| rejected(format!("the Link header {short:?} {why}"));
    let mut i = 0;
    let ws = |i: &mut usize| {
        while *i < n && link_ws(b[*i]) {
            *i += 1;
        }
    };
    let mut out = Vec::new();
    loop {
        ws(&mut i);
        if i >= n || b[i] != b'<' {
            return bad("is not a list of links");
        }
        let Some(end) = value[i..].find('>').map(|e| i + e) else {
            return bad("has an unterminated <");
        };
        let target = &value[i + 1..end];
        if !refkv_vzip::uri::is_uri_reference(target) {
            return rejected(format!("the Link header's target {:?} is not a URI reference", target.chars().take(200).collect::<String>()));
        }
        i = end + 1;
        let mut params = Vec::new();
        loop {
            ws(&mut i);
            if i >= n || b[i] != b';' {
                break;
            }
            i += 1;
            ws(&mut i);
            let start = i;
            while i < n && (b[i].is_ascii_alphanumeric() || b[i] == b'-') {
                i += 1;
            }
            if i == start {
                return bad("has a bad parameter");
            }
            let name = value[start..i].to_string();
            ws(&mut i);
            if i >= n || b[i] != b'=' {
                return bad("has a parameter without =");
            }
            i += 1;
            ws(&mut i);
            let param = if i < n && b[i] == b'"' {
                let Some(close) = value[i + 1..].find('"').map(|c| i + 1 + c) else {
                    return bad("has an unterminated quote");
                };
                let v = value[i + 1..close].to_string();
                i = close + 1;
                v
            } else {
                let start = i;
                while i < n && !link_ws(b[i]) && !matches!(b[i], b';' | b',' | b'"') {
                    i += 1;
                }
                if i == start {
                    return bad("has a parameter without a value");
                }
                value[start..i].to_string()
            };
            params.push((name, param));
        }
        out.push((target.to_string(), params));
        if i >= n {
            return Ok(out);
        }
        if b[i] != b',' {
            return bad("is not a list of links");
        }
        i += 1;
    }
}

/// The next link of a Hub tree response whose `Link` fields are `fields` and which `url`
/// answered, resolved against `url` (spec/zarrify.md §1.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>); None if it has none; a rejection if
/// the fields are not links, give two next links, or one off `endpoint`'s origin.
pub fn next_link(fields: &[String], url: &str, endpoint: &str) -> Result<Option<String>, Fail> {
    if fields.is_empty() {
        return Ok(None);
    }
    let found: Vec<String> = links(&fields.join(","))?
        .into_iter()
        .filter(|(_, params)| {
            params.iter().any(|(k, v)| k.eq_ignore_ascii_case("rel") && v.to_ascii_lowercase().split(link_ws_char).any(|t| t == "next"))
        })
        .map(|(t, _)| t)
        .collect();
    if found.len() > 1 {
        return rejected("the Hub tree response has more than one next link");
    }
    let Some(target) = found.first() else { return Ok(None) };
    let new = refkv_vzip::uri::resolve(url, target).map_err(Fail::Rejected)?;
    let (a, e) = (refkv_vzip::uri::split(&new), refkv_vzip::uri::split(endpoint));
    let same = |x: Option<&str>, y: Option<&str>| x.unwrap_or("").eq_ignore_ascii_case(y.unwrap_or(""));
    if !same(a.scheme, e.scheme) || !same(a.authority, e.authority) {
        return rejected(format!("the Hub tree's next link {:?} is not on {endpoint:?}", new.chars().take(200).collect::<String>()));
    }
    Ok(Some(new))
}

fn link_ws_char(c: char) -> bool {
    c.is_ascii() && link_ws(c as u8)
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
            let (_, _, body) = store.get_s3(&format!("{endpoint}{q}"))?;
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

    /// A Hugging Face Hub repository at a revision, `repo` (spec/zarrify.md §1.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>): the revision
    /// resolved once to a commit, the tree listed at that commit, and the files read at the store's
    /// base (`location`, and `listing.url` unless renamed), which names the commit.
    pub fn open_hub(url: &str, repo: &HubRepo, client: Arc<Client>, workers: usize, max_objects: Option<usize>) -> Result<HttpStore, Fail> {
        let mut store = HttpStore { client, location: url.to_string(), listing: Listing::new(url, workers) };
        let (_, _, body) = store.get_hub(&repo.revision_url())?;
        let commit = parse_hub_revision(&body)?;
        let base = repo.base(&commit);
        store.location = base.clone();
        store.listing.url = base;
        let mut requested = HashSet::new();
        let mut link = Some(repo.tree_url(&commit));
        while let Some(l) = link {
            requested.insert(l.clone());
            let (answered, fields, body) = store.get_hub(&l)?;
            let (listed, entries) = parse_hub_tree(&body, &repo.directory)?;
            store.listing.add(listed, &repo.directory)?;
            if max_objects.is_some_and(|m| store.listing.listed > m) {
                return Err(Fail::Other(format!("the store lists more than {} objects", max_objects.unwrap())));
            }
            link = next_link(&fields, &answered, &repo.endpoint)?;
            if link.is_some() && entries == 0 {
                // a page that lists nothing could be followed forever
                return rejected("a Hub tree page with a next link lists nothing");
            }
            if let Some(l) = link.as_ref().filter(|l| requested.contains(*l)) {
                return rejected(format!("the Hub tree's next link {:?} was requested already", l.chars().take(200).collect::<String>()));
            }
        }
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
        for hop in 0..6 {
            self.client.policy.check(&self.location, &url).map_err(|e| Failure::Fatal(Fail::Refused(e)))?;
            let r = self.client.get(&url, headers, |_, _| true).map_err(|e| match e {
                http::SendError::Refused(m) => Failure::Fatal(Fail::Refused(m)),
                http::SendError::Network(m) => Failure::Network(m),
            })?;
            if !http::REDIRECTS.contains(&r.status) {
                return Ok((url, r));
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
    /// and named `url` in the output if given): a Hub store when it is under the Hub's endpoint
    /// `hub` ([`hub_endpoint`]), else an S3-style one. Or a local directory served at `url`.
    pub fn open(location: &str, url: Option<&str>, client: &Arc<Client>, workers: usize, hub: &str) -> Result<Store, Fail> {
        if location.starts_with("http://") || location.starts_with("https://") {
            let mut s = match hub_repo(location, hub)? {
                Some(repo) => HttpStore::open_hub(location, &repo, client.clone(), workers, None)?,
                None => HttpStore::open(location, client.clone(), workers, None)?,
            };
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
    const SHA: &str = "1bae0e1088993b9b82de08f59de5be5a34e42170";

    fn hub(url: &str) -> Result<Option<HubRepo>, Fail> {
        hub_repo(url, HUB)
    }

    #[test]
    fn hub_stores() {
        assert_eq!(hub_endpoint(None).unwrap(), HUB);
        assert_eq!(hub_endpoint(Some("http://127.0.0.1:9/hub/")).unwrap(), "http://127.0.0.1:9/hub");
        let r = hub("https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash-1017/tree/main/").unwrap().unwrap();
        assert_eq!((r.kind, r.revision.as_str(), r.directory.as_str()), ("models", "main", ""));
        assert_eq!(r.revision_url(), "https://huggingface.co/api/models/deepseek-ai/DeepSeek-V4.1-Flash-1017/revision/main?expand%5B%5D=sha&expand%5B%5D=gated");
        assert_eq!(r.tree_url(SHA), format!("https://huggingface.co/api/models/deepseek-ai/DeepSeek-V4.1-Flash-1017/tree/{SHA}?recursive=true"));
        assert_eq!(r.base(SHA), format!("https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash-1017/resolve/{SHA}/"));
        let r = hub("HTTPS://HuggingFace.co/datasets/o/r/tree/refs%2Fpr%2F1/a%20b/c/").unwrap().unwrap();
        assert_eq!((r.kind, r.revision.as_str(), r.directory.as_str()), ("datasets", "refs/pr/1", "a b/c/"));
        assert_eq!(r.revision_url(), "https://huggingface.co/api/datasets/o/r/revision/refs%2Fpr%2F1?expand%5B%5D=sha&expand%5B%5D=gated");
        assert_eq!(r.tree_url(SHA), format!("https://huggingface.co/api/datasets/o/r/tree/{SHA}/a%20b/c?recursive=true"));
        assert_eq!(r.base(SHA), format!("https://huggingface.co/datasets/o/r/resolve/{SHA}/a%20b/c/"));
        let r = hub_repo("http://127.0.0.1:9/hub/f/fixtures/tree/main/n5/x/", "http://127.0.0.1:9/hub").unwrap().unwrap();
        assert_eq!((r.owner.as_str(), r.name.as_str(), r.directory.as_str()), ("f", "fixtures", "n5/x/"));
        assert!(hub("https://s3.amazonaws.com/b/").unwrap().is_none());
        assert!(hub_repo("http://127.0.0.1:9/f/x/", "http://127.0.0.1:9/hub").unwrap().is_none());
        assert!(hub_page("https://huggingface.co/o/r/tree/main", HUB));
        assert!(!hub_page("https://huggingface.co/o/r/resolve/main/x.bin", HUB));
        assert!(!hub_page("https://huggingface.co/o/r/tree/main?x", HUB));
        // a recorded page (huggingface.co's, shortened): directories list nothing, sizes are the files'
        let page = br#"[{"type":"directory","oid":"0e4b","size":0,"path":"inference/examples"},
            {"type":"file","oid":"a8a0","size":1594,"path":"inference/README.md"},
            {"type":"file","oid":"3b02","size":212495,"lfs":{"oid":"5df8","size":212495,"pointerSize":131},"path":"inference/examples/images/carrots.jpeg"},
            {"type":"file","size":0,"path":"inference/empty"}]"#;
        let (listed, entries) = parse_hub_tree(page, "inference/").unwrap();
        assert_eq!(entries, 4);
        assert_eq!(listed, vec![("inference/README.md".into(), 1594), ("inference/examples/images/carrots.jpeg".into(), 212495), ("inference/empty".into(), 0)]);
        assert_eq!(parse_hub_revision(format!(r#"{{"_id":"x","id":"o/r","sha":"{SHA}","gated":false}}"#).as_bytes()).unwrap(), SHA);
        assert_eq!(parse_hub_revision(format!(r#"{{"sha":"{SHA}"}}"#).as_bytes()).unwrap(), SHA);
        let api = "https://huggingface.co/api/models/o/r/tree/x?recursive=true";
        assert_eq!(next_link(&[], api, HUB).unwrap(), None);
        assert_eq!(next_link(&[r#"<https://huggingface.co/api/models/o/r/xet>; rel="xet-auth""#.into()], api, HUB).unwrap(), None);
        assert_eq!(
            next_link(&[r#"<https://huggingface.co/api/models/o/r/tree/x?recursive=true&cursor=ab>; rel="next""#.into()], api, HUB).unwrap().unwrap(),
            "https://huggingface.co/api/models/o/r/tree/x?recursive=true&cursor=ab"
        );
        assert_eq!(
            next_link(&["<a>; title=\"x, y\" ; REL = \"prev  Next\"".into(), "</z>;rel=last".into()], api, "HTTPS://HUGGINGFACE.CO").unwrap().unwrap(),
            "https://huggingface.co/api/models/o/r/tree/a"
        );
    }

    #[test]
    fn rejects_a_hub_url_without_a_revision() {
        assert!(matches!(hub("https://huggingface.co/o/r/"), Err(Fail::Rejected(_))));
    }

    #[test]
    fn rejects_a_hub_space() {
        assert!(matches!(hub("https://huggingface.co/spaces/o/r/tree/main/"), Err(Fail::Rejected(_))));
    }

    #[test]
    fn rejects_a_hub_name_that_is_a_dot_segment() {
        assert!(matches!(hub("https://huggingface.co/o/../tree/main/"), Err(Fail::Rejected(_))));
    }

    #[test]
    fn rejects_a_hub_directory_with_a_dot_segment() {
        assert!(matches!(hub("https://huggingface.co/o/r/tree/main/a/%2E%2E/"), Err(Fail::Rejected(_))));
    }

    #[test]
    fn rejects_a_hub_revision_that_is_not_utf8() {
        assert!(matches!(hub("https://huggingface.co/o/r/tree/%FF/"), Err(Fail::Rejected(_))));
    }

    #[test]
    fn rejects_a_hub_endpoint_with_a_query() {
        assert!(matches!(hub_endpoint(Some("https://h/?x")), Err(Fail::Other(_))));
    }

    #[test]
    fn rejects_a_hub_revision_that_is_not_json() {
        assert!(matches!(parse_hub_revision(b"<html>"), Err(Fail::Rejected(_))));
    }

    #[test]
    fn rejects_a_short_sha() {
        assert!(matches!(parse_hub_revision(br#"{"sha":"1bae0e1"}"#), Err(Fail::Rejected(_))));
    }

    #[test]
    fn rejects_a_gated_repository() {
        assert!(matches!(parse_hub_revision(format!(r#"{{"sha":"{SHA}","gated":"manual"}}"#).as_bytes()), Err(Fail::Rejected(_))));
    }

    #[test]
    fn rejects_a_tree_that_is_not_an_array() {
        assert!(matches!(parse_hub_tree(br#"{"error":"x"}"#, ""), Err(Fail::Rejected(_))));
    }

    #[test]
    fn rejects_a_tree_entry_of_another_type() {
        assert!(matches!(parse_hub_tree(br#"[{"type":"submodule","path":"a"}]"#, ""), Err(Fail::Rejected(_))));
    }

    #[test]
    fn rejects_a_tree_entry_outside_the_directory() {
        assert!(matches!(parse_hub_tree(br#"[{"type":"file","size":1,"path":"b/x"}]"#, "a/"), Err(Fail::Rejected(_))));
    }

    #[test]
    fn rejects_a_tree_entry_that_is_the_directory() {
        assert!(matches!(parse_hub_tree(br#"[{"type":"directory","path":"a/"}]"#, "a/"), Err(Fail::Rejected(_))));
    }

    #[test]
    fn rejects_a_tree_path_with_a_lone_surrogate() {
        assert!(matches!(parse_hub_tree(br#"[{"type":"file","size":1,"path":"a\ud800"}]"#, ""), Err(Fail::Rejected(_))));
    }

    #[test]
    fn rejects_a_file_without_a_size() {
        assert!(matches!(parse_hub_tree(br#"[{"type":"file","path":"a"}]"#, ""), Err(Fail::Rejected(_))));
    }

    #[test]
    fn rejects_a_negative_size() {
        assert!(matches!(parse_hub_tree(br#"[{"type":"file","size":-1,"path":"a"}]"#, ""), Err(Fail::Rejected(_))));
    }

    #[test]
    fn rejects_a_size_beyond_2_53() {
        assert!(matches!(parse_hub_tree(br#"[{"type":"file","size":9007199254740992,"path":"a"}]"#, ""), Err(Fail::Rejected(_))));
    }

    #[test]
    fn rejects_a_link_header_that_is_not_links() {
        assert!(matches!(next_link(&["https://h/x; rel=next".into()], "https://huggingface.co/", HUB), Err(Fail::Rejected(_))));
    }

    #[test]
    fn rejects_a_link_parameter_without_a_value() {
        assert!(matches!(next_link(&["<x>; rel=".into()], "https://huggingface.co/", HUB), Err(Fail::Rejected(_))));
    }

    #[test]
    fn rejects_two_next_links() {
        assert!(matches!(next_link(&["<a>; rel=next".into(), "<b>; rel=next".into()], "https://huggingface.co/", HUB), Err(Fail::Rejected(_))));
    }

    #[test]
    fn rejects_a_next_link_off_the_endpoint() {
        assert!(matches!(next_link(&["<https://evil.test/x>; rel=next".into()], "https://huggingface.co/", HUB), Err(Fail::Rejected(_))));
    }
}
