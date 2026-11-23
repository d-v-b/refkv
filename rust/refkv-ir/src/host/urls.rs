//! Store URLs (spec/zarrify.md §1.4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>, §1.5
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>): a store URL's
//! checks, the URL of each object, the listing endpoint and key prefix of an S3-style store, and
//! the repository, revision and directory of a Hugging Face Hub store.

use super::{rejected, usage, Fail};
use refkv_uri::{is_uri_reference, percent_decode, split};

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

/// The URL of the object with relative key `key` (spec/zarrify.md §1.4
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>): the store URL, then
/// the key's UTF-8 bytes, each but the unreserved ones and `!$&'()*+,;=:@/` percent-encoded.
pub fn object_url(store_url: &str, key: &str) -> String {
    store_url.to_string() + &pct(key.as_bytes(), |b| unreserved(b) || b"!$&'()*+,;=:@/".contains(&b))
}

/// `enc` of spec/zarrify.md §1.5
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>: every byte but
/// the unreserved ones percent-encoded.
pub fn query_encode(s: &str) -> String {
    pct(s.as_bytes(), unreserved)
}

/// (scheme, authority, path) of a valid store URL (spec/zarrify.md §1.4
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>): an http(s) URL
/// with an authority and no userinfo, no query or fragment, whose path ends in `/`; or a rejection.
pub fn check_store_url(url: &str) -> Result<(String, String, String), Fail> {
    if !is_uri_reference(url) {
        return rejected(format!("{url:?} is not a valid URI"));
    }
    let p = split(url);
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
    Ok((p.scheme.unwrap_or_default().to_string(), authority.to_string(), p.path.to_string()))
}

/// (endpoint, prefix `P`) of a store URL (spec/zarrify.md §1.5
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>): virtual-hosted
/// style for an S3 host, path style (the first segment the bucket) for any other.
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

/// The URL of the ListObjectsV2 request (spec/zarrify.md §1.5
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>) for the key
/// prefix `prefix` at `endpoint`: the first page's, or with `token`, the page after the response
/// that gave it.
pub fn listing_url(endpoint: &str, prefix: &str, token: Option<&str>) -> String {
    let mut u = format!("{endpoint}?list-type=2&prefix={}", query_encode(prefix));
    if let Some(t) = token {
        u += &format!("&continuation-token={}", query_encode(t));
    }
    u
}

/// The Hugging Face Hub's endpoint, unless the application gives another (`HF_ENDPOINT`).
pub const HUB: &str = "https://huggingface.co";

/// The Hub's endpoint `E` (spec/zarrify.md §1.5
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>): `endpoint` (the
/// application's, or the environment's `HF_ENDPOINT`, which the host reads) when it is given and
/// not empty, else [`HUB`], without a trailing `/` (as `huggingface_hub` reads it); a usage error
/// if it is not an http(s) URL with an authority, without userinfo, a query or a fragment.
pub fn hub_endpoint(endpoint: Option<&str>) -> Result<String, Fail> {
    let e = endpoint.filter(|e| !e.is_empty()).unwrap_or(HUB).trim_end_matches('/').to_string();
    let p = split(&e);
    let http = p.scheme.is_some_and(|s| s.eq_ignore_ascii_case("http") || s.eq_ignore_ascii_case("https"));
    if !is_uri_reference(&e) || !http || p.authority.is_none_or(|a| a.is_empty() || a.contains('@')) || p.query.is_some() || p.fragment.is_some() {
        return usage(format!("the Hub endpoint {e:?} is not an http(s) URL without userinfo, query or fragment"));
    }
    Ok(e)
}

/// A Hub store (spec/zarrify.md §1.5
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>): its endpoint,
/// kind (`models` or `datasets`), owner, name, revision `r` and directory `D` (empty, or ending in
/// `/`), the last two percent-decoded.
#[derive(Debug, Clone, PartialEq)]
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

    /// The URL that resolves the revision to its commit, and says whether the repository is gated.
    pub fn revision_url(&self) -> String {
        format!("{}/revision/{}?expand%5B%5D=sha&expand%5B%5D=gated", self.api(), query_encode(&self.revision))
    }

    /// The first page of the directory's tree at `commit`.
    pub fn tree_url(&self, commit: &str) -> String {
        let d = match self.directory.strip_suffix('/') {
            Some(d) => format!("/{}", object_url("", d)),
            None => String::new(),
        };
        format!("{}/tree/{commit}{d}?recursive=true", self.api())
    }

    /// The store's base (spec/zarrify.md §1.4
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>) at `commit`.
    pub fn base(&self, commit: &str) -> String {
        let kind = if self.kind == "datasets" { "/datasets/" } else { "/" };
        format!("{}{kind}{}/{}/resolve/{commit}/{}", self.endpoint, self.owner, self.name, object_url("", &self.directory))
    }
}

/// The rest of `url`'s path after `endpoint`'s and a `/`, when `url` has `endpoint`'s scheme and
/// authority (in any case) and its path starts so.
fn under<'a>(url: &'a str, endpoint: &str) -> Option<&'a str> {
    let (u, e) = (split(url), split(endpoint));
    let same = |a: Option<&str>, b: Option<&str>| matches!((a, b), (Some(a), Some(b)) if a.eq_ignore_ascii_case(b));
    if !same(u.scheme, e.scheme) || !same(u.authority, e.authority) {
        return None;
    }
    u.path.strip_prefix(e.path)?.strip_prefix('/')
}

