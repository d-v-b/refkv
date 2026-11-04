//! A file's siblings (spec/zarrify.md §1.9
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#19-sources-beside-the-input>): what
//! every profile that reads them shares, a written name's steps to a key and the bounds.

/// The most distinct keys a run probes, the named file not counted (`bounds`, the **Files**
/// bound of the section).
pub const MAX_FILES: u64 = 1 << 16;
/// How deep documents that name documents go, the named file being level 1.
pub const MAX_DEPTH: u32 = 16;
/// The longest written name, in UTF-8 bytes.
pub const MAX_NAME: usize = 1024;

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

#[cfg(test)]
mod tests {
    use super::*;

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
