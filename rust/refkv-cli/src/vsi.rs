//! An input read with the files beside it: a VSI input (spec/zarrify/vsi.md §9.2
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#92-the-tile-files>), the `.vsi`
//! file named by the input and the tile files beside it, and a NRRD input (spec/zarrify/nrrd.md
//! §8.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#83-the-data-file>), the
//! header file named by the input and a detached header's data file. The core's `vsi` and `nrrd`
//! store formats ask for those files by key (the probes of `refkv_ir::store::StoreRun`) rather
//! than from a listing. The files are the objects of a store whose URL is the named file's
//! directory, each read through a transport of its own (`transport.rs`); a probe opens the file,
//! and one that does not exist (a missing local file, an HTTP 404) is absent. A LAS X project's files
//! (spec/zarrify/lif.md §8 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/lif.md#8-las-x-documents>)
//! are named in another case than they have, so a local directory's are found by listing it. The twin of
//! python/src/refkv/zarrify/store/beside.py.

use crate::http::Client;
use crate::run::{concurrency, map};
use crate::store::{check_store_url, object_url};
use crate::transport::Transport;
use crate::Fail;
use refkv_ir::out::Out;
use refkv_ir::store::{StoreFormat, StoreRun};
use std::collections::HashMap;
use std::path::{Component, Path, PathBuf};
use std::sync::Arc;

/// (the directory, the named file's key) of an input of the format `title` names (VSI, NRRD):
/// for an http(s) URL the URL up to its last `/` and the last segment, percent-decoded; for a
/// local path, its directory (empty for a bare name) and its file name.
pub fn split_input(location: &str, title: &str) -> Result<(String, String), Fail> {
    if location.starts_with("http://") || location.starts_with("https://") {
        if location.contains('?') || location.contains('#') {
            return Err(Fail::Rejected(format!("{title} input {location:?} has a query or a fragment")));
        }
        let k = location.rfind('/').unwrap_or(0);
        let name = &location[k + 1..];
        let key = String::from_utf8(crate::store::percent_decode(name)).map_err(|_| Fail::Rejected(format!("{title} input {location:?}: its file name is not percent-encoded UTF-8")))?;
        if key.is_empty() || key.contains('/') {
            return Err(Fail::Rejected(format!("{title} input {location:?} names no file")));
        }
        return Ok((location[..=k].to_string(), key));
    }
    // as Python's os.path.split: the directory (empty for a bare name), and the name
    let (dir, name) = match location.rfind(['/', std::path::MAIN_SEPARATOR]) {
        Some(k) => (&location[..k], &location[k + 1..]),
        None => ("", location),
    };
    if name.is_empty() {
        return Err(Fail::Other(format!("{location}: not a file name")));
    }
    Ok((dir.to_string(), name.to_string()))
}

/// `path` with its `.` and `..` components taken out, as Python's `os.path.normpath` does.
fn lexical(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for c in path.components() {
        match c {
            Component::CurDir => {}
            Component::ParentDir => {
                if !out.pop() {
                    out.push(c);
                }
            }
            c => out.push(c),
        }
    }
    out
}

/// The scope of the file input at `location` (spec/zarrify.md §1.9
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#19-sources-beside-the-input>,
/// **Scope**) as the keys `StoreRun::scope` takes: each entry is a path or URL written as
/// `location` is (local paths, or http(s) URLs), the input's directory or a file or directory
/// below it. A directory's key ends in `/`; the input itself, always in scope, adds no key. None
/// when the scope holds the input's directory, every key being in it. The twin of
/// python/src/refkv/zarrify/store/beside.py's `scope_keys`.
pub fn scope_keys(location: &str, scope: &[String]) -> Result<Option<Vec<String>>, Fail> {
    let outside = |e: &str, d: &str| Fail::Usage(format!("scope \"{e}\" is not the input's directory \"{d}\" or below it"));
    let mut keys = Vec::new();
    if location.starts_with("http://") || location.starts_with("https://") {
        let (d, named) = split_input(location, "the")?;
        for e in scope {
            let rest = e.strip_prefix(d.as_str()).ok_or_else(|| outside(e, &d))?;
            let key = String::from_utf8(crate::store::percent_decode(rest)).map_err(|_| outside(e, &d))?;
            if key.is_empty() {
                return Ok(None);
            }
            if key != named {
                keys.push(key);
            }
        }
        return Ok(Some(keys));
    }
    let absolute = |p: &str| std::path::absolute(p).map(|a| lexical(&a)).map_err(|e| Fail::Other(format!("{p}: {e}")));
    let named = absolute(location)?;
    let d = named.parent().map(Path::to_path_buf).unwrap_or_default();
    for e in scope {
        let at = absolute(e)?;
        let rel = at.strip_prefix(&d).map_err(|_| outside(e, &d.to_string_lossy()))?;
        if rel.as_os_str().is_empty() {
            return Ok(None);
        }
        if at != named {
            let parts: Vec<String> = rel.components().map(|c| c.as_os_str().to_string_lossy().into_owned()).collect();
            keys.push(parts.join("/") + if at.is_dir() { "/" } else { "" });
        }
    }
    Ok(Some(keys))
}

