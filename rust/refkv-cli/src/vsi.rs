//! An input read with the files beside it: a VSI input (spec/zarrify/vsi.md §9.2
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#92-the-tile-files>), the `.vsi`
//! file named by the input and the tile files beside it, and a NRRD input (spec/zarrify/nrrd.md
//! §8.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#83-the-data-file>), the
//! header file named by the input and a detached header's data file. The core's `vsi` and `nrrd`
//! store formats ask for those files by key (the probes of `refkv_ir::store::StoreRun`) rather
//! than from a listing. The files are the objects of a store whose URL is the named file's
//! directory, each read through a transport of its own (`transport.rs`); a probe opens the file,
//! and one that does not exist (a missing local file, an HTTP 404) is absent. The twin of
//! python/src/refkv/zarrify/vsi.py.

use crate::http::Client;
use crate::run::{concurrency, map};
use crate::store::{check_store_url, object_url};
use crate::transport::Transport;
use crate::Fail;
use refkv_ir::out::Out;
use refkv_ir::store::{StoreFormat, StoreRun};
use std::collections::HashMap;
use std::sync::Arc;

/// The title a rejection names the input's format by, and its named file: a VSI or NRRD input.
fn titles(format: StoreFormat) -> (&'static str, &'static str) {
    if format == StoreFormat::Nrrd { ("NRRD", "header file") } else { ("VSI", ".vsi file") }
}

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
}

impl Files {
    /// The input at `location` of the store format `format`, its named file opened as `t`; `url`,
    /// when given, is the URL the named file is named by, whose directory is the store URL.
    pub fn open(location: &str, url: Option<&str>, t: Transport, client: &Arc<Client>, format: StoreFormat) -> Result<Files, Fail> {
        let (title, noun) = titles(format);
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
        Ok(Files { location: dir, url, remote, client: client.clone(), keys: vec![key], transports: vec![t], sizes, requests: 0, title })
    }

    /// The size of the file `key` once opened, or None when it does not exist.
    fn probe(&mut self, key: &str) -> Result<Option<u64>, Fail> {
        if let Some(&n) = self.sizes.get(key) {
            return Ok(Some(n));
        }
        let t = if self.remote {
            match Transport::open(&object_url(&self.location, key), &self.client) {
                Ok(t) => t,
                Err(Fail::Other(m)) if m.contains(" HTTP 404 ") => return Ok(None),
                Err(e) => return Err(e),
            }
        } else {
            let mut p = std::path::PathBuf::from(if self.location.is_empty() { "." } else { self.location.as_str() });
            for seg in key.split('/') {
                p.push(seg);
            }
            if !p.is_file() {
                return Ok(None);
            }
            Transport::open(p.to_str().ok_or_else(|| Fail::Other(format!("{key}: not a path")))?, &self.client)?
        };
        self.requests += 1;
        let n = t.size();
        self.keys.push(key.to_string());
        self.transports.push(t);
        self.sizes.insert(key.to_string(), n);
        Ok(Some(n))
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
/// them, read `connections` at a time when remote.
pub fn zarrify_vsi(location: &str, url: Option<&str>, t: Transport, client: &Arc<Client>, connections: Option<usize>,
                   format: StoreFormat) -> Result<(Out, Files), Fail> {
    let workers = concurrency(&t, connections)?;
    let mut files = Files::open(location, url, t, client, format)?;
    let listing = vec![(files.keys[0].clone(), files.sizes[&files.keys[0]])];
    let mut run = StoreRun::new(format, listing, workers).map_err(Fail::Rejected)?;
    while let Some(reqs) = run.poll().map_err(Fail::Rejected)? {
        if reqs.is_empty() {
            // a batch of probes: each key answered with its size, or None when it does not exist
            let keys: Vec<String> = run.probes().to_vec();
            for k in keys {
                let size = files.probe(&k)?;
                run.found(size).map_err(Fail::Rejected)?;
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
    let mut out = run.into_output(&files.url, true).map_err(Fail::Rejected)?;
    if let Some(m) = out.summary.as_object_mut() {
        m.insert("listingRequests".into(), files.requests.into());
    }
    Ok((out, files))
}
