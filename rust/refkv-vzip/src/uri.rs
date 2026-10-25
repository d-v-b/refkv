//! RFC 3986 URI references, strict resolution, and the `file:` base URI of a local path (spec/refkv.md
//! §5 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#5-source-table>), as
//! python/src/refkv/uri.py has them.

/// The components of a URI reference (RFC 3986 Appendix B): scheme, authority, path, query and
/// fragment, each but the path absent when its delimiter is.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Parts<'a> {
    pub scheme: Option<&'a str>,
    pub authority: Option<&'a str>,
    pub path: &'a str,
    pub query: Option<&'a str>,
    pub fragment: Option<&'a str>,
}

/// Splits a URI reference by RFC 3986 Appendix B's expression.
pub fn split(s: &str) -> Parts<'_> {
    let mut rest = s;
    let mut scheme = None;
    if let Some(i) = rest.find([':', '/', '?', '#'])
        && i > 0 && rest.as_bytes()[i] == b':' {
            scheme = Some(&rest[..i]);
            rest = &rest[i + 1..];
        }
    let mut authority = None;
    if let Some(r) = rest.strip_prefix("//") {
        let i = r.find(['/', '?', '#']).unwrap_or(r.len());
        authority = Some(&r[..i]);
        rest = &r[i..];
    }
    let i = rest.find(['?', '#']).unwrap_or(rest.len());
    let path = &rest[..i];
    rest = &rest[i..];
    let mut query = None;
    if let Some(r) = rest.strip_prefix('?') {
        let i = r.find('#').unwrap_or(r.len());
        query = Some(&r[..i]);
        rest = &r[i..];
    }
    let fragment = rest.strip_prefix('#');
    Parts { scheme, authority, path, query, fragment }
}

fn unreserved(c: u8) -> bool {
    c.is_ascii_alphanumeric() || matches!(c, b'-' | b'.' | b'_' | b'~')
}

fn sub_delim(c: u8) -> bool {
    matches!(c, b'!' | b'$' | b'&' | b'\'' | b'(' | b')' | b'*' | b'+' | b',' | b';' | b'=')
}

/// Does `s` consist of the bytes `ok` allows and of percent-encodings `%XX`?
fn chars(s: &str, ok: impl Fn(u8) -> bool) -> bool {
    let b = s.as_bytes();
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' {
            if i + 2 >= b.len() || !b[i + 1].is_ascii_hexdigit() || !b[i + 2].is_ascii_hexdigit() {
                return false;
            }
            i += 3;
        } else if ok(b[i]) {
            i += 1;
        } else {
            return false;
        }
    }
    true
}

fn pchar(c: u8) -> bool {
    unreserved(c) || sub_delim(c) || c == b':' || c == b'@'
}

fn authority_ok(a: &str) -> bool {
    let hostport = match a.find('@') {
        Some(i) => {
            if !chars(&a[..i], |c| unreserved(c) || sub_delim(c) || c == b':') {
                return false;
            }
            &a[i + 1..]
        }
        None => a,
    };
    let port = if let Some(r) = hostport.strip_prefix('[') {
        let Some(j) = r.find(']') else { return false };
        let lit = &r[..j];
        if lit.is_empty() || !lit.bytes().all(|c| c.is_ascii_hexdigit() || matches!(c, b':' | b'.' | b'v' | b'V') || unreserved(c) || sub_delim(c)) {
            return false;
        }
        &r[j + 1..]
    } else {
        let j = hostport.find(':').unwrap_or(hostport.len());
        if !chars(&hostport[..j], |c| unreserved(c) || sub_delim(c)) {
            return false;
        }
        &hostport[j..]
    };
    port.is_empty() || (port.starts_with(':') && port[1..].bytes().all(|c| c.is_ascii_digit()))
}

