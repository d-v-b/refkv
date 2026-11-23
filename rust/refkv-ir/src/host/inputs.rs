//! A zarrifier's input (spec/zarrify.md §1.2
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>, §1.9
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#19-sources-beside-the-input>): whether
//! a location is a store input, a file input, or a local path (a `file:` URL's among them); the
//! directory and key of an input read with the files beside it; and the keys of its scope.

use super::{rejected, usage, Fail};
use refkv_uri::percent_decode;

/// The scheme of a `location` that is a URL with an authority (`s3://b/k`, `https://h/p`), in lower
/// case; None for a local path (`C:\data` on Windows among them), and for a `file:` URL without an
/// authority (`file:/x`, RFC 8089's minimal form), which [`input`] reads as the path it names.
pub fn url_scheme(location: &str) -> Option<String> {
    let i = location.find("://")?;
    let s = &location[..i];
    let b = s.as_bytes();
    (b.first().is_some_and(u8::is_ascii_alphabetic) && b.iter().all(|&c| c.is_ascii_alphanumeric() || matches!(c, b'+' | b'.' | b'-'))).then(|| s.to_ascii_lowercase())
}

/// What a location names (spec/zarrify.md §1.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>).
#[derive(Debug, PartialEq)]
pub enum Input {
    /// a URL (not a `file:` one): a store input when its path ends in `/`, else a file input
    Url { store: bool },
    /// a local file or directory, by its path or its `file:` URL (the path it names), which the host
    /// looks at: a directory is a store input
    Local { path: String },
}

/// What `location` names: a URL (not a `file:` one), a store input when its path ends in `/`; else
/// a local path, a `file:` URL's (spec/zarrify.md §1.2: one an implementation accepts names the
/// local file or directory it is the URL of) on Windows when `windows`, its host allowed when
/// `allow_file_hosts` (spec/refkv.md §6.7
/// <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>); a usage error for a
/// `file:` URL that names no local path.
pub fn input(location: &str, windows: bool, allow_file_hosts: bool) -> Result<Input, Fail> {
    if location.len() >= 5 && location[..5].eq_ignore_ascii_case("file:") {
        return refkv_uri::file_path(location, windows, allow_file_hosts).map(|path| Input::Local { path }).or_else(|e| usage(format!("{location}: {e}")));
    }
    if url_scheme(location).is_some() {
        let path = location.split(['?', '#']).next().unwrap_or("");
        return Ok(Input::Url { store: path.ends_with('/') });
    }
    Ok(Input::Local { path: location.to_string() })
}

/// (the directory, the named file's key) of an input of the format `title` names (VSI, NRRD) read
/// with the files beside it (spec/zarrify.md §1.9
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#19-sources-beside-the-input>): for a
/// URL, the URL up to its last `/` and the last segment, percent-decoded, which must be UTF-8 (a
/// file named otherwise is no object a store can have); for a local path, its directory and its
/// name, as Python's `os.path.split` gives them (`ntpath`'s when `windows`).
pub fn split_input(location: &str, title: &str, windows: bool) -> Result<(String, String), Fail> {
    if url_scheme(location).is_some() {
        if location.contains(['?', '#']) {
            return rejected(format!("{title} input {location:?} has a query or a fragment"));
        }
        let k = location.rfind('/').unwrap_or(0);
        let key = String::from_utf8(percent_decode(&location[k + 1..])).or_else(|_| rejected(format!("{title} input {location:?}: its file name is not percent-encoded UTF-8")))?;
        if key.is_empty() || key.contains('/') {
            return rejected(format!("{title} input {location:?} names no file"));
        }
        return Ok((location[..=k].to_string(), key));
    }
    let sep = |c: char| c == '/' || (windows && c == '\\');
    // ntpath keeps a drive (`C:`) with the directory
    let drive = if windows && location.as_bytes().get(1) == Some(&b':') { 2 } else { 0 };
    let (head, name) = match location[drive..].rfind(sep) {
        Some(k) => (&location[..drive + k + 1], &location[drive + k + 1..]),
        None => (&location[..drive], &location[drive..]),
    };
    // the directory without its trailing separators, unless it is all separators (a root)
    let bare = head[drive..].trim_end_matches(sep);
    let dir = if bare.is_empty() { head.to_string() } else { format!("{}{bare}", &head[..drive]) };
    if name.is_empty() {
        return usage(format!("{location}: not a file name"));
    }
    Ok((dir, name.to_string()))
}

