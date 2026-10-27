"""refkv.uri's local paths and file: URIs, on POSIX and on Windows (spec §5
<https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#5-source-table>, RFC 8089 Appendix E
<https://www.rfc-editor.org/rfc/rfc8089#appendix-E>). The path flavor is a parameter, so the
Windows cases run on every system."""

import ntpath
import posixpath
import re

import pytest

from refkv.uri import file_path, file_uri, is_uri_reference, is_url, path_reference, resolve

P, W = posixpath, ntpath


def test_file_uri_and_file_path_round_trip():
    """file_uri writes each path as its URL, and file_path maps the URL back to the
    absolute, normalized path (a UNC host with allow_host)."""
    table = [
        # flavor, cwd, path, URL, the path file_path maps it back to
        (P, "/w", "/data/my file.vzip", "file:///data/my%20file.vzip", "/data/my file.vzip"),
        (P, "/w", "x/./y/../z.vzip", "file:///w/x/z.vzip", "/w/x/z.vzip"),
        (P, "/w", "/../a", "file:///a", "/a"),
        (P, "/w", "//a//b", "file:///a/b", "/a/b"),
        (P, "/w", "/é.bin", "file:///%C3%A9.bin", "/é.bin"),
        (P, "/w", "/a@b:c,d;e=f+g!h$i&j'k(l)m*n", "file:///a@b:c,d;e=f+g!h$i&j'k(l)m*n",
         "/a@b:c,d;e=f+g!h$i&j'k(l)m*n"),
        (P, "/w", "/a#b?c%d", "file:///a%23b%3Fc%25d", "/a#b?c%d"),
        (P, "/w", "/", "file:///", "/"),
        (W, "C:\\w", "C:\\data\\x.vzip", "file:///C:/data/x.vzip", "C:\\data\\x.vzip"),
        (W, "C:\\w", "c:\\data\\x.vzip", "file:///c:/data/x.vzip", "c:\\data\\x.vzip"),
        (W, "C:\\w", "D:/data/x.vzip", "file:///D:/data/x.vzip", "D:\\data\\x.vzip"),
        (W, "C:\\w", "rel\\a b.vzip", "file:///C:/w/rel/a%20b.vzip", "C:\\w\\rel\\a b.vzip"),
        (W, "C:\\w", "\\root\\x", "file:///C:/root/x", "C:\\root\\x"),
        (W, "C:\\w", "C:\\..\\a", "file:///C:/a", "C:\\a"),
        (W, "C:\\w", "C:\\", "file:///C:/", "C:\\"),
        (W, "C:\\w", "C:\\é#%.bin", "file:///C:/%C3%A9%23%25.bin", "C:\\é#%.bin"),
        (W, "C:\\w", "\\\\server\\share\\x", "file://server/share/x", "\\\\server\\share\\x"),
        (W, "C:\\w", "\\\\server\\share", "file://server/share/", "\\\\server\\share\\"),
        (W, "\\\\server\\share\\w", "x.vzip", "file://server/share/w/x.vzip",
         "\\\\server\\share\\w\\x.vzip"),
        (W, "C:\\w", "\\\\?\\C:\\x\\..\\y", "file:///C:/y", "C:\\y"),
        (W, "C:\\w", "\\\\?\\UNC\\server\\share\\x", "file://server/share/x",
         "\\\\server\\share\\x"),
    ]
    for flavor, cwd, path, url, back in table:
        assert file_uri(path, pathmod=flavor, cwd=cwd) == url, (flavor.__name__, path)
        assert file_path(url, pathmod=flavor, allow_host=True) == back, (flavor.__name__, url)

    # the other spellings file_path accepts: an absent or localhost authority, the
    # scheme in any case, RFC 8089's `C|`, a fragment (ignored), percent-encoding
    others = [
        (P, "file:/x", "/x"),
        (P, "FILE://LocalHost/x%20y#frag", "/x y"),
        (W, "file:/C:/x", "C:\\x"),
        (W, "file://localhost/C:/x", "C:\\x"),
        (W, "file:///C|/x", "C:\\x"),
        (W, "file:///C:", "C:\\"),
        (W, "file:///C:/a%20b/%C3%A9#frag", "C:\\a b\\é"),
    ]
    for flavor, url, back in others:
        assert file_path(url, pathmod=flavor) == back, (flavor.__name__, url)


def test_path_reference():
    """A local source's default name: the path's `/`-separated segments, percent-encoded
    (a space, `%`, `#`, `?`, non-ASCII), `./` first when a relative path's first segment
    holds `:`; on Windows an absolute path's file: URI."""
    table = [
        (P, "/data/a b.nii", "/data/a%20b.nii"),
        (P, "x/y.nii", "x/y.nii"),
        (P, "/d d/100%#1?.nii", "/d%20d/100%25%231%3F.nii"),
        (P, "/é/ü.nii", "/%C3%A9/%C3%BC.nii"),
        (P, "a b/c[1].nii", "a%20b/c%5B1%5D.nii"),
        (P, "/a:b@c/x;y=z.nii", "/a:b@c/x;y=z.nii"),
        (P, "a:b/c.nii", "./a:b/c.nii"),
        (P, "//srv/a.nii", "/srv/a.nii"),
        (P, "a\\b.nii", "a%5Cb.nii"),
        (W, "C:\\data\\a b.nii", "file:///C:/data/a%20b.nii"),
        (W, "\\\\server\\share\\a.nii", "file://server/share/a.nii"),
        (W, "x\\a b.nii", "x/a%20b.nii"),
        (W, "x\\a:b.nii", "x/a:b.nii"),
        (W, "ab:c.nii", "./ab:c.nii"),
        (W, "d d\\100%#é.nii", "d%20d/100%25%23%C3%A9.nii"),
    ]
    for flavor, path, ref in table:
        got = path_reference(path, pathmod=flavor)
        assert got == ref, (flavor.__name__, path)
        assert is_uri_reference(got), (flavor.__name__, path)
        # resolved against an archive in `cwd`, the reference names the path's file again
        cwd = "/w" if flavor is P else "C:\\w"
        want = flavor.normpath(flavor.join(cwd, path))
        want = "/" + want.lstrip("/") if flavor is P else want
        base = file_uri(cwd, pathmod=flavor, cwd=cwd) + "/"
        assert file_path(resolve(base, got), pathmod=flavor, allow_host=True) == want, (flavor.__name__, path)