/// Does `r` match the URI-reference of RFC 3986 §4.1?
pub fn is_uri_reference(r: &str) -> bool {
    if !r.is_ascii() {
        return false;
    }
    let p = split(r);
    if let Some(s) = p.scheme {
        let b = s.as_bytes();
        if !b[0].is_ascii_alphabetic() || !b.iter().all(|&c| c.is_ascii_alphanumeric() || matches!(c, b'+' | b'.' | b'-')) {
            return false;
        }
    }
    if let Some(a) = p.authority
        && !authority_ok(a) {
            return false;
        }
    if !chars(p.path, |c| pchar(c) || c == b'/') {
        return false;
    }
    if p.scheme.is_none() && p.authority.is_none() && p.path.split('/').next().unwrap_or("").contains(':') {
        return false; // a relative-path reference: its first segment has no colon
    }
    [p.query, p.fragment].iter().flatten().all(|x| chars(x, |c| pchar(c) || c == b'/' || c == b'?'))
}

/// RFC 3986 §5.2.4.
pub fn remove_dot_segments(path: &str) -> String {
    let mut out: Vec<&str> = Vec::new();
    let mut p = path;
    while !p.is_empty() {
        if let Some(r) = p.strip_prefix("../") {
            p = r;
        } else if let Some(r) = p.strip_prefix("./") {
            p = r;
        } else if p.starts_with("/./") {
            p = &p[2..];
        } else if p == "/." {
            p = "/";
        } else if p.starts_with("/../") {
            p = &p[3..];
            out.pop();
        } else if p == "/.." {
            p = "/";
            out.pop();
        } else if p == "." || p == ".." {
            p = "";
        } else {
            let i = p[1..].find('/').map_or(p.len(), |i| i + 1);
            out.push(&p[..i]);
            p = &p[i..];
        }
    }
    out.concat()
}

fn compose(scheme: Option<&str>, authority: Option<&str>, path: &str, query: Option<&str>, fragment: Option<&str>) -> String {
    let mut s = String::new();
    if let Some(x) = scheme {
        s += x;
        s.push(':');
    }
    if let Some(x) = authority {
        s += "//";
        s += x;
    }
    s += path;
    if let Some(x) = query {
        s.push('?');
        s += x;
    }
    if let Some(x) = fragment {
        s.push('#');
        s += x;
    }
    s
}

/// Strict RFC 3986 §5.2.2 resolution of `r` against `base`; an error when `r` is not a URI
/// reference.
pub fn resolve(base: &str, r: &str) -> Result<String, String> {
    if !is_uri_reference(r) {
        return Err(format!("not a valid URI reference: {r:?}"));
    }
    let b = split(base);
    let x = split(r);
    if x.scheme.is_some() {
        return Ok(compose(x.scheme, x.authority, &remove_dot_segments(x.path), x.query, x.fragment));
    }
    if x.authority.is_some() {
        return Ok(compose(b.scheme, x.authority, &remove_dot_segments(x.path), x.query, x.fragment));
    }
    if x.path.is_empty() {
        return Ok(compose(b.scheme, b.authority, b.path, x.query.or(b.query), x.fragment));
    }
    let path = if x.path.starts_with('/') {
        remove_dot_segments(x.path)
    } else if b.authority.is_some() && b.path.is_empty() {
        remove_dot_segments(&format!("/{}", x.path))
    } else {
        let cut = b.path.rfind('/').map_or(0, |i| i + 1);
        remove_dot_segments(&format!("{}{}", &b.path[..cut], x.path))
    };
    Ok(compose(b.scheme, b.authority, &path, x.query, x.fragment))
}

/// Percent-encodes every byte of `data` but the unreserved ones and those of `safe`, as Python's
/// `urllib.parse.quote(data, safe)` does.
pub fn quote(data: &[u8], safe: &str) -> String {
    let mut s = String::with_capacity(data.len());
    for &c in data {
        if c.is_ascii_alphanumeric() || matches!(c, b'_' | b'.' | b'-' | b'~') || safe.as_bytes().contains(&c) {
            s.push(c as char);
        } else {
            s += &format!("%{c:02X}");
        }
    }
    s
}

/// The bytes a base URI's path writes as they are (spec/refkv.md §5
/// <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#5-source-table>); every other byte is
/// percent-encoded.
const PATH_SAFE: &str = "/:@!$&'()*+,;=";

