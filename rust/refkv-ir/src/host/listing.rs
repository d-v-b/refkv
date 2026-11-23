//! A store's listing (spec/zarrify.md §1.4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>, §1.5
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>): the objects
//! and continuation token of one S3 ListObjectsV2 response, read in the XML subset of §1.5
//! (`crate::xmltree`); a Hub store's commit, the objects of one page of its tree, and the page's
//! next link; and the keys a whole listing gives a store run, its ignored keys and its folder
//! markers.

use super::{rejected, Fail};
use crate::json::{self, Jv};
use crate::xmltree::{self, Element};
use std::collections::HashSet;

/// The largest size a listing may give an object: 2^53 − 1, which every host holds exactly.
pub const MAX_SAFE: u64 = (1 << 53) - 1;

fn text(el: &Element) -> Result<String, Fail> {
    if !el.children.is_empty() {
        return rejected(format!("listing: <{}> has a child element", el.name));
    }
    Ok(el.text.concat())
}

/// The objects listed (key, size) by one ListObjectsV2 response, and the continuation token when
/// the listing is truncated (spec/zarrify.md §1.5
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>); every key is
/// under `prefix`.
#[allow(clippy::type_complexity)]
pub fn parse_listing(body: &[u8], prefix: &str) -> Result<(Vec<(String, u64)>, Option<String>), Fail> {
    let s = std::str::from_utf8(body).or_else(|_| rejected("listing is not UTF-8"))?;
    let root = xmltree::parse(s, "listing").map_err(Fail::Rejected)?;
    if root.name != "ListBucketResult" {
        return rejected(format!("listing root element is <{}>, not <ListBucketResult>", root.name));
    }
    let named = |n: &'static str| root.children.iter().filter(move |c| c.name == n);
    let truncated: Vec<&Element> = named("IsTruncated").collect();
    let t = match truncated.as_slice() {
        [t] => text(t)?,
        _ => String::new(),
    };
    if truncated.len() != 1 || (t != "true" && t != "false") {
        return rejected("listing needs exactly one IsTruncated of true or false");
    }
    let tokens: Vec<&Element> = named("NextContinuationToken").collect();
    if tokens.len() > 1 {
        return rejected("listing has more than one NextContinuationToken");
    }
    let token = tokens.first().map(|e| text(e)).transpose()?;
    let mut objects = Vec::new();
    for c in named("Contents") {
        let keys: Vec<&Element> = c.children.iter().filter(|x| x.name == "Key").collect();
        let sizes: Vec<&Element> = c.children.iter().filter(|x| x.name == "Size").collect();
        if keys.len() != 1 || sizes.len() != 1 {
            return rejected("listing Contents needs exactly one Key and one Size");
        }
        let (key, size) = (text(keys[0])?, text(sizes[0])?);
        let n = (!size.is_empty() && size.bytes().all(|c| c.is_ascii_digit()) && size.trim_start_matches('0').len() <= 16)
            .then(|| size.parse::<u64>().ok())
            .flatten()
            .filter(|&n| n <= MAX_SAFE);
        let Some(n) = n else {
            return rejected(format!("listing Size {:?} is not a size", size.chars().take(40).collect::<String>()));
        };
        if !key.starts_with(prefix) {
            return rejected(format!("listed key {key:?} is outside the prefix {prefix:?}"));
        }
        objects.push((key, n));
    }
    if t == "true" {
        if token.as_deref().unwrap_or("").is_empty() {
            return rejected("truncated listing without a NextContinuationToken");
        }
        if objects.is_empty() {
            // a page that lists nothing could be followed forever
            return rejected("truncated listing without a Contents");
        }
        return Ok((objects, token));
    }
    Ok((objects, None))
}

/// The commit `C` of a Hub revision's response (spec/zarrify.md §1.5
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>), or a rejection:
/// a gated repository's too.
pub fn parse_hub_revision(body: &[u8]) -> Result<String, Fail> {
    let doc = json::parse(body).map_err(Fail::Rejected)?.value;
    if !doc.is_obj() {
        return rejected("the Hub revision is not a JSON object");
    }
    let sha = doc.get("sha").and_then(Jv::as_str);
    let Some(sha) = sha.filter(|s| s.len() == 40 && s.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))) else {
        return rejected(format!("the Hub revision's sha {} is not 40 lowercase hex digits", doc.get("sha").map_or("None".into(), |v| v.short(80))));
    };
    match doc.get("gated") {
        None | Some(Jv::Bool(false)) => Ok(sha.to_string()),
        Some(g) => rejected(format!("the Hub repository is gated ({}): its files are not read anonymously", g.short(40))),
    }
}

