//! The rules of a zarrifier's host that do no I/O (spec/zarrify.md §1.2
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input> to §1.9
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#19-sources-beside-the-input>): what a location names, which
//! strings are URI references and how one resolves (`refkv_uri`), the store URL rules and the URL of each object, the S3 listing and the Hugging Face Hub's tree and
//! `Link` fields, the keys a listing ignores, the directory and scope of an input read with the
//! files beside it, the archive an output is written as, and the hosts' defaults. Each host (the
//! Python bindings, the wasm32 module, the CLI) does the I/O and calls these, so that the hosts
//! cannot disagree; the Rust functions are the API, and [`call`] gives each by name with its
//! parameters and result as JSON (`routines::call`, which the Python bindings bind as
//! `refkv._core.coding` and the wasm32 module exports as `vz_call`). The archive's layout
//! (`archive::file_layout`, `archive::store_layout`, `Layout::checksummed`) works on an output the
//! core made (`out::Out`), so it has no JSON form: a host reaches it through the binding of the
//! run that made the output; only its `late` rule and page size are given by name.

pub mod archive;
pub mod inputs;
pub mod listing;
pub mod urls;

pub use archive::{late, Layout, PAGE_SIZE};
pub use inputs::{input, is_url, scope_keys, split_input, split_url, url_scheme, Input};
pub use listing::{folder_marker, ignored, next_link, parse_hub_revision, parse_hub_tree, parse_listing, store_listing, Listing};
pub use urls::{check_store_url, hub_endpoint, hub_page, hub_repo, listing_endpoint, listing_url, object_url, query_encode, HubRepo, HUB};

use serde_json::{json, Value as J};

/// Why a host rule gave no result.
#[derive(Debug, Clone, PartialEq)]
pub enum Fail {
    /// the input is rejected (spec/zarrify.md Appendix A
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#appendix-a-conformance>)
    Rejected(String),
    /// the caller's argument is not one the rule takes (a scope outside the input's directory, a
    /// Hub endpoint that is no http(s) URL): the caller's error, not the input's
    Usage(String),
    /// the rule's parameters are not its own (a missing or mistyped member), or an output a host
    /// cannot write: a bug of the caller
    Bad(String),
}

impl std::fmt::Display for Fail {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Fail::Rejected(m) | Fail::Usage(m) | Fail::Bad(m) => f.write_str(m),
        }
    }
}

fn rejected<T>(m: impl Into<String>) -> Result<T, Fail> {
    Err(Fail::Rejected(m.into()))
}

fn usage<T>(m: impl Into<String>) -> Result<T, Fail> {
    Err(Fail::Usage(m.into()))
}

/// Concurrent object reads of a remote store, by default.
pub const WORKERS: usize = 16;
/// The bytes of a store's documents read ahead and not yet taken, at most (but always one).
pub const PREFETCH_BYTES: u64 = 64 << 20;
/// Requests at a time to a remote file's host, by default (a browser makes 6).
pub const CONNECTIONS: usize = 8;
/// The bytes the request that opens a remote file reads.
pub const OPEN: u64 = 1 << 16;
/// The User-Agent of every request.
pub const USER_AGENT: &str = "refkv-zarrify";
/// The statuses a request is retried on, and the attempts and seconds between them: 1, 2, 4, 8.
pub const RETRY: [u16; 5] = [429, 500, 502, 503, 504];
pub const ATTEMPTS: u32 = 5;
/// The redirect statuses a request follows, and how many in a row at most.
pub const REDIRECTS: [u16; 5] = [301, 302, 303, 307, 308];
pub const MAX_REDIRECTS: u32 = 5;

/// The hosts' defaults, as JSON (the rule `defaults`).
pub fn defaults() -> J {
    json!({
        "workers": WORKERS, "prefetch_bytes": PREFETCH_BYTES, "connections": CONNECTIONS, "open": OPEN,
        "user_agent": USER_AGENT, "retry": RETRY, "attempts": ATTEMPTS, "redirects": REDIRECTS,
        "max_redirects": MAX_REDIRECTS, "page_size": PAGE_SIZE, "hub": HUB,
        "max_document": crate::json::MAX_DOCUMENT, "max_safe": listing::MAX_SAFE,
    })
}