/// `posixpath.normpath`.
fn posix_normpath(path: &str) -> String {
    if path.is_empty() {
        return ".".into();
    }
    let slashes = path.len() - path.trim_start_matches('/').len();
    let initial = match slashes {
        0 => "",
        2 => "//",
        _ => "/",
    };
    let mut out: Vec<&str> = Vec::new();
    for c in path.split('/') {
        if c.is_empty() || c == "." {
            continue;
        }
        if c != ".." || (initial.is_empty() && out.is_empty()) || out.last() == Some(&"..") {
            out.push(c);
        } else if !out.is_empty() {
            out.pop();
        }
    }
    let s = format!("{initial}{}", out.join("/"));
    if s.is_empty() { ".".into() } else { s }
}

/// The `file:` base URI of a POSIX path, joined to `cwd` when relative: absolute, lexical,
/// percent-encoded.
pub fn posix_file_uri(path: &str, cwd: &str) -> String {
    let joined = if path.starts_with('/') {
        path.to_string()
    } else if cwd.is_empty() || cwd.ends_with('/') {
        format!("{cwd}{path}")
    } else {
        format!("{cwd}/{path}")
    };
    let mut abs = posix_normpath(&joined);
    if abs.starts_with("//") {
        abs = format!("/{}", abs.trim_start_matches('/'));
    }
    format!("file://{}", quote(abs.as_bytes(), PATH_SAFE))
}

/// `ntpath.splitdrive`: (drive, the rest).
pub fn windows_split_drive(p: &str) -> (&str, &str) {
    let norm = p.replace('/', "\\");
    let n = norm.as_bytes();
    if n.first() == Some(&b'\\') {
        if n.get(1) == Some(&b'\\') {
            let start = if norm.len() >= 8 && norm[..8].eq_ignore_ascii_case("\\\\?\\UNC\\") { 8 } else { 2 };
            let Some(i) = norm[start..].find('\\').map(|i| i + start) else {
                return (p, "");
            };
            let Some(j) = norm[i + 1..].find('\\').map(|j| j + i + 1) else {
                return (p, "");
            };
            return (&p[..j], &p[j..]);
        }
        return ("", p);
    }
    if n.get(1) == Some(&b':') {
        return (&p[..2], &p[2..]);
    }
    ("", p)
}

/// The `file:` URI of a normalized, absolute Windows path (RFC 8089 Appendix E): a drive-letter
/// path `C:\data\x` is `file:///C:/data/x`, and a UNC path `\\server\share\x` is
/// `file://server/share/x`.
pub fn windows_file_uri(abs: &str) -> Result<String, String> {
    let mut abs = abs.to_string();
    if abs.starts_with("\\\\?\\") || abs.starts_with("\\\\.\\") {
        let rest = abs[4..].to_string();
        let verbatim = abs.as_bytes()[2] == b'?';
        let rb = rest.as_bytes();
        if verbatim && rest.len() >= 4 && rest[..4].eq_ignore_ascii_case("UNC\\") {
            abs = format!("\\\\{}", &rest[4..]);
        } else if verbatim && rb.len() >= 3 && rb[0].is_ascii_alphabetic() && rb[1] == b':' && rb[2] == b'\\' {
            abs = rest;
        } else {
            return Err(format!("{abs:?} is a device path, which no file: URI names"));
        }
    }
    let (drive, rest) = windows_split_drive(&abs);
    let mut rest = rest.to_string();
    if !rest.starts_with('\\') {
        if !drive.starts_with("\\\\") || !rest.is_empty() {
            return Err(format!("{abs:?} is not absolute: it has no drive and root"));
        }
        rest = "\\".into();
    }
    let path = rest.replace('\\', "/");
    let d = drive.as_bytes();
    if d.len() == 2 && d[0].is_ascii_alphabetic() && d[1] == b':' {
        return Ok(format!("file://{}", quote(format!("/{drive}{path}").as_bytes(), PATH_SAFE)));
    }
    let (server, share) = drive[2..].split_once('\\').unwrap_or((&drive[2..], ""));
    if share.is_empty() || server.is_empty() || !server.bytes().all(|c| unreserved(c) || sub_delim(c)) {
        return Err(format!("{abs:?}: a UNC path is \\\\server\\share\\..., with a plain host name"));
    }
    Ok(format!("file://{server}{}", quote(format!("/{share}{path}").as_bytes(), PATH_SAFE)))
}