/// The objects (path, size) of one page of a Hub tree listing (spec/zarrify.md §1.5
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>), its files under
/// the directory `prefix` (its directories list nothing), and its number of entries.
pub fn parse_hub_tree(body: &[u8], prefix: &str) -> Result<(Vec<(String, u64)>, usize), Fail> {
    let doc = json::parse(body).map_err(Fail::Rejected)?.value;
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
            let Some(size) = e.get("size").and_then(|v| v.int(0, MAX_SAFE as i64)) else {
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

/// The links of a `Link` field value (spec/zarrify.md §1.5
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>): (URI reference,
/// parameters), or a rejection.
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
        if !refkv_uri::is_uri_reference(target) {
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

/// The next link of a Hub tree response whose `Link` fields are `fields` and which `url` answered,
/// resolved against `url` (spec/zarrify.md §1.5
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>); None if it has
/// none; a rejection if the fields are not links, give two next links, or one off `endpoint`'s
/// origin.
pub fn next_link(fields: &[String], url: &str, endpoint: &str) -> Result<Option<String>, Fail> {
    if fields.is_empty() {
        return Ok(None);
    }
    let found: Vec<String> = links(&fields.join(","))?
        .into_iter()
        .filter(|(_, params)| params.iter().any(|(k, v)| k.eq_ignore_ascii_case("rel") && v.to_ascii_lowercase().split(|c: char| c.is_ascii() && link_ws(c as u8)).any(|t| t == "next")))
        .map(|(t, _)| t)
        .collect();
    if found.len() > 1 {
        return rejected("the Hub tree response has more than one next link");
    }
    let Some(target) = found.first() else { return Ok(None) };
    let new = refkv_uri::resolve(url, target).map_err(Fail::Rejected)?;
    let (a, e) = (refkv_uri::split(&new), refkv_uri::split(endpoint));
    let same = |x: Option<&str>, y: Option<&str>| x.unwrap_or("").eq_ignore_ascii_case(y.unwrap_or(""));
    if !same(a.scheme, e.scheme) || !same(a.authority, e.authority) {
        return rejected(format!("the Hub tree's next link {:?} is not on {endpoint:?}", new.chars().take(200).collect::<String>()));
    }
    Ok(Some(new))
}

/// An ignored key (spec/zarrify.md §1.4
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>): empty, ending in
/// `/`, or with an empty, `.` or `..` segment.
pub fn ignored(rel: &str) -> bool {
    rel.is_empty() || rel.ends_with('/') || rel.split('/').any(|s| s.is_empty() || s == "." || s == "..")
}

/// An ignored object whose key is not recorded (the same section): an empty one whose relative
/// key is empty or ends in `/`.
pub fn folder_marker(rel: &str, size: u64) -> bool {
    size == 0 && (rel.is_empty() || rel.ends_with('/'))
}

/// A store's listing as a store run takes it (spec/zarrify.md §1.4
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>): `objects`, each
/// object's relative key and size, keys in the order of their UTF-8 bytes; `ignored`, the relative
/// keys of the ignored objects that are recorded, in listing order; and `folders`, those of the
/// empty objects whose keys end in `/` (directories, to the SAFE profile), in listing order.
#[derive(Debug, Default, PartialEq)]
pub struct Listing {
    pub objects: Vec<(String, u64)>,
    pub ignored: Vec<String>,
    pub folders: Vec<String>,
}