/// The scope of the file input at the URL `location` (spec/zarrify.md §1.9
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#19-sources-beside-the-input>,
/// **Scope**) as the keys a store run's `scope` takes: each entry of `scope` is a URL, the input's
/// directory or a file or directory below it (a directory's key ends in `/`), percent-decoded; the
/// input itself, always in scope, adds no key. None when the scope holds the input's directory,
/// every key being in it. A usage error for an entry outside the directory. (A local input's scope
/// is the host's: its entries are paths, made absolute and looked at.)
pub fn scope_keys(location: &str, scope: &[String]) -> Result<Option<Vec<String>>, Fail> {
    let (d, named) = split_input(location, "the", false)?;
    let outside = |e: &str| usage(format!("scope \"{e}\" is not the input's directory \"{d}\" or below it"));
    let mut keys = Vec::new();
    for e in scope {
        let Some(rest) = e.strip_prefix(d.as_str()) else { return outside(e) };
        let Ok(key) = String::from_utf8(percent_decode(rest)) else { return outside(e) };
        if key.is_empty() {
            return Ok(None);
        }
        if key != named {
            keys.push(key);
        }
    }
    Ok(Some(keys))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn inputs() {
        assert_eq!(input("https://h/a/?x", false, false).unwrap(), Input::Url { store: true });
        assert_eq!(input("https://h/a#/", false, false).unwrap(), Input::Url { store: false });
        assert_eq!(input("s3://b/k/", false, false).unwrap(), Input::Url { store: true });
        assert_eq!(input("dir/x.tif", false, false).unwrap(), Input::Local { path: "dir/x.tif".into() });
        assert_eq!(input("C:\\data", true, false).unwrap(), Input::Local { path: "C:\\data".into() });
        assert_eq!(input("file:///d/a%20b/", false, false).unwrap(), Input::Local { path: "/d/a b/".into() });
        assert_eq!(input("FILE:/x", false, false).unwrap(), Input::Local { path: "/x".into() });
        assert_eq!(url_scheme("HTTP://h/"), Some("http".into()));
        assert_eq!(url_scheme("a b://h/"), None);
    }

    #[test]
    fn rejects_a_file_url_that_names_no_local_path() {
        assert!(matches!(input("file://server/x", false, false), Err(Fail::Usage(_))));
    }

    #[test]
    fn splits() {
        let s = |l: &str, w: bool| split_input(l, "VSI", w).unwrap();
        assert_eq!(s("https://h/a/b%20c.vsi", false), ("https://h/a/".into(), "b c.vsi".into()));
        assert_eq!(s("s3://b/x.vsi", false), ("s3://b/".into(), "x.vsi".into()));
        assert_eq!(s("dir/x.vsi", false), ("dir".into(), "x.vsi".into()));
        assert_eq!(s("x.vsi", false), ("".into(), "x.vsi".into()));
        assert_eq!(s("/x.vsi", false), ("/".into(), "x.vsi".into()));
        assert_eq!(s("a//b/x.vsi", false), ("a//b".into(), "x.vsi".into()));
        assert_eq!(s("a\\x.vsi", false), ("".into(), "a\\x.vsi".into()));
        assert_eq!(s("C:\\d\\x.vsi", true), ("C:\\d".into(), "x.vsi".into()));
        assert_eq!(s("C:x.vsi", true), ("C:".into(), "x.vsi".into()));
        assert_eq!(s("C:\\x.vsi", true), ("C:\\".into(), "x.vsi".into()));
    }

    #[test]
    fn rejects_an_input_url_with_a_query() {
        assert!(matches!(split_input("https://h/a/x.vsi?q", "VSI", false), Err(Fail::Rejected(_))));
    }

    #[test]
    fn rejects_an_input_url_that_names_no_file() {
        assert!(matches!(split_input("https://h/a/", "VSI", false), Err(Fail::Rejected(_))));
    }

    #[test]
    fn rejects_an_input_url_whose_name_is_not_utf8() {
        assert!(matches!(split_input("https://h/a/%FF.vsi", "VSI", false), Err(Fail::Rejected(_))));
    }

    #[test]
    fn rejects_a_local_input_that_names_no_file() {
        assert!(matches!(split_input("dir/", "VSI", false), Err(Fail::Usage(_))));
    }

    #[test]
    fn scopes() {
        let k = |s: &[&str]| scope_keys("https://h/d/a.nhdr", &s.iter().map(|x| x.to_string()).collect::<Vec<_>>()).unwrap();
        assert_eq!(k(&["https://h/d/a.nhdr", "https://h/d/f/", "https://h/d/b%20c.raw"]), Some(vec!["f/".into(), "b c.raw".into()]));
        assert_eq!(k(&["https://h/d/x", "https://h/d/"]), None);
        assert_eq!(k(&[]), Some(vec![]));
    }

    #[test]
    fn rejects_a_scope_outside_the_directory() {
        assert!(matches!(scope_keys("https://h/d/a.nhdr", &["https://h/e/b".into()]), Err(Fail::Usage(_))));
    }

    #[test]
    fn rejects_a_scope_that_is_not_utf8() {
        assert!(matches!(scope_keys("https://h/d/a.nhdr", &["https://h/d/%FF".into()]), Err(Fail::Usage(_))));
    }
}