/// The base URI of a local path (spec/refkv.md §5
/// <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#5-source-table>): absolute, lexical,
/// percent-encoded.
pub fn file_uri(path: &std::path::Path) -> Result<String, String> {
    if cfg!(windows) {
        let abs = std::path::absolute(path).map_err(|e| format!("{}: {e}", path.display()))?;
        windows_file_uri(&abs.to_string_lossy())
    } else {
        let cwd = std::env::current_dir().map_err(|e| e.to_string())?;
        Ok(posix_file_uri(&path.to_string_lossy(), &cwd.to_string_lossy()))
    }
}

/// The URI reference a source read from the local path `path` is named by, when no URL is given:
/// the path's `/`-separated segments, percent-encoded as spec/refkv.md §5
/// <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#5-source-table> requires (so
/// `/data/a b.nii` is `/data/a%20b.nii`). An absolute POSIX path is a path-absolute reference, a
/// relative one is relative to the archive, and a relative one whose first segment holds `:` is
/// written `./` first, so that the segment is not read as a scheme. On Windows, where a drive
/// letter is not URI syntax, an absolute path is its `file_uri`.
pub fn path_reference(path: &str) -> Result<String, String> {
    if !cfg!(windows) {
        return Ok(posix_path_reference(path));
    }
    windows_path_reference(path, |p| file_uri(std::path::Path::new(p)))
}

/// `path_reference` on POSIX.
pub fn posix_path_reference(path: &str) -> String {
    // `//x` is `/x` on Linux and macOS, not an authority
    let path = if path.starts_with("//") { format!("/{}", path.trim_start_matches('/')) } else { path.to_string() };
    encoded_reference(&path)
}

/// `path_reference` on Windows, with `file_uri` the function that names an absolute path.
pub fn windows_path_reference(path: &str, file_uri: impl Fn(&str) -> Result<String, String>) -> Result<String, String> {
    let (drive, rest) = windows_split_drive(path);
    if !drive.is_empty() || rest.starts_with(['\\', '/']) {
        return file_uri(path);
    }
    Ok(encoded_reference(&path.replace('\\', "/")))
}

