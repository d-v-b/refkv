"""RFC 3986 URI references, strict resolution, and the file: rules of spec §5
<https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#5-source-table>."""

from __future__ import annotations

import ntpath
import os
import re
from pathlib import Path
from types import ModuleType
from typing import cast
from urllib.parse import quote, unquote, urlsplit

_PCT = r"%[0-9A-Fa-f]{2}"
_UNRESERVED = r"A-Za-z0-9\-._~"
_SUB = r"!$&'()*+,;="
_PCHAR = rf"(?:[{_UNRESERVED}{_SUB}:@]|{_PCT})"
_SPLIT = re.compile(r"^(?:([^:/?#]+):)?(?://([^/?#]*))?([^?#]*)(?:\?([^#]*))?(?:#(.*))?\Z")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*\Z")
_USERINFO_HOST_PORT = re.compile(
    rf"^(?:(?:[{_UNRESERVED}{_SUB}:]|{_PCT})*@)?"
    rf"(?:\[[0-9A-Fa-f:.vV{_UNRESERVED}{_SUB}]+\]|(?:[{_UNRESERVED}{_SUB}]|{_PCT})*)"
    r"(?::[0-9]*)?$"
)
_PATH = re.compile(rf"^(?:{_PCHAR}|/)*\Z")
_QUERY = re.compile(rf"^(?:{_PCHAR}|[/?])*\Z")


def is_uri_reference(ref: str) -> bool:
    """Does `ref` match RFC 3986 URI-reference (RFC 3986 §4.1)?"""
    m = _SPLIT.match(ref)
    if not m or not ref.isascii():
        return False
    scheme, authority, path, query, fragment = m.groups()
    if scheme is not None and not _SCHEME.match(scheme):
        return False
    if authority is not None and not _USERINFO_HOST_PORT.match(authority):
        return False
    if not _PATH.match(path):
        return False
    if authority is not None and path and not path.startswith("/"):
        return False
    if scheme is None and authority is None:
        first = path.split("/", 1)[0]
        if ":" in first:  # relative-path reference: first segment has no colon
            return False
    return all(x is None or _QUERY.match(x) for x in (query, fragment))


def _remove_dot_segments(path: str) -> str:
    """RFC 3986 §5.2.4."""
    out: list[str] = []
    while path:
        if path.startswith("../"):
            path = path[3:]
        elif path.startswith(("./", "/./")):
            path = path[2:]
        elif path == "/.":
            path = "/"
        elif path.startswith("/../"):
            path = path[3:]
            if out:
                out.pop()
        elif path == "/..":
            path = "/"
            if out:
                out.pop()
        elif path in (".", ".."):
            path = ""
        else:
            i = path.find("/", 1)
            seg, path = (path, "") if i < 0 else (path[:i], path[i:])
            out.append(seg)
    return "".join(out)


def _compose(scheme: str | None, authority: str | None, path: str, query: str | None, fragment: str | None) -> str:
    """RFC 3986 §5.3; a defined-but-empty component keeps its delimiter."""
    s = f"{scheme}:" if scheme is not None else ""
    if authority is not None:
        s += "//" + authority
    s += path
    if query is not None:
        s += "?" + query
    if fragment is not None:
        s += "#" + fragment
    return s


def resolve(base: str, ref: str) -> str:
    """Strict RFC 3986 §5.2.2 resolution (urllib's urljoin drops empty queries)."""
    if not is_uri_reference(ref):
        raise ValueError(f"not a valid URI reference: {ref!r}")
    bs, ba, bp, bq, _ = cast("re.Match[str]", _SPLIT.match(base)).groups()  # which every string matches
    rs, ra, rp, rq, rf = cast("re.Match[str]", _SPLIT.match(ref)).groups()
    if rs is not None:
        return _compose(rs, ra, _remove_dot_segments(rp), rq, rf)
    if ra is not None:
        return _compose(bs, ra, _remove_dot_segments(rp), rq, rf)
    if rp == "":
        return _compose(bs, ba, bp, rq if rq is not None else bq, rf)
    if rp.startswith("/"):
        path = _remove_dot_segments(rp)
    elif ba is not None and bp == "":
        path = _remove_dot_segments("/" + rp)
    else:
        path = _remove_dot_segments(bp[: bp.rfind("/") + 1] + rp)
    return _compose(bs, ba, path, rq, rf)