/// The listing of a store whose listed objects are `listed` (key, size), every key under
/// `prefix`: each key relative to `prefix`, sorted into objects, recorded ignored keys and folder
/// markers; a rejection if a key is listed twice.
pub fn store_listing(listed: Vec<(String, u64)>, prefix: &str) -> Result<Listing, Fail> {
    let mut seen = HashSet::with_capacity(listed.len());
    let mut l = Listing::default();
    for (key, size) in listed {
        if !seen.insert(key.clone()) {
            return rejected(format!("key {key:?} is listed twice"));
        }
        let Some(rel) = key.strip_prefix(prefix) else {
            return rejected(format!("listed key {key:?} is outside the prefix {prefix:?}"));
        };
        if !ignored(rel) {
            l.objects.push((rel.to_string(), size));
        } else if !folder_marker(rel, size) {
            l.ignored.push(rel.to_string());
        } else if !rel.is_empty() && !ignored(&rel[..rel.len() - 1]) {
            l.folders.push(rel.to_string());
        }
    }
    l.objects.sort_unstable_by(|a, b| a.0.as_bytes().cmp(b.0.as_bytes()));
    Ok(l)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::host::HUB;

    const SHA: &str = "1bae0e1088993b9b82de08f59de5be5a34e42170";

    fn page(contents: &str, truncated: &str, token: &str) -> Vec<u8> {
        format!("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<!-- c --><ListBucketResult xmlns=\"x\"><IsTruncated>{truncated}</IsTruncated>{token}{contents}</ListBucketResult> ").into_bytes()
    }

    fn is_rejected<T: std::fmt::Debug>(r: Result<T, Fail>) -> bool {
        matches!(r, Err(Fail::Rejected(_)))
    }

    fn rejects(body: &str) {
        assert!(is_rejected(parse_listing(body.as_bytes(), "p/")), "{body}");
    }

    #[test]
    fn listings() {
        let one = "<Contents><Key>p/a&amp;b</Key><Size>0012</Size></Contents><Contents><Key>p/&#x3b1;&#946;</Key><Size>9007199254740991</Size></Contents>";
        let (objects, token) = parse_listing(&page(one, "false", ""), "p/").unwrap();
        assert_eq!(objects, vec![("p/a&b".into(), 12), ("p/αβ".into(), MAX_SAFE)]);
        assert_eq!(token, None);
        let (_, token) = parse_listing(&page(one, "true", "<NextContinuationToken>t&lt;1</NextContinuationToken>"), "p/").unwrap();
        assert_eq!(token.as_deref(), Some("t<1"));
        let (objects, token) = parse_listing(&page("", "false", "<NextContinuationToken>x</NextContinuationToken>"), "").unwrap();
        assert!(objects.is_empty() && token.is_none());
    }

    #[test]
    fn rejects_not_utf8() {
        assert!(is_rejected(parse_listing(b"<ListBucketResult>\xff</ListBucketResult>", "")));
    }

    #[test]
    fn rejects_another_root() {
        rejects("<Other><IsTruncated>false</IsTruncated></Other>");
    }

    #[test]
    fn rejects_no_is_truncated() {
        rejects("<ListBucketResult></ListBucketResult>");
    }

    #[test]
    fn rejects_a_bad_is_truncated() {
        rejects("<ListBucketResult><IsTruncated>yes</IsTruncated></ListBucketResult>");
    }

    #[test]
    fn rejects_two_tokens() {
        rejects("<ListBucketResult><IsTruncated>false</IsTruncated><NextContinuationToken>a</NextContinuationToken><NextContinuationToken>b</NextContinuationToken></ListBucketResult>");
    }

    #[test]
    fn rejects_contents_without_a_size() {
        rejects("<ListBucketResult><IsTruncated>false</IsTruncated><Contents><Key>p/a</Key></Contents></ListBucketResult>");
    }

    #[test]
    fn rejects_a_size_over_2_53() {
        rejects("<ListBucketResult><IsTruncated>false</IsTruncated><Contents><Key>p/a</Key><Size>9007199254740992</Size></Contents></ListBucketResult>");
    }

    #[test]
    fn rejects_a_negative_size() {
        rejects("<ListBucketResult><IsTruncated>false</IsTruncated><Contents><Key>p/a</Key><Size>-1</Size></Contents></ListBucketResult>");
    }

    #[test]
    fn rejects_a_key_outside_the_prefix() {
        rejects("<ListBucketResult><IsTruncated>false</IsTruncated><Contents><Key>q/a</Key><Size>1</Size></Contents></ListBucketResult>");
    }

    #[test]
    fn rejects_a_text_with_a_child() {
        rejects("<ListBucketResult><IsTruncated>false<b/></IsTruncated></ListBucketResult>");
    }

    #[test]
    fn rejects_truncated_without_a_token() {
        rejects("<ListBucketResult><IsTruncated>true</IsTruncated><Contents><Key>p/a</Key><Size>1</Size></Contents></ListBucketResult>");
    }

    #[test]
    fn rejects_truncated_without_contents() {
        rejects("<ListBucketResult><IsTruncated>true</IsTruncated><NextContinuationToken>t</NextContinuationToken></ListBucketResult>");
    }

    #[test]
    fn rejects_markup_outside_the_subset() {
        rejects("<!DOCTYPE x><ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>");
    }

    #[test]
    fn hub_pages() {
        // a recorded page (huggingface.co's, shortened): directories list nothing, sizes are the files'
        let body = br#"[{"type":"directory","oid":"0e4b","size":0,"path":"inference/examples"},
            {"type":"file","oid":"a8a0","size":1594,"path":"inference/README.md"},
            {"type":"file","oid":"3b02","size":212495,"lfs":{"oid":"5df8","size":212495,"pointerSize":131},"path":"inference/examples/images/carrots.jpeg"},
            {"type":"file","size":0,"path":"inference/empty"}]"#;
        let (listed, entries) = parse_hub_tree(body, "inference/").unwrap();
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
    fn rejects_a_hub_revision_that_is_not_json() {
        assert!(is_rejected(parse_hub_revision(b"<html>")));
    }

    #[test]
    fn rejects_a_short_sha() {
        assert!(is_rejected(parse_hub_revision(br#"{"sha":"1bae0e1"}"#)));
    }

    #[test]
    fn rejects_a_gated_repository() {
        assert!(is_rejected(parse_hub_revision(format!(r#"{{"sha":"{SHA}","gated":"manual"}}"#).as_bytes())));
    }

    #[test]
    fn rejects_a_tree_that_is_not_an_array() {
        assert!(is_rejected(parse_hub_tree(br#"{"error":"x"}"#, "")));
    }

    #[test]
    fn rejects_a_tree_entry_of_another_type() {
        assert!(is_rejected(parse_hub_tree(br#"[{"type":"submodule","path":"a"}]"#, "")));
    }

    #[test]
    fn rejects_a_tree_entry_outside_the_directory() {
        assert!(is_rejected(parse_hub_tree(br#"[{"type":"file","size":1,"path":"b/x"}]"#, "a/")));
    }

    #[test]
    fn rejects_a_tree_entry_that_is_the_directory() {
        assert!(is_rejected(parse_hub_tree(br#"[{"type":"directory","path":"a/"}]"#, "a/")));
    }

    #[test]
    fn rejects_a_tree_path_with_a_lone_surrogate() {
        assert!(is_rejected(parse_hub_tree(br#"[{"type":"file","size":1,"path":"a\ud800"}]"#, "")));
    }

    #[test]
    fn rejects_a_file_without_a_size() {
        assert!(is_rejected(parse_hub_tree(br#"[{"type":"file","path":"a"}]"#, "")));
    }

    #[test]
    fn rejects_a_tree_size_beyond_2_53() {
        assert!(is_rejected(parse_hub_tree(br#"[{"type":"file","size":9007199254740992,"path":"a"}]"#, "")));
    }

    #[test]
    fn rejects_a_link_header_that_is_not_links() {
        assert!(is_rejected(next_link(&["https://h/x; rel=next".into()], "https://huggingface.co/", HUB)));
    }

    #[test]
    fn rejects_a_link_parameter_without_a_value() {
        assert!(is_rejected(next_link(&["<x>; rel=".into()], "https://huggingface.co/", HUB)));
    }

    #[test]
    fn rejects_two_next_links() {
        assert!(is_rejected(next_link(&["<a>; rel=next".into(), "<b>; rel=next".into()], "https://huggingface.co/", HUB)));
    }

    #[test]
    fn rejects_a_next_link_off_the_endpoint() {
        assert!(is_rejected(next_link(&["<https://evil.test/x>; rel=next".into()], "https://huggingface.co/", HUB)));
    }

    #[test]
    fn listed_keys() {
        let l = store_listing(
            vec![("p/b".into(), 2), ("p/a".into(), 1), ("p/".into(), 0), ("p/d/".into(), 0), ("p/x/./y".into(), 3), ("p/e/".into(), 2), ("p//f/".into(), 0)],
            "p/",
        )
        .unwrap();
        assert_eq!(l.objects, vec![("a".to_string(), 1), ("b".to_string(), 2)]);
        assert_eq!(l.ignored, vec!["x/./y".to_string(), "e/".to_string()]);
        assert_eq!(l.folders, vec!["d/".to_string()]);
        assert!(!ignored("a/b") && ignored("a//b") && ignored("../a") && ignored(""));
        assert!(folder_marker("a/", 0) && !folder_marker("a/", 1) && !folder_marker("a", 0));
    }

    #[test]
    fn rejects_a_key_listed_twice() {
        assert!(is_rejected(store_listing(vec![("a".into(), 1), ("a".into(), 1)], "")));
    }

    #[test]
    fn rejects_a_listed_key_outside_the_prefix() {
        assert!(is_rejected(store_listing(vec![("q/a".into(), 1)], "p/")));
    }
}