def test_is_url():
    table = [
        (P, "https://example.test/a.vzip", True),
        (P, "file:///a.vzip", True),
        (P, "/data/a.vzip", False),
        (P, "a.vzip", False),
        (P, "c:a.vzip", True),
        (W, "https://example.test/a.vzip", True),
        (W, "file:///C:/a.vzip", True),
        (W, "C:\\data\\a.vzip", False),
        (W, "c:/data/a.vzip", False),
        (W, "C:a.vzip", False),
        (W, "\\\\server\\share\\a.vzip", False),
    ]
    for flavor, location, want in table:
        assert is_url(location, pathmod=flavor) is want, (flavor.__name__, location)


# ------------------------------------------------------------- file_path refusals


def test_file_path_refuses_a_host_by_default():
    for flavor in (P, W):
        with pytest.raises(ValueError, match="file: URI with authority 'server'"):
            file_path("file://server/share/x", pathmod=flavor)


def test_file_path_on_posix_refuses_a_host_even_when_allowed():
    with pytest.raises(ValueError, match="file: URI with authority 'server'"):
        file_path("file://server/share/x", pathmod=P, allow_host=True)


def test_file_path_refuses_a_host_with_userinfo_port_or_percent_encoding():
    for host in ("user@server", "server:445", "ser%41ver"):
        with pytest.raises(ValueError, match="not a plain host name"):
            file_path(f"file://{host}/share/x", pathmod=W, allow_host=True)


def test_file_path_refuses_a_unc_host_with_no_share():
    with pytest.raises(ValueError, match="names no share"):
        file_path("file://server/", pathmod=W, allow_host=True)


def test_file_path_refuses_another_scheme():
    with pytest.raises(ValueError, match="not a file: URI"):
        file_path("https://example.test/x")


def test_file_path_refuses_a_query():
    with pytest.raises(ValueError, match="query component"):
        file_path("file:///x?")


def test_file_path_refuses_a_relative_path():
    with pytest.raises(ValueError, match="relative path"):
        file_path("file:x")


def test_file_path_refuses_an_encoded_slash_or_nul():
    for url in ("file:///a%2Fb", "file:///a%2fb", "file:///a%00b"):
        with pytest.raises(ValueError, match="encodes '/' or NUL"):
            file_path(url, pathmod=P)


def test_file_path_refuses_an_encoded_dot_segment():
    for flavor, url in ((P, "file:///a/%2E%2E/b"), (W, "file:///C:/a/%2e/b")):
        with pytest.raises(ValueError, match=re.escape("encoded '.' or '..' segment")):
            file_path(url, pathmod=flavor)


def test_file_path_on_windows_refuses_a_backslash():
    for url in ("file:///C:/a%5Cb", "file:///C:/a%5cb", "file:///C:/a\\b"):
        with pytest.raises(ValueError, match="Windows path separator"):
            file_path(url, pathmod=W)


def test_file_path_on_windows_refuses_bytes_that_are_not_utf8():
    with pytest.raises(ValueError, match="not UTF-8"):
        file_path("file:///C:/%FF", pathmod=W)


def test_file_path_on_windows_refuses_a_path_with_no_drive():
    for url in ("file:///data/x", "file:///", "file:///CC:/x"):
        with pytest.raises(ValueError, match="no drive letter"):
            file_path(url, pathmod=W)


# ------------------------------------------------------------- file_uri refusals


def test_file_uri_refuses_a_device_path():
    for path in ("\\\\.\\COM1", "\\\\?\\GLOBALROOT\\x"):
        with pytest.raises(ValueError, match="device path"):
            file_uri(path, pathmod=W, cwd="C:\\w")


def test_file_uri_refuses_a_drive_relative_path_on_another_drive():
    with pytest.raises(ValueError, match="not absolute"):
        file_uri("D:x", pathmod=W, cwd="C:\\w")


def test_file_uri_refuses_a_unc_path_without_a_plain_host_or_a_share():
    for path in ("\\\\ser ver\\share\\x", "\\\\server"):
        with pytest.raises(ValueError, match="a UNC path is"):
            file_uri(path, pathmod=W, cwd="C:\\w")


# ------------------------------------------------------------- path_reference refusals


def test_path_reference_on_windows_refuses_a_device_path():
    with pytest.raises(ValueError, match="device path"):
        path_reference("\\\\.\\COM1", pathmod=W)