# spec §5 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#5-source-table>: the bytes a
# base URI's path writes as they are; every other byte is percent-encoded
_PATH_SAFE = "/:@!$&'()*+,;="
_HOST = re.compile(rf"[{_UNRESERVED}{_SUB}]+\Z")  # a reg-name with no percent-encoding
_DRIVE = re.compile(r"[A-Za-z][:|]\Z")  # RFC 8089 Appendix E.2: `C:`, or the older `C|`


def _windows(pathmod: ModuleType) -> bool:
    """Do `pathmod`'s paths follow Windows' rules (ntpath), not POSIX's?"""
    return pathmod.sep == "\\"


def is_url(location: str, *, pathmod: ModuleType = os.path) -> bool:
    """Is `location` a URL (it has a scheme), not a local path? On Windows a path
    with a drive letter (`C:\\data`, `C:data`) is a path, though RFC 3986 would read
    its drive as a scheme."""
    if _windows(pathmod) and re.match(r"[A-Za-z]:", location):
        return False
    return bool(urlsplit(location).scheme)


def file_uri(path: str | os.PathLike[str], *, pathmod: ModuleType = os.path, cwd: str | None = None) -> str:
    """Base URI of a local path (spec §5
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#5-source-table>): absolute, lexical,
    percent-encoded. `pathmod` is the path flavor, `os.path` by default (posixpath or
    ntpath), and `cwd` the directory a relative path is joined to (default `os.getcwd()`).

    A POSIX path `/data/a b.vzip` gives `file:///data/a%20b.vzip`. A Windows path, by
    RFC 8089 Appendix E <https://www.rfc-editor.org/rfc/rfc8089#appendix-E>, writes `\\`
    as `/`: a drive-letter path `C:\\data\\x.vzip` gives `file:///C:/data/x.vzip`, and a
    UNC path `\\\\server\\share\\x` gives `file://server/share/x`, the server as the
    authority (which `file_path` maps back only when the reader allows file hosts)."""
    cwd = str(Path.cwd()) if cwd is None else cwd
    abs_path = pathmod.normpath(pathmod.join(cwd, os.fspath(path)))
    if not _windows(pathmod):
        if abs_path.startswith("//"):
            abs_path = "/" + abs_path.lstrip("/")
        return "file://" + quote(abs_path.encode("utf-8"), safe=_PATH_SAFE)
    return _windows_file_uri(abs_path)


def _windows_file_uri(abs_path: str) -> str:
    """`file_uri` of a normalized, joined Windows path."""
    if abs_path.startswith(("\\\\?\\", "\\\\.\\")):
        rest = abs_path[4:]
        if abs_path[2] == "?" and rest[:4].upper() == "UNC\\":  # \\?\UNC\server\share
            abs_path = "\\\\" + rest[4:]
        elif abs_path[2] == "?" and re.match(r"[A-Za-z]:\\", rest):  # \\?\C:\
            abs_path = rest
        else:
            raise ValueError(f"{abs_path!r} is a device path, which no file: URI names")
    drive, rest = ntpath.splitdrive(abs_path)
    if not rest.startswith("\\"):
        if not drive.startswith("\\\\") or rest:
            raise ValueError(f"{abs_path!r} is not absolute: it has no drive and root")
        rest = "\\"
    path = rest.replace("\\", "/")
    if re.fullmatch(r"[A-Za-z]:", drive):
        return "file://" + quote(("/" + drive + path).encode("utf-8"), safe=_PATH_SAFE)
    server, _, share = drive[2:].partition("\\")
    if not share or not _HOST.match(server):
        raise ValueError(f"{abs_path!r}: a UNC path is \\\\server\\share\\..., with a plain host name")
    return "file://" + server + quote(("/" + share + path).encode("utf-8"), safe=_PATH_SAFE)