/// The percent-encoded reference of a `/`-separated path, `./` first when its first segment
/// holds `:`.
fn encoded_reference(path: &str) -> String {
    let r = quote(path.as_bytes(), PATH_SAFE);
    if r.split('/').next().unwrap_or("").contains(':') { format!("./{r}") } else { r }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn uri_references() {
        for (r, ok) in [
            ("", true),
            ("a/b", true),
            ("/abs?q#f", true),
            ("http://h:80/p", true),
            ("http://u:p@h/", true),
            ("http://[::1]:8000/x", true),
            ("http://h:/x", true),
            ("file:///C:/data/x.vzip", true),
            ("x%20y", true),
            ("./a:b", true),
            ("?q", true),
            ("a:b", true),
            ("1a:b", false),
            ("a b", false),
            ("x%2", false),
            ("x%zz", false),
            ("é", false),
            ("http://h h/", false),
            ("http://[]/", false),
            ("http://h:8a/", false),
            ("http://a@b@c/", false),
            ("p#f#g", false),
            ("p?q[", false),
        ] {
            assert_eq!(is_uri_reference(r), ok, "{r:?}");
        }
    }

    #[test]
    fn resolution() {
        // RFC 3986 §5.4
        let base = "http://a/b/c/d;p?q";
        for (r, want) in [
            ("g:h", "g:h"),
            ("g", "http://a/b/c/g"),
            ("./g", "http://a/b/c/g"),
            ("g/", "http://a/b/c/g/"),
            ("/g", "http://a/g"),
            ("//g", "http://g"),
            ("?y", "http://a/b/c/d;p?y"),
            ("g?y", "http://a/b/c/g?y"),
            ("#s", "http://a/b/c/d;p?q#s"),
            ("", "http://a/b/c/d;p?q"),
            (".", "http://a/b/c/"),
            ("..", "http://a/b/"),
            ("../g", "http://a/b/g"),
            ("../../../g", "http://a/g"),
            ("/./g", "http://a/g"),
            ("g.", "http://a/b/c/g."),
            ("g/../h", "http://a/b/c/h"),
            ("g;x=1/../y", "http://a/b/c/y"),
        ] {
            assert_eq!(resolve(base, r).unwrap(), want, "{r:?}");
        }
        assert_eq!(resolve("http://h", "x").unwrap(), "http://h/x");
        assert_eq!(resolve("file:///data/a.vzip", "img.tif").unwrap(), "file:///data/img.tif");
    }

    #[test]
    fn resolve_rejects_invalid_reference() {
        assert!(resolve("http://h/", "a b").is_err());
    }

    #[test]
    fn file_uris() {
        for (path, cwd, want) in [
            ("/data/a b.vzip", "/x", "file:///data/a%20b.vzip"),
            ("a.vzip", "/x/y", "file:///x/y/a.vzip"),
            ("../a.vzip", "/x/y/", "file:///x/a.vzip"),
            ("./d/./e/../f", "/", "file:///d/f"),
            ("//srv/a", "/", "file:///srv/a"),
            ("///srv/a", "/", "file:///srv/a"),
            ("/é", "/", "file:///%C3%A9"),
            ("/a:b@c", "/", "file:///a:b@c"),
        ] {
            assert_eq!(posix_file_uri(path, cwd), want, "{path:?}");
        }
        for (abs, want) in [
            ("C:\\data\\x.vzip", "file:///C:/data/x.vzip"),
            ("c:\\a b", "file:///c:/a%20b"),
            ("\\\\server\\share\\x", "file://server/share/x"),
            ("\\\\server\\share", "file://server/share/"),
            ("\\\\?\\C:\\x", "file:///C:/x"),
            ("\\\\?\\UNC\\server\\share\\x", "file://server/share/x"),
        ] {
            assert_eq!(windows_file_uri(abs).unwrap(), want, "{abs:?}");
        }
    }

    #[test]
    fn windows_file_uri_rejects_device_path() {
        assert!(windows_file_uri("\\\\.\\PhysicalDrive0").is_err());
    }

    #[test]
    fn windows_file_uri_rejects_relative_path() {
        assert!(windows_file_uri("C:data").is_err());
    }

    #[test]
    fn windows_file_uri_rejects_bad_unc_host() {
        assert!(windows_file_uri("\\\\ser%ver\\share\\x").is_err());
    }

    #[test]
    fn path_references() {
        let abs = |p: &str| Ok(format!("ABS:{p}"));
        for (path, want) in [
            ("C:\\data\\x.tif", "ABS:C:\\data\\x.tif"),
            ("\\x.tif", "ABS:\\x.tif"),
            ("/x.tif", "ABS:/x.tif"),
            ("C:x.tif", "ABS:C:x.tif"),
            ("d\\a b.tif", "d/a%20b.tif"),
            ("a:b\\c", "ABS:a:b\\c"),
            ("ab:c\\d", "./ab:c/d"),
            ("d d\\100%#é.tif", "d%20d/100%25%23%C3%A9.tif"),
        ] {
            assert_eq!(windows_path_reference(path, abs).unwrap(), want, "{path:?}");
        }
        for (path, want) in [
            ("/data/a b.nii", "/data/a%20b.nii"),
            ("x/y.nii", "x/y.nii"),
            ("/d d/100%#1?.nii", "/d%20d/100%25%231%3F.nii"),
            ("/é/ü.nii", "/%C3%A9/%C3%BC.nii"),
            ("/a:b@c/x;y=z.nii", "/a:b@c/x;y=z.nii"),
            ("a:b/c.nii", "./a:b/c.nii"),
            ("//srv/a.nii", "/srv/a.nii"),
            ("a\\b.nii", "a%5Cb.nii"),
        ] {
            assert_eq!(posix_path_reference(path), want, "{path:?}");
        }
        if !cfg!(windows) {
            assert_eq!(path_reference("fixtures/a b.tif").unwrap(), "fixtures/a%20b.tif");
        }
        assert_eq!(windows_split_drive("\\\\srv\\share\\x"), ("\\\\srv\\share", "\\x"));
        assert_eq!(windows_split_drive("\\\\srv"), ("\\\\srv", ""));
        assert_eq!(windows_split_drive("C:\\x"), ("C:", "\\x"));
        assert_eq!(windows_split_drive("x"), ("", "x"));
    }
}
