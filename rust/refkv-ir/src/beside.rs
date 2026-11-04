//! A file's siblings (spec/zarrify.md §1.9
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#19-sources-beside-the-input>): what
//! every profile that reads them shares, a written name's steps to a key and the bounds.

use crate::ir::{key_name, Ir, Res};
use std::collections::HashMap;

/// The most distinct keys a run probes, the named file not counted (`bounds`, the **Files**
/// bound of the section).
pub const MAX_FILES: u64 = 1 << 16;
/// How deep documents that name documents go, the named file being level 1.
pub const MAX_DEPTH: u32 = 16;
/// The longest written name, in UTF-8 bytes.
pub const MAX_NAME: usize = 1024;

/// The directories of a store IR that siblings have added, each once.
#[derive(Default)]
pub struct Tree {
    dirs: HashMap<String, u32>,
}

impl Tree {
    /// The object of a found sibling, `key` of `size` bytes, under the directories its key names.
    pub fn object(&mut self, ir: &mut Ir, key: &str, size: u64) -> Res<u32> {
        let mut parent = 0u32;
        let mut from = 0;
        for (at, _) in key.match_indices('/') {
            parent = match self.dirs.get(&key[..at]) {
                Some(&d) => d,
                None => {
                    let (n, x) = key_name(&key[from..at], false);
                    let d = ir.struct_(parent, &n, x, 0, None)?;
                    self.dirs.insert(key[..at].to_string(), d);
                    d
                }
            };
            from = at + 1;
        }
        let (n, x) = key_name(&key[from..], false);
        ir.object(parent, &n, x, size)
    }
}

/// How a format writes names: the steps its profile's document allows (the section's **Naming**).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Written {
    /// names are percent-encoded: decoded once
    pub percent: bool,
    /// names are Windows paths: each `\` is a `/`
    pub windows: bool,
    /// a segment `..` is allowed, while it stays below the store URL
    pub parents: bool,
}

/// One percent-decoding of `b`; a `%` without two hexadecimal digits after it stays as it is.
fn percent_decoded(b: &[u8]) -> Vec<u8> {
    let hex = |c: u8| (c as char).to_digit(16);
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' && i + 2 < b.len() {
            if let (Some(h), Some(l)) = (hex(b[i + 1]), hex(b[i + 2])) {
                out.push((h * 16 + l) as u8);
                i += 3;
                continue;
            }
        }
        out.push(b[i]);
        i += 1;
    }
    out
}

/// The key of the written name `name`, held by a file in the directory `dir` below the store URL
/// (empty for the named file's own; with no trailing `/`), or None when the name is rejected: the
/// profile says with which message.
pub fn written_key(name: &[u8], dir: &str, how: Written) -> Option<String> {
    if name.len() > MAX_NAME {
        return None;
    }
    std::str::from_utf8(name).ok()?;
    let decoded = if how.percent { percent_decoded(name) } else { name.to_vec() };
    let mut text = String::from_utf8(decoded).ok()?;
    if how.windows {
        text = text.replace('\\', "/");
    }
    if text.len() > MAX_NAME || text.starts_with('/') || text.contains(['\\', ':']) {
        return None;
    }
    let mut key: Vec<&str> = dir.split('/').filter(|s| !s.is_empty()).collect();
    for segment in text.split('/') {
        match segment {
            "" => return None,
            "." => {}
            ".." => {
                if !how.parents {
                    return None;
                }
                key.pop()?;
            }
            s => key.push(s),
        }
    }
    (!key.is_empty()).then(|| key.join("/"))
}

/// A name under Unicode's simple case folding (the section's **Case**), as the hosts all compare
/// names: each character's single-character uppercase, then that one's single-character
/// lowercase. Dotless `ı`, which folds only under the Turkic rules, stays itself.
pub fn folded(name: &str) -> String {
    let one = |mut it: std::vec::IntoIter<char>, or: char| match (it.next(), it.next()) {
        (Some(c), None) => c,
        _ => or,
    };
    name.chars()
        .map(|c| {
            if c == '\u{131}' {
                return c;
            }
            let up = one(c.to_uppercase().collect::<Vec<_>>().into_iter(), c);
            one(up.to_lowercase().collect::<Vec<_>>().into_iter(), up)
        })
        .collect()
}