def path_reference(path: str, *, pathmod: ModuleType = os.path) -> str:
    """The URI reference a source read from the local path `path` is named by, when no
    URL is given: the path's `/`-separated segments, percent-encoded as spec §5
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#5-source-table> requires (so
    `/data/a b.nii` is `/data/a%20b.nii`). An absolute POSIX path is a path-absolute
    reference, a relative one is relative to the archive, and a relative one whose first
    segment holds `:` is written `./` first, so that the segment is not read as a scheme.
    On Windows, where a drive letter is not URI syntax, an absolute path is its
    `file_uri`."""
    if _windows(pathmod):
        drive, rest = ntpath.splitdrive(path)
        if drive or rest.startswith(("\\", "/")):
            return file_uri(path, pathmod=pathmod)
        path = path.replace("\\", "/")
    elif path.startswith("//"):  # `//x` is `/x` on Linux and macOS, not an authority
        path = "/" + path.lstrip("/")
    ref = quote(path.encode("utf-8"), safe=_PATH_SAFE)
    return "./" + ref if ":" in ref.split("/", 1)[0] else ref


def file_path(url: str, *, pathmod: ModuleType = os.path, allow_host: bool = False) -> str:
    """Local path of a file: URI, or ValueError per the rules of spec §5
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#5-source-table>.

    `pathmod` is the path flavor, `os.path` by default. For ntpath (Windows, RFC 8089
    Appendix E <https://www.rfc-editor.org/rfc/rfc8089#appendix-E>), `/C:/data/x` maps to
    `C:\\data\\x`, and a path with no drive letter is refused, since it would depend on
    the current drive. An authority other than `localhost` names a UNC path's server,
    which a reader reaches over the network: refused unless `allow_host` (the reader
    policy's `allow_file_hosts`), and then `file://server/share/x` maps to
    `\\\\server\\share\\x`. A POSIX reader has no such mapping and refuses it always."""
    windows = _windows(pathmod)
    m = cast("re.Match[str]", _SPLIT.match(url))  # which every string matches
    scheme, authority, path, query, _ = m.groups()
    if (scheme or "").lower() != "file":
        raise ValueError(f"not a file: URI: {url!r}")
    host = authority or ""
    local = host.lower() in ("", "localhost")
    if not local and not (windows and allow_host):
        raise ValueError(f"file: URI with authority {authority!r}"
                         + (" (a UNC path: allow it with Policy(allow_file_hosts=True))"
                            if windows and _HOST.match(host) else ""))
    if not local and not _HOST.match(host):
        raise ValueError(f"file: URI with authority {authority!r}: not a plain host name")
    if query is not None:
        raise ValueError("file: URI with a query component")
    if not path.startswith("/"):
        raise ValueError("file: URI with a relative path")
    if re.search(r"%2[fF]|%00", path):
        raise ValueError("file: URI path encodes '/' or NUL")
    if not windows:
        decoded = unquote(path)
        if any(seg in (".", "..") for seg in decoded.split("/")):
            raise ValueError("file: URI path has an encoded '.' or '..' segment")
        return decoded
    if "\\" in path or re.search(r"%5[cC]", path):
        raise ValueError("file: URI path holds '\\', a Windows path separator")
    try:
        decoded = unquote(path, errors="strict")
    except UnicodeDecodeError:
        raise ValueError("file: URI path is not UTF-8, which a Windows path needs") from None
    segs = decoded.split("/")[1:]
    if any(seg in (".", "..") for seg in segs):
        raise ValueError("file: URI path has an encoded '.' or '..' segment")
    if not local:
        if not segs[0]:
            raise ValueError(f"file: URI of the UNC host {host!r} names no share")
        return "\\\\" + host + "\\" + "\\".join(segs)
    if not _DRIVE.match(segs[0]):
        raise ValueError("file: URI path names no drive letter (such as /C:/), which a "
                         "Windows path needs")
    return segs[0][0] + ":\\" + "\\".join(segs[1:])