fn hub_name(n: &str) -> bool {
    !n.is_empty() && n != "." && n != ".." && n.bytes().all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-'))
}

/// The Hub store that the store URL `url` names under `endpoint` (spec/zarrify.md §1.5
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>), None if `url`
/// is not under it, or a rejection if it is but does not name one.
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
    Ok(Some(HubRepo { endpoint: endpoint.to_string(), kind, owner: segments[0].to_string(), name: segments[1].to_string(), revision, directory }))
}

/// Whether the file input `url` would be a Hub store if it ended in `/` (spec/zarrify.md §1.5
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>): a web page,
/// rejected without being read.
pub fn hub_page(url: &str, endpoint: &str) -> bool {
    let p = split(url);
    if p.query.is_some() || p.fragment.is_some() || url.ends_with('/') {
        return false;
    }
    matches!(hub_repo(&format!("{url}/"), endpoint), Ok(Some(_)))
}

#[cfg(test)]
mod tests {
    use super::*;

    const SHA: &str = "1bae0e1088993b9b82de08f59de5be5a34e42170";

    fn hub(url: &str) -> Result<Option<HubRepo>, Fail> {
        hub_repo(url, HUB)
    }

    fn is_rejected<T: std::fmt::Debug>(r: Result<T, Fail>) -> bool {
        matches!(r, Err(Fail::Rejected(_)))
    }

    #[test]
    fn urls() {
        assert_eq!(object_url("http://h/b/", "a b/c~d%é:@!"), "http://h/b/a%20b/c~d%25%C3%A9:@!");
        assert_eq!(query_encode("a/b c~"), "a%2Fb%20c~");
        assert_eq!(check_store_url("HTTPS://h:9/a/").unwrap(), ("HTTPS".into(), "h:9".into(), "/a/".into()));
        assert_eq!(listing_endpoint("http://127.0.0.1:8000/f/n5/x/").unwrap(), ("http://127.0.0.1:8000/f/".into(), "n5/x/".into()));
        assert_eq!(listing_endpoint("https://b.s3.us-west-2.amazonaws.com/p/q/").unwrap(), ("https://b.s3.us-west-2.amazonaws.com/".into(), "p/q/".into()));
        assert_eq!(listing_endpoint("https://s3.amazonaws.com/b/").unwrap(), ("https://s3.amazonaws.com/b/".into(), "".into()));
        assert_eq!(listing_url("http://h/b/", "p q/", None), "http://h/b/?list-type=2&prefix=p%20q%2F");
        assert_eq!(listing_url("http://h/b/", "", Some("a/b=")), "http://h/b/?list-type=2&prefix=&continuation-token=a%2Fb%3D");
        assert_eq!(listing_endpoint("http://[::1]:9/b/%C3%A9/").unwrap(), ("http://[::1]:9/b/".into(), "é/".into()));
    }

    #[test]
    fn rejects_an_invalid_store_url() {
        assert!(is_rejected(check_store_url("http://h/a b/")));
    }

    #[test]
    fn rejects_another_scheme() {
        assert!(is_rejected(check_store_url("ftp://h/a/")));
    }

    #[test]
    fn rejects_userinfo() {
        assert!(is_rejected(check_store_url("http://u@h/a/")));
    }

    #[test]
    fn rejects_a_query() {
        assert!(is_rejected(check_store_url("http://h/a/?q")));
    }

    #[test]
    fn rejects_no_trailing_slash() {
        assert!(is_rejected(check_store_url("http://h/a")));
    }

    #[test]
    fn rejects_no_bucket() {
        assert!(is_rejected(listing_endpoint("http://h/")));
    }

    #[test]
    fn rejects_an_empty_bucket() {
        assert!(is_rejected(listing_endpoint("http://h//a/")));
    }

    #[test]
    fn rejects_a_prefix_that_is_not_utf8() {
        assert!(is_rejected(listing_endpoint("http://h/b/%FF/")));
    }

    #[test]
    fn hub_stores() {
        assert_eq!(hub_endpoint(None).unwrap(), HUB);
        assert_eq!(hub_endpoint(Some("")).unwrap(), HUB);
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
    }

    #[test]
    fn rejects_a_hub_url_without_a_revision() {
        assert!(is_rejected(hub("https://huggingface.co/o/r/")));
    }

    #[test]
    fn rejects_a_hub_space() {
        assert!(is_rejected(hub("https://huggingface.co/spaces/o/r/tree/main/")));
    }

    #[test]
    fn rejects_a_hub_name_that_is_a_dot_segment() {
        assert!(is_rejected(hub("https://huggingface.co/o/../tree/main/")));
    }

    #[test]
    fn rejects_a_hub_directory_with_a_dot_segment() {
        assert!(is_rejected(hub("https://huggingface.co/o/r/tree/main/a/%2E%2E/")));
    }

    #[test]
    fn rejects_a_hub_revision_that_is_not_utf8() {
        assert!(is_rejected(hub("https://huggingface.co/o/r/tree/%FF/")));
    }

    #[test]
    fn rejects_a_hub_endpoint_with_a_query() {
        assert!(matches!(hub_endpoint(Some("https://h/?x")), Err(Fail::Usage(_))));
    }
}