/// The files of a VSI or NRRD input: the named file, then every file found beside it, each with
/// its key, size and transport.
pub struct Files {
    /// where the files are: the directory's URL, or the local directory (possibly empty)
    location: String,
    /// the store URL the output's object URLs are made from
    pub url: String,
    remote: bool,
    client: Arc<Client>,
    keys: Vec<String>,
    transports: Vec<Transport>,
    pub sizes: HashMap<String, u64>,
    /// the files opened
    pub requests: u64,
    /// the format's title, which a rejection names
    title: &'static str,
    /// a local directory's files are found by listing it (spec/zarrify.md §1.9
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#19-sources-beside-the-input>, **Case**)
    case: bool,
    /// the directories listed, by key
    listed: HashMap<String, Option<Vec<String>>>,
}

impl Files {
    /// The input at `location` of the store format `format`, its named file opened as `t`; `url`,
    /// when given, is the URL the named file is named by, whose directory is the store URL.
    pub fn open(location: &str, url: Option<&str>, t: Transport, client: &Arc<Client>, format: StoreFormat) -> Result<Files, Fail> {
        let (title, noun) = (format.title(), format.named().unwrap_or("file"));
        let (dir, key) = split_input(location, title)?;
        let remote = t.remote();
        let url = match url {
            // the URL the named file is named by (--url): the store URL is its directory's
            Some(u) => {
                let (d, name) = split_input(u, title)?;
                check_store_url(&d)?;
                if name != key {
                    return Err(Fail::Rejected(format!("--url {u:?} does not name the {noun} {key:?}")));
                }
                d
            }
            None if remote => dir.clone(),
            None if dir.is_empty() => String::new(),
            None => {
                let r = refkv_vzip::uri::path_reference(&dir).map_err(Fail::Other)?;
                if r.ends_with('/') { r } else { format!("{r}/") }
            }
        };
        let mut sizes = HashMap::new();
        sizes.insert(key.clone(), t.size());
        let case = format.case() && !remote;
        Ok(Files { location: dir, url, remote, client: client.clone(), keys: vec![key], transports: vec![t], sizes, requests: 0, title, case,
                   listed: HashMap::new() })
    }

    /// The path of the file or directory at `key`.
    fn path(&self, key: &str) -> std::path::PathBuf {
        let mut p = std::path::PathBuf::from(if self.location.is_empty() { "." } else { self.location.as_str() });
        for seg in key.split('/').filter(|s| !s.is_empty()) {
            p.push(seg);
        }
        p
    }

    /// Whether the local file or directory at `key` is below the named file's directory, symbolic
    /// links followed (spec/zarrify.md §1.9
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#19-sources-beside-the-input>).
    fn below(&self, key: &str) -> bool {
        match (self.path(key).canonicalize(), self.path("").canonicalize()) {
            (Ok(p), Ok(root)) => p.starts_with(root),
            _ => false,
        }
    }

    /// The entries of the local directory at the key `key`, or None when there is none.
    fn listdir(&mut self, key: &str) -> Option<Vec<String>> {
        if let Some(l) = self.listed.get(key) {
            return l.clone();
        }
        let got = self.below(key).then(|| std::fs::read_dir(self.path(key)).ok()).flatten().map(|d| {
            d.filter_map(|e| e.ok().and_then(|e| e.file_name().into_string().ok())).collect::<Vec<String>>()
        });
        self.listed.insert(key.to_string(), got.clone());
        got
    }