fn bad<T>(m: impl Into<String>) -> Result<T, Fail> {
    Err(Fail::Bad(m.into()))
}

fn s<'a>(p: &'a J, k: &str) -> Result<&'a str, Fail> {
    p.get(k).and_then(J::as_str).map_or_else(|| bad(format!("{k} is a string")), Ok)
}

fn opt_s<'a>(p: &'a J, k: &str) -> Result<Option<&'a str>, Fail> {
    match p.get(k) {
        None | Some(J::Null) => Ok(None),
        Some(J::String(v)) => Ok(Some(v)),
        Some(_) => bad(format!("{k} is a string or null")),
    }
}

fn flag(p: &J, k: &str) -> Result<bool, Fail> {
    match p.get(k) {
        None | Some(J::Null) => Ok(false),
        Some(J::Bool(b)) => Ok(*b),
        Some(_) => bad(format!("{k} is a bool")),
    }
}

fn strings(p: &J, k: &str) -> Result<Vec<String>, Fail> {
    let a = p.get(k).and_then(J::as_array).map_or_else(|| bad(format!("{k} is a list of strings")), Ok)?;
    a.iter().map(|v| v.as_str().map(str::to_string).map_or_else(|| bad(format!("{k} is a list of strings")), Ok)).collect()
}

/// A response's body, given as a string of the characters U+0000 to U+00FF, one per byte (a
/// `latin-1` decoding, which every host has).
fn body(p: &J) -> Result<Vec<u8>, Fail> {
    s(p, "body")?.chars().map(|c| u8::try_from(c as u32).or_else(|_| bad("body is a string of characters U+0000 to U+00FF, one per byte"))).collect()
}

fn objects(v: Vec<(String, u64)>) -> J {
    J::Array(v.into_iter().map(|(k, n)| json!([k, n])).collect())
}

fn repo_json(r: &HubRepo) -> J {
    json!({
        "endpoint": r.endpoint, "kind": r.kind, "owner": r.owner, "name": r.name,
        "revision": r.revision, "directory": r.directory, "revision_url": r.revision_url(),
    })
}

fn repo_of(p: &J) -> Result<HubRepo, Fail> {
    let r = p.get("repo").map_or_else(|| bad("repo is a Hub repository (the rule hub_repo's)"), Ok)?;
    let kind = match s(r, "kind")? {
        "models" => "models",
        "datasets" => "datasets",
        k => return bad(format!("no Hub repository kind {k:?}")),
    };
    Ok(HubRepo { endpoint: s(r, "endpoint")?.into(), kind, owner: s(r, "owner")?.into(), name: s(r, "name")?.into(), revision: s(r, "revision")?.into(), directory: s(r, "directory")?.into() })
}

