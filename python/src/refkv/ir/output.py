"""An output the Rust core built (`refkv._core.project`, `refkv._core.Run.output`) as a Python
`Output`, which writes the archive."""

from __future__ import annotations

import json

from refkv.zarrify.common import Output, Part

type RustOutput = tuple[str, list[tuple[str, bytes]], list[tuple[str, list[Part]]], list[bytes], list[str], str]
"""An output as the Rust core gives it: (url, entries, refs, data sources, lazy prefixes,
summary JSON)."""


def from_rust(t: RustOutput) -> Output:
    """(url, entries, refs, data sources, lazy prefixes, summary JSON) as an Output."""
    url, entries, refs, data, lazy, summary = t
    out = Output(url)
    out.bytes_entries = dict(entries)
    out.refs = {k: list(v) for k, v in refs}
    out.data = {bytes(d): i + 1 for i, d in enumerate(data)}
    out.lazy = tuple(lazy)
    out.summary = json.loads(summary)
    return out