    /// The size and key of the file `written` once opened, or None when it does not exist.
    fn probe(&mut self, written: &str) -> Result<Option<(u64, String)>, Fail> {
        let found = if self.case {
            match refkv_ir::beside::found_key(written, |d| self.listdir(d)) {
                Ok(Some(k)) => k,
                Ok(None) => return Ok(None),
                Err((a, b)) => return Err(Fail::Rejected(format!("the file \"{written}\" matches both \"{a}\" and \"{b}\""))),
            }
        } else {
            written.to_string()
        };
        let key = found.as_str();
        if let Some(&n) = self.sizes.get(key) {
            return Ok(Some((n, found)));
        }
        let t = if self.remote {
            match Transport::open(&object_url(&self.location, key), &self.client) {
                Ok(t) => t,
                Err(Fail::Other(m)) if m.contains(" HTTP 404 ") => return Ok(None),
                Err(e) => return Err(e),
            }
        } else {
            let p = self.path(key);
            if !p.is_file() || !self.below(key) {
                return Ok(None);
            }
            Transport::open(p.to_str().ok_or_else(|| Fail::Other(format!("{key}: not a path")))?, &self.client)?
        };
        self.requests += 1;
        let n = t.size();
        self.keys.push(key.to_string());
        self.transports.push(t);
        self.sizes.insert(key.to_string(), n);
        Ok(Some((n, found)))
    }

    pub fn remote(&self) -> bool {
        self.remote
    }

    /// The bytes [offset, offset + length) of the file `key`.
    pub fn read_range(&self, key: &str, offset: u64, length: u64) -> Result<Vec<u8>, Fail> {
        let i = self.keys.iter().position(|k| k == key).ok_or_else(|| Fail::Other(format!("{key}: not a file of the {} input", self.title)))?;
        if length == 0 {
            return Ok(Vec::new());
        }
        let data = self.transports[i].get(offset, offset + length)?;
        if data.len() as u64 != length {
            return Err(Fail::Other(format!("{key}: read {} bytes at {offset}, not {length}", data.len())));
        }
        Ok(data)
    }
}