/// The host rule `name` of the parameters `p` (a JSON object), and its result as JSON; None when
/// there is no rule `name`.
pub fn call(name: &str, p: &J) -> Option<Result<J, Fail>> {
    Some(match name {
        "defaults" => Ok(defaults()),
        "object_url" => s(p, "store_url").and_then(|u| Ok(J::String(object_url(u, s(p, "key")?)))),
        "object_urls" => s(p, "store_url").and_then(|u| Ok(J::Array(strings(p, "keys")?.iter().map(|k| J::String(object_url(u, k))).collect()))),
        "query_encode" => s(p, "s").map(|v| J::String(query_encode(v))),
        "check_store_url" => s(p, "url").and_then(check_store_url).map(|(a, b, c)| json!([a, b, c])),
        "listing_endpoint" => s(p, "url").and_then(listing_endpoint).map(|(e, pre)| json!([e, pre])),
        "listing_url" => (|| Ok(J::String(listing_url(s(p, "endpoint")?, s(p, "prefix")?, opt_s(p, "token")?))))(),
        "hub_endpoint" => opt_s(p, "endpoint").and_then(hub_endpoint).map(J::String),
        "hub_repo" => (|| Ok(hub_repo(s(p, "url")?, s(p, "endpoint")?)?.as_ref().map_or(J::Null, repo_json)))(),
        "hub_urls" => (|| {
            // the revision's URL, and with a commit, the tree's first page and the store's base
            let r = repo_of(p)?;
            Ok(match opt_s(p, "commit")? {
                Some(c) => json!({"revision_url": r.revision_url(), "tree_url": r.tree_url(c), "base": r.base(c)}),
                None => json!({"revision_url": r.revision_url()}),
            })
        })(),
        "hub_page" => (|| Ok(J::Bool(hub_page(s(p, "url")?, s(p, "endpoint")?))))(),
        "parse_listing" => (|| {
            let (objs, token) = parse_listing(&body(p)?, s(p, "prefix")?)?;
            Ok(json!({"objects": objects(objs), "token": token}))
        })(),
        "parse_hub_revision" => body(p).and_then(|b| parse_hub_revision(&b)).map(J::String),
        "parse_hub_tree" => (|| {
            let (objs, entries) = parse_hub_tree(&body(p)?, s(p, "prefix")?)?;
            Ok(json!({"objects": objects(objs), "entries": entries}))
        })(),
        "next_link" => (|| Ok(next_link(&strings(p, "fields")?, s(p, "url")?, s(p, "endpoint")?)?.map_or(J::Null, J::String)))(),
        "store_listing" => (|| {
            let listed = p.get("listed").and_then(J::as_array).map_or_else(|| bad("listed is a list of [key, size]"), Ok)?;
            let listed = listed
                .iter()
                .map(|e| match (e.get(0).and_then(J::as_str), e.get(1).and_then(J::as_u64)) {
                    (Some(k), Some(n)) if e.as_array().is_some_and(|a| a.len() == 2) => Ok((k.to_string(), n)),
                    _ => bad("listed is a list of [key, size]"),
                })
                .collect::<Result<Vec<_>, _>>()?;
            let l = store_listing(listed, s(p, "prefix")?)?;
            Ok(json!({"objects": objects(l.objects), "ignored": l.ignored, "folders": l.folders}))
        })(),
        "input" => (|| {
            Ok(match input(s(p, "location")?, flag(p, "windows")?, flag(p, "allow_file_hosts")?)? {
                Input::Url { store } => json!({"kind": if store { "store" } else { "file" }}),
                Input::Local { path } => json!({"kind": "local", "path": path}),
            })
        })(),
        "is_url" => s(p, "location").map(|l| J::Bool(is_url(l))),
        "is_uri_reference" => s(p, "ref").map(|r| J::Bool(refkv_uri::is_uri_reference(r))),
        "resolve_reference" => (|| Ok(J::String(refkv_uri::resolve(s(p, "base")?, s(p, "ref")?).map_err(Fail::Rejected)?)))(),
        "split_input" => (|| {
            // `url`: the host has no local paths (a browser): every location is a URL reference
            let (loc, title) = (s(p, "location")?, s(p, "title")?);
            let (d, k) = if flag(p, "url")? { split_url(loc, title)? } else { split_input(loc, title, flag(p, "windows")?)? };
            Ok(json!([d, k]))
        })(),
        "scope_keys" => (|| Ok(scope_keys(s(p, "location")?, &strings(p, "scope")?)?.map_or(J::Null, |k| json!(k))))(),
        "late" => (|| {
            let lazy = strings(p, "lazy")?;
            Ok(J::Array(strings(p, "keys")?.iter().map(|k| J::Bool(late(k, &lazy))).collect()))
        })(),
        _ => return None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ok(name: &str, p: J) -> J {
        call(name, &p).unwrap().unwrap()
    }

    #[test]
    fn rules_by_name() {
        assert_eq!(ok("object_urls", json!({"store_url": "http://h/", "keys": ["a b", "c"]})), json!(["http://h/a%20b", "http://h/c"]));
        assert_eq!(ok("listing_url", json!({"endpoint": "http://h/b/", "prefix": "p/", "token": null})), json!("http://h/b/?list-type=2&prefix=p%2F"));
        let repo = ok("hub_repo", json!({"url": "https://huggingface.co/o/r/tree/main/d/", "endpoint": HUB}));
        assert_eq!(repo["directory"], "d/");
        let sha = "1bae0e1088993b9b82de08f59de5be5a34e42170";
        assert_eq!(ok("hub_urls", json!({"repo": repo, "commit": sha}))["base"], format!("https://huggingface.co/o/r/resolve/{sha}/d/"));
        assert_eq!(ok("hub_urls", json!({"repo": repo}))["revision_url"], repo["revision_url"]);
        let page = "<ListBucketResult><IsTruncated>false</IsTruncated><Contents><Key>p/\u{e9}</Key><Size>1</Size></Contents></ListBucketResult>";
        let latin1: String = page.as_bytes().iter().map(|&b| b as char).collect();
        assert_eq!(ok("parse_listing", json!({"body": latin1, "prefix": "p/"})), json!({"objects": [["p/\u{e9}", 1]], "token": null}));
        assert_eq!(ok("store_listing", json!({"listed": [["p/b", 1], ["p/a/", 0]], "prefix": "p/"})), json!({"objects": [["b", 1]], "ignored": [], "folders": ["a/"]}));
        assert_eq!(ok("input", json!({"location": "file:///d/"})), json!({"kind": "local", "path": "/d/"}));
        assert_eq!(ok("input", json!({"location": "https://h/x"})), json!({"kind": "file"}));
        assert_eq!(ok("is_url", json!({"location": "file:/d/x"})), json!(true));
        assert_eq!(ok("is_uri_reference", json!({"ref": "./a:b"})), json!(true));
        assert_eq!(ok("resolve_reference", json!({"base": "http://a/b/c/d;p?q", "ref": "../g"})), json!("http://a/b/g"));
        assert_eq!(ok("split_input", json!({"location": "https://h/a/x.vsi", "title": "VSI"})), json!(["https://h/a/", "x.vsi"]));
        assert_eq!(ok("split_input", json!({"location": "data/scan.nhdr", "title": "NRRD", "url": true})), json!(["data/", "scan.nhdr"]));
        assert_eq!(ok("split_input", json!({"location": "data/scan.nhdr", "title": "NRRD"})), json!(["data", "scan.nhdr"]));
        assert_eq!(ok("scope_keys", json!({"location": "https://h/a/x.vsi", "scope": ["https://h/a/"]})), J::Null);
        assert_eq!(ok("late", json!({"keys": ["zarr.json", "foozarr.json"], "lazy": ["refkv_source/"]})), json!([true, false]));
        assert_eq!(ok("defaults", json!({}))["page_size"], 65536);
        assert!(call("no such rule", &json!({})).is_none());
    }

    #[test]
    fn refuses_a_missing_parameter() {
        assert!(matches!(call("object_url", &json!({"store_url": "http://h/"})), Some(Err(Fail::Bad(_)))));
    }

    #[test]
    fn refuses_a_body_that_is_not_bytes() {
        assert!(matches!(call("parse_hub_revision", &json!({"body": "\u{100}"})), Some(Err(Fail::Bad(_)))));
    }

    #[test]
    fn gives_a_rejection_as_one() {
        assert!(matches!(call("check_store_url", &json!({"url": "ftp://h/"})), Some(Err(Fail::Rejected(_)))));
    }

    #[test]
    fn rejects_a_reference_that_is_not_one() {
        assert!(matches!(call("resolve_reference", &json!({"base": "http://h/", "ref": "a b"})), Some(Err(Fail::Rejected(_)))));
    }

    #[test]
    fn gives_a_usage_error_as_one() {
        assert!(matches!(call("scope_keys", &json!({"location": "https://h/a/x", "scope": ["https://e/"]})), Some(Err(Fail::Usage(_)))));
    }
}