/// The key a host that can list finds for the written key `key` (the section's **Case**): `list`
/// gives the entries of the directory at a key (empty for the store's root), or None when there is
/// no such directory. Each segment is the entry equal to it, else the only one equal to it under
/// [`folded`]. None when one has neither, and the two keys when one has two such entries.
pub fn found_key(key: &str, mut list: impl FnMut(&str) -> Option<Vec<String>>) -> Result<Option<String>, (String, String)> {
    let mut at = String::new();
    let joined = |at: &str, name: &str| if at.is_empty() { name.to_string() } else { format!("{at}/{name}") };
    for segment in key.split('/') {
        let Some(entries) = list(&at) else { return Ok(None) };
        let name = if entries.iter().any(|e| e == segment) {
            segment.to_string()
        } else {
            let wanted = folded(segment);
            let mut matching: Vec<&String> = entries.iter().filter(|e| folded(e) == wanted).collect();
            matching.sort();
            match matching.as_slice() {
                [] => return Ok(None),
                [one] => (*one).clone(),
                [a, b, ..] => return Err((joined(&at, a), joined(&at, b))),
            }
        };
        at = joined(&at, &name);
    }
    Ok(Some(at))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The directories of a store, listed.
    fn listing(tree: &[(&str, &[&str])]) -> impl FnMut(&str) -> Option<Vec<String>> {
        let tree: HashMap<String, Vec<String>> =
            tree.iter().map(|(d, e)| (d.to_string(), e.iter().map(|s| s.to_string()).collect())).collect();
        move |d| tree.get(d).cloned()
    }

    #[test]
    fn a_key_is_found_by_listing_each_segment() {
        let tree: &[(&str, &[&str])] = &[
            ("", &["Metadata", "Series010.lof", "both.lof", "BOTH.lof"]),
            ("Metadata", &["Series010.xlif", "stra\u{df}e", "\u{c9}.xlif", "\u{131}.xlif"]),
        ];
        let cases = [
            ("Series010.lof", Some("Series010.lof")),
            ("series010.lof", Some("Series010.lof")),
            ("metadata/series010.xlif", Some("Metadata/Series010.xlif")),
            ("METADATA/SERIES010.XLIF", Some("Metadata/Series010.xlif")),
            ("Metadata/\u{e9}.xlif", Some("Metadata/\u{c9}.xlif")),
            // an exact match is taken, though another entry matches ignoring case
            ("both.lof", Some("both.lof")),
            ("BOTH.lof", Some("BOTH.lof")),
            // ß folds to no two letters, and dotless ı to no i: simple folding
            ("Metadata/strasse", None),
            ("Metadata/i.xlif", None),
            ("absent.lof", None),
            ("Series010.lof/x", None),
            ("Elsewhere/a", None),
        ];
        for (key, want) in cases {
            assert_eq!(found_key(key, listing(tree)), Ok(want.map(str::to_string)), "{key}");
        }
    }

    #[test]
    fn two_entries_that_match_ignoring_case_are_an_error() {
        let tree: &[(&str, &[&str])] = &[("", &["Dir"]), ("Dir", &["A.lof", "a.LOF"])];
        assert_eq!(found_key("dir/a.lof", listing(tree)), Err(("Dir/A.lof".to_string(), "Dir/a.LOF".to_string())));
    }

    const PLAIN: Written = Written { percent: false, windows: false, parents: false };
    const LEICA: Written = Written { percent: true, windows: true, parents: true };

    #[test]
    fn written_names_become_keys() {
        let cases: &[(&[u8], &str, Written, &str)] = &[
            (b"data.raw", "", PLAIN, "data.raw"),
            (b"./a/./b.raw", "", PLAIN, "a/b.raw"),
            (b"b.raw", "a", PLAIN, "a/b.raw"),
            (b"x%20y", "", PLAIN, "x%20y"),
            (b".%5Cmetadata%5Cseries010.xlif", "", LEICA, "metadata/series010.xlif"),
            (b"..%5Cseries010.lof", "metadata", LEICA, "series010.lof"),
            (b"..%5Cedf%5Cdepth%20map.xlif", "metadata", LEICA, "edf/depth map.xlif"),
            (b"a/../b", "", Written { parents: true, ..PLAIN }, "b"),
            (b"100%", "", Written { percent: true, ..PLAIN }, "100%"),
        ];
        for &(name, dir, how, want) in cases {
            assert_eq!(written_key(name, dir, how).as_deref(), Some(want), "{}", String::from_utf8_lossy(name));
        }
    }

    #[test]
    fn a_name_that_is_not_utf8_is_rejected() {
        assert_eq!(written_key(b"a\xff", "", PLAIN), None);
        assert_eq!(written_key(b"a%ff", "", Written { percent: true, ..PLAIN }), None);
    }

    #[test]
    fn a_name_longer_than_the_limit_is_rejected() {
        assert_eq!(written_key(&vec![b'a'; MAX_NAME + 1], "", PLAIN), None);
    }

    #[test]
    fn an_absolute_name_is_rejected() {
        assert_eq!(written_key(b"/etc/passwd", "", PLAIN), None);
        assert_eq!(written_key(b"%2Fetc", "", Written { percent: true, ..PLAIN }), None);
    }

    #[test]
    fn a_scheme_or_a_drive_is_rejected() {
        assert_eq!(written_key(b"http://elsewhere/x", "", PLAIN), None);
        assert_eq!(written_key(b"C:\\x", "", LEICA), None);
    }

    #[test]
    fn a_backslash_outside_a_windows_format_is_rejected() {
        assert_eq!(written_key(b"a\\b", "", PLAIN), None);
    }

    #[test]
    fn an_empty_segment_is_rejected() {
        assert_eq!(written_key(b"", "", PLAIN), None);
        assert_eq!(written_key(b"a//b", "", PLAIN), None);
        assert_eq!(written_key(b"a/", "", PLAIN), None);
    }

    #[test]
    fn a_parent_segment_needs_the_profile_to_allow_it() {
        assert_eq!(written_key(b"../b", "a", PLAIN), None);
    }

    #[test]
    fn a_name_cannot_leave_the_store() {
        assert_eq!(written_key(b"../x", "", LEICA), None);
        assert_eq!(written_key(b"../../x", "a", LEICA), None);
    }

    #[test]
    fn a_name_of_no_file_is_rejected() {
        assert_eq!(written_key(b".", "", PLAIN), None);
        assert_eq!(written_key(b"a/..", "", Written { parents: true, ..PLAIN }), None);
    }
}
