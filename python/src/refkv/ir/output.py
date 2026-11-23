"""A zarrifier's output (`Output`), which writes the archive, and an output the Rust core built
(`refkv._core.Run.output`, `refkv._core.Run.project`) as one (`from_rust`)."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from refkv._files import replacing
from refkv.pb import Concat, Range, Source
from refkv.policy import Policy
from refkv.reader import Registry
from refkv.refstore import ReferenceStore, bulk
from refkv.uri import file_path, file_uri
from refkv.uri import resolve as resolve_reference
from refkv.vzip.archive import VZipWriter

if TYPE_CHECKING:
    from refkv import _core

# A range of an output: (offset, length) of source 0, (source, offset, length)
# of any source, or literal bytes.
Part = tuple[int, int] | tuple[int, int, int] | bytes

@dataclass
class Output:
    """A zarrifier's output: the url source 0, the data sources after it,
    and the entries (§1.1
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#11-output-and-equivalence>, §1.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>)."""

    url: str
    bytes_entries: dict[str, bytes] = field(default_factory=dict)
    refs: dict[str, list[Part]] = field(default_factory=dict)
    summary: dict[str, object] = field(default_factory=dict)
    # The data sources 1, 2, ... in order of first use (§1.2
    # <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>), and their indexes.
    data: dict[bytes, int] = field(default_factory=dict)
    # The documents under these prefixes are read only when asked for, like chunks:
    # those of the source metadata node, and of nodes a profile has one of per tile.
    lazy: tuple[str, ...] = ("refkv_source/",)
    # Source 0's pins (§1.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>),
    # and the CRC-32C of a url source's bytes, when the
    # ranges are to carry checksums (spec/refkv.md §4.2
    # <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#42-range>); set by
    # refkv.zarrify._zarrify.
    size: int | None = None
    etag: str | None = None
    checksum: Callable[[str, int, int], int] | None = None
    # The IR the output was projected from, which a plugin's layout is given (spec/layout.md
    # <https://github.com/d-v-b/refkv/blob/main/spec/layout.md>); set by refkv.ir.
    ir: _core.Ir | None = None

    def json(self, key: str, value: object) -> None:
        # UTF-8, not \u escapes: the documents are the size the budgets count, as JSON.stringify writes them.
        self.bytes_entries[key] = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()

    def shared(self, value: bytes) -> tuple[int, int, int]:
        """A range of all of `value`, as a data source (§1.2
        <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>): the source is
        added to the table the first time `value` is used."""
        value = bytes(value)
        i = self.data.setdefault(value, len(self.data) + 1)
        return (i, 0, len(value))

    def write(self, out_path: str) -> None:
        with replacing(out_path) as fh:
            w = VZipWriter(fh, page_size=1 << 16, checksum=self.checksum)
            src = w.source(Source(url=self.url, size=self.size, etag=self.etag))
            for value, i in self.data.items():
                if w.blob(value) != i:
                    raise AssertionError("data sources out of order")

            def rng(r: Part) -> Range:
                if isinstance(r, bytes):
                    return Range(data=r)
                if len(r) == 3:
                    return Range(source=r[0], offset=r[1], length=r[2])
                return Range(source=src, offset=r[0], length=r[1])

            for key in sorted(self.refs):
                w.add_ranges(key, [rng(r) for r in self.refs[key]])
            for key in sorted(self.bytes_entries):
                # The hierarchy's documents are read when the archive opens; those of
                # the source metadata node (and the lazy prefixes), like chunks, only when asked for.
                late = key.endswith("zarr.json") and not key.startswith(self.lazy)
                w.add_bytes(key, self.bytes_entries[key], compress=not late, late=late)
            w.close()

    def to_store(self, *, policy: Policy | None = None, registry: Registry | None = None,
                 info: dict[str, object] | None = None) -> ReferenceStore:
        """The output as a `refkv.ReferenceStore`, which `save_vzip` writes as the archive
        `write` writes, byte for byte (`reference_store`)."""
        sources = [Source(url=self.url, size=self.size, etag=self.etag)]
        for value, i in sorted(self.data.items(), key=lambda item: item[1]):
            if i != len(sources):
                raise AssertionError("data sources out of order")
            sources.append(Source(data=value))

        def rng(r: Part) -> Range:
            if isinstance(r, bytes):
                return Range(data=r)
            if len(r) == 3:
                return Range(source=r[0], offset=r[1], length=r[2])
            return Range(source=0, offset=r[0], length=r[1])

        refs = [(key, [rng(r) for r in self.refs[key]]) for key in sorted(self.refs)]
        entries = [(key, self.bytes_entries[key],
                    key.endswith("zarr.json") and not key.startswith(self.lazy))
                   for key in sorted(self.bytes_entries)]
        return reference_store(refs, entries, sources, checksum=self.checksum, policy=policy, registry=registry,
                               info=info)


PAGE_SIZE = 1 << 16  # the page index of a zarrified archive (spec/bindings/vzip.md §12
# <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#12-page-index-optional>)


def reference_store(refs: Iterable[tuple[str, list[Range]]], entries: Iterable[tuple[str, bytes, bool]],
                    sources: list[Source], *, checksum: Callable[[str, int, int], int] | None = None,
                    policy: Policy | None = None, registry: Registry | None = None,
                    info: dict[str, object] | None = None) -> ReferenceStore:
    """A profile output as a `refkv.ReferenceStore`, in the layout every output's
    `write` gives its archive: the references (key, ranges) first, then the bytes
    entries (key, bytes, late), in the order given; a late entry is read when the
    archive opens (`read_at_open`), and every other one is compressed. With
    `checksum(url, offset, length)`, each range of a url source carries the CRC-32C
    of its bytes (spec/refkv.md §4.2
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#42-range>). The store
    reads its sources under `policy`, with the stores of `registry`."""
    def checked(r: Range) -> Range:
        if checksum is None or r.data is not None or r.crc32c is not None:
            return r
        url = sources[r.source].url
        return r if url is None else Range(r.source, r.offset, r.length, None, checksum(url, r.offset, r.length))

    mapping: dict[str, bytes | Range | Concat] = {}
    late: list[str] = []
    compressed: list[str] = []
    with bulk():
        for key, ranges in refs:
            if key in mapping:
                raise ValueError(f"duplicate key {key!r}")
            done = [checked(r) for r in ranges]
            mapping[key] = done[0] if len(done) == 1 else Concat(tuple(done))
        for key, data, is_late in entries:
            if key in mapping:
                raise ValueError(f"duplicate key {key!r}")
            mapping[key] = data
            (late if is_late else compressed).append(key)
        return ReferenceStore(mapping, sources, read_at_open=late, compressed=compressed, page_size=PAGE_SIZE,
                              policy=reading_own_files(policy, sources), registry=registry, info=info)


def reading_own_files(policy: Policy | None, sources: Iterable[Source]) -> Policy:
    """`policy` (default `Policy()`), which also lets the store a zarrifier just built read the
    local files it names: the input its caller named and the files that input led it to, each
    below the input's directory (spec/zarrify.md §1.9
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#19-sources-beside-the-input>).
    It allows the directory that holds them all, as a prefix, so that a file replaced by a link
    after the store was built is not read through it (spec/refkv.md §6.7
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy> rule 2); a store
    that also names other sources allows each of them by its URL as well, since a prefix list
    replaces rule 1, which allowed them. A saved store does not keep this: whoever opens it
    chooses (rule 1)."""
    policy = policy or Policy()
    if policy.prefixes is not None:
        return policy
    base = file_uri(Path.cwd()) + "/"
    urls = [resolve_reference(base, s.url) for s in sources if s.url is not None]
    files = [u for u in urls if u.lower().startswith("file:")]
    if not files:
        return policy
    paths = [file_path(u, allow_host=policy.allow_file_hosts) for u in files]
    directory = file_uri(os.path.commonpath([str(Path(p).parent) for p in paths]))
    if not directory.endswith("/"):  # a root's URL ends with one already: file:///
        directory += "/"
    others = dict.fromkeys(u for u in urls if not u.lower().startswith("file:"))
    return replace(policy, prefixes=(directory, *others))


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