/// (the output, the files) of the VSI or NRRD input at `location` (`format`), its named file
/// opened as `t`: a store run of the core's `format` over the files, probed as the run asks for
/// them, read `connections` at a time when remote; sealed for ranges that carry checksums when
/// `checksums`. `scope`, when given, is the keys of the input's scope (`scope_keys`): no file
/// outside it is opened.
pub fn zarrify_vsi(location: &str, url: Option<&str>, t: Transport, client: &Arc<Client>, connections: Option<usize>,
                   format: StoreFormat, checksums: bool, scope: Option<Vec<String>>) -> Result<(Out, Files), Fail> {
    let workers = concurrency(&t, connections)?;
    let mut files = Files::open(location, url, t, client, format)?;
    let listing = vec![(files.keys[0].clone(), files.sizes[&files.keys[0]])];
    let mut run = StoreRun::new(format, listing, workers).map_err(Fail::Rejected)?;
    if let Some(within) = scope {
        run.scope(within).map_err(Fail::Rejected)?;
    }
    while let Some(reqs) = run.poll().map_err(Fail::Rejected)? {
        if reqs.is_empty() {
            // a batch of probes: each key answered with its size, or None when it does not exist
            let keys = run.probes();
            for k in keys {
                match files.probe(&k)? {
                    Some((size, key)) if key != k => run.found_as(size, &key),
                    Some((size, _)) => run.found(Some(size)),
                    None => run.found(None),
                }
                .map_err(Fail::Rejected)?;
            }
            continue;
        }
        let results = map(&reqs, if files.remote() { workers } else { 1 }, |q| {
            q.spans.iter().map(|&(a, b)| files.read_range(&q.key, a, b - a)).collect::<Result<Vec<_>, Fail>>()
        });
        for (k, parts) in results.into_iter().enumerate() {
            run.complete(k, parts?).map_err(Fail::Rejected)?;
        }
    }
    let mut out = run.into_output(&files.url, true, checksums)?;
    if let Some(m) = out.summary.as_object_mut() {
        m.insert("listingRequests".into(), files.requests.into());
    }
    Ok((out, files))
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::os::unix::fs::symlink;
    use std::time::Duration;

    /// A directory with a named file, its files and links, beside a directory with a secret.
    fn upload(name: &str) -> PathBuf {
        let root = std::env::temp_dir().join(format!("refkv-cli-beside-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let (outside, upload) = (root.join("outside"), root.join("upload"));
        std::fs::create_dir_all(&outside).unwrap();
        std::fs::create_dir_all(upload.join("sub")).unwrap();
        std::fs::write(outside.join("secret.bin"), b"secret").unwrap();
        std::fs::write(upload.join("a.nhdr"), b"named").unwrap();
        std::fs::write(upload.join("plain.bin"), b"plain").unwrap();
        std::fs::write(upload.join("sub/plain.bin"), b"plain").unwrap();
        symlink("plain.bin", upload.join("link_inside.bin")).unwrap();
        symlink("sub", upload.join("dir_inside")).unwrap();
        symlink(outside.join("secret.bin"), upload.join("link_outside.bin")).unwrap();
        symlink(&outside, upload.join("dir_outside")).unwrap();
        symlink(&upload, root.join("linked")).unwrap();
        upload
    }

    fn files(dir: &Path, format: StoreFormat) -> Files {
        let client = Client::new(crate::policy::Policy::default(), Duration::from_secs(5));
        let named = dir.join("a.nhdr");
        let named = named.to_str().unwrap();
        let t = Transport::open(named, &client).ok().unwrap();
        Files::open(named, None, t, &client, format).ok().unwrap()
    }

    fn found(files: &mut Files, key: &str) -> Option<u64> {
        files.probe(key).ok().unwrap().map(|(n, _)| n)
    }

    #[test]
    fn a_sibling_below_the_directory_is_found() {
        let dir = upload("found");
        for named in [dir.clone(), dir.parent().unwrap().join("linked")] {
            for format in [StoreFormat::Nrrd, StoreFormat::Lif] {
                for key in ["plain.bin", "sub/plain.bin", "link_inside.bin", "dir_inside/plain.bin"] {
                    assert_eq!(found(&mut files(&named, format), key), Some(5), "{key} from {named:?}");
                }
            }
        }
    }

    #[test]
    fn a_link_to_a_file_outside_is_absent() {
        let dir = upload("file");
        for format in [StoreFormat::Nrrd, StoreFormat::Lif] {
            assert_eq!(found(&mut files(&dir, format), "link_outside.bin"), None);
        }
    }

    #[test]
    fn a_key_through_a_link_to_a_directory_outside_is_absent() {
        let dir = upload("key");
        for format in [StoreFormat::Nrrd, StoreFormat::Lif] {
            assert_eq!(found(&mut files(&dir, format), "dir_outside/secret.bin"), None);
        }
    }

    #[test]
    fn a_directory_outside_is_not_listed() {
        let dir = upload("list");
        assert_eq!(files(&dir, StoreFormat::Lif).listdir("dir_outside"), None);
    }

    #[test]
    fn a_detached_header_whose_data_file_is_out_of_scope_is_rejected() {
        let client = Client::new(crate::policy::Policy::default(), Duration::from_secs(5));
        let named = concat!(env!("CARGO_MANIFEST_DIR"), "/../../fixtures/nrrd/nrrd_detached_raw.nhdr");
        let t = Transport::open(named, &client).ok().unwrap();
        let Err(Fail::Rejected(m)) = zarrify_vsi(named, None, t, &client, None, StoreFormat::Nrrd, false, Some(Vec::new())) else {
            panic!("not rejected");
        };
        assert_eq!(m, "out of scope: the input names the file \"nrrd_detached_raw.raw\"");
    }

    #[test]
    fn a_scope_gives_keys_below_the_input_directory() {
        let dir = upload("scope");
        let at = |p: &str| dir.join(p).to_string_lossy().into_owned();
        let named = at("a.nhdr");
        let cases: &[(Vec<String>, Option<Vec<&str>>)] = &[
            (vec![named.clone()], Some(vec![])),
            (vec![], Some(vec![])),
            (vec![at("plain.bin"), at("sub"), at("sub/../plain.bin")], Some(vec!["plain.bin", "sub/", "plain.bin"])),
            (vec![at("plain.bin"), dir.to_string_lossy().into_owned()], None),
            (vec![at("sub/..")], None),
        ];
        for (scope, want) in cases {
            let got = scope_keys(&named, scope).ok().unwrap();
            assert_eq!(got, want.as_ref().map(|w| w.iter().map(|s| s.to_string()).collect::<Vec<_>>()), "{scope:?}");
        }
        let url = scope_keys("https://h/d/a.nhdr", &["https://h/d/x%20y".into(), "https://h/d/s/".into()]).ok().unwrap();
        assert_eq!(url, Some(vec!["x y".to_string(), "s/".to_string()]));
    }

    #[test]
    fn a_scope_outside_the_input_directory_is_refused() {
        let dir = upload("outside-scope");
        let named = dir.join("a.nhdr").to_string_lossy().into_owned();
        let outside = dir.parent().unwrap().join("outside").to_string_lossy().into_owned();
        assert!(matches!(scope_keys(&named, &[outside]), Err(Fail::Usage(_))));
        assert!(matches!(scope_keys("https://h/d/a.nhdr", &["https://h/e/b".into()]), Err(Fail::Usage(_))));
    }
}
