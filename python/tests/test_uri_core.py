"""refkv.uri's RFC 3986 rules against the Rust core's (rust/refkv-uri, by name through
`_core.coding`): over a generated corpus of references and bases, both accept the same references
and resolve each to the same string, since a zarrifier writes a reference as it is given (spec §5
<https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#5-source-table>) and every host must read
it alike."""

from __future__ import annotations

import itertools
import json

from refkv import _core
from refkv.uri import is_uri_reference, resolve

SCHEMES = [None, "", "http", "file", "a", "A", "1a", "a+b.c-d", "a_b"]
AUTHORITIES = [
    None, "", "h", "H.EXAMPLE", "h:80", "h:", "h:8a", "u:p@h", "u@h:1", "a@b@c", "u%zz@h", "%41", "%4",
    "h h", "1.2.3.4", "[::1]", "[::1]:8000", "[::1]x", "[::1", "[]", "[v1.x]", "[v.x]", "[V1.:]",
    "[hello]", "[1::2::3]", "[::ffff:1.2.3.4]", "[fe80::1%25en0]", "h\n", "\n", "é",
]
PATHS = [
    "", "/", "a", "a/b", "/a/b", ".", "..", "./a", "../a", "/./a", "/../a", "a/./b", "a/../b",
    "/a/..", "/a/.", "/a/b/../../..", "g;x=1/../y", "a//b", ".//b", "/..//b", "//a", "a:b", "./a:b",
    "a/b:c", "a@b", "x%20y", "%2E%2E", "/%2e/x", "x%2", "x%zz", "%", "a b", "a\\b", "[x]", "é", "\n",
    "a\n",
]
QUERIES = [None, "", "q", "q?r/s", "a=b&c", "%41", "%x", "q[", "é", "a\nb"]
FRAGMENTS = [None, "", "f", "f?/", "f#g", "%", "a b", "a\nb"]
BASES = [
    "http://a/b/c/d;p?q", "http://a/b/c/d;p?q#frag", "http://h", "http://h/x?", "https://u@h:443/p/q/",
    "file:///data/a.vzip", "s3://bucket/key/", "s://h", "scheme:", "scheme:foo/bar", "mailto:a@b",
    "foo:/", "foo:/.", "a:/b/..", "x:/bar/baz/.%2E/", "http://h/a b/", "rel/base", "http://h/#a\nb",
    "http://h/?a\nb",
]
# RFC 3986 §5.4, against its base http://a/b/c/d;p?q
EXAMPLES = [
    "g:h", "g", "./g", "g/", "/g", "//g", "?y", "g?y", "#s", "g#s", "g?y#s", ";x", "g;x", "g;x?y#s", "",
    ".", "./", "..", "../", "../g", "../..", "../../", "../../g", "../../../g", "../../../../g", "/./g",
    "/../g", "g.", ".g", "g..", "..g", "./../g", "./g/.", "g/./h", "g/../h", "g;x=1/./y", "g;x=1/../y",
    "g?y/./x", "g?y/../x", "g#s/./x", "g#s/../x", "http:g",
]


def compose(scheme: str | None, authority: str | None, path: str, query: str | None, fragment: str | None) -> str:
    s = "" if scheme is None else scheme + ":"
    s += "" if authority is None else "//" + authority
    s += path
    s += "" if query is None else "?" + query
    return s + ("" if fragment is None else "#" + fragment)


def core(name: str, **params: str) -> object:
    try:
        return json.loads(_core.coding(name, json.dumps(params)))
    except _core.Rejected:
        return "rejected"


def python_resolve(base: str, ref: str) -> str:
    try:
        return resolve(base, ref)
    except ValueError:
        return "rejected"


def test_python_and_core_agree():
    refs = sorted(
        {compose(s, a, p, None, None) for s, a, p in itertools.product(SCHEMES, AUTHORITIES, PATHS)}
        | {compose(None, a, p, q, f) for a, p, q, f in itertools.product([None, "h"], PATHS, QUERIES, FRAGMENTS)}
    )
    relative = [r for r in refs if not r.startswith(("/", "?", "#")) and ":" not in r.split("/", 1)[0]]
    pairs = (
        [(BASES[i % len(BASES)], r) for i, r in enumerate(refs)]
        + [(b, r) for b in BASES for r in relative[::7]]
        + [("http://a/b/c/d;p?q", r) for r in EXAMPLES]
    )
    accepted = [r for r in refs if is_uri_reference(r) != core("is_uri_reference", ref=r)]
    assert accepted == [], f"{len(accepted)} references accepted by one only, e.g. {accepted[:5]}"
    resolved = [(b, r) for b, r in pairs if python_resolve(b, r) != core("resolve_reference", base=b, ref=r)]
    assert resolved == [], f"{len(resolved)} resolutions differ, e.g. {resolved[:5]}"
