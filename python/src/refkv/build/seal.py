"""`seal`: the check that vouches for an output built with `refkv.build`, and the store it makes
(the design of d-v-b/refkv#278, "Building and writing"). The core numbers the source table and
checks every key, range and payload (`refkv._core.seal`, the function every built-in
output passes through); this module checks what only the host knows (the caller's objects, the
inputs, the reader policy's URL rules, the placements and the archive) and reads the checksums."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, BinaryIO, cast

from refkv import Concat, Range, ReferenceStore, Source, _core
from refkv.errors import ResolutionError
from refkv.policy import MAX_SOURCES, Policy, crc32c
from refkv.reader import Registry, Resolver
from refkv.uri import file_uri, resolve
from refkv.vzip.names import INDEX_KEY, SOURCES_KEY
from refkv.zarrify.common import PAGE_SIZE, reading_own_files
from refkv.zarrify.layouts import (
    DOCUMENTS,
    MAX_OPEN,
    PAGE_ENTRY,
    STORED,
    _Counting,
    _deflate_bound,
)

from .parsed import Parsed
from .values import Copy, Data, Literal, Part, Ref, Shared, Value, Written, Zeros, value

__all__ = ["CODES", "SealError", "seal"]

U64 = (1 << 64) - 1  # the largest offset or length a range holds
LARGEST_CRC = (1 << 32) - 1  # the checksum whose field is the widest
# what a SealError's code names
CODES = frozenset({"key", "value", "range", "payload", "checksums", "copy", "inputs", "open", "archive"})


class SealError(ValueError):
    """Why `seal` refuses an output: `code` is one of `CODES` (`key`; `value`, an entry or part
    of the wrong type; `range`; `payload`; `checksums`; `copy`; `inputs`, including
    arguments that do not go with them; `open`, a placement; `archive`), and `key` the entry's
    key, or None for the output as a whole."""

    def __init__(self, code: str, key: str | None, message: str) -> None:
        super().__init__(f"{key}: {message}" if key else message)
        self.code = code
        self.key = key


def seal(written: Mapping[str, Any], inputs: Sequence[Parsed], *, policy: Policy | None = None,
         registry: Registry | None = None, info: Mapping[str, object] | None = None) -> ReferenceStore:
    """The output `written` (a `Written`, or a mapping of keys to bytes or values) as a
    `refkv.ReferenceStore`, its references read from `inputs`. Raises `SealError` for an output it
    does not vouch for. `info` is the store's (`ReferenceStore.info`).

    The source table is numbered by first use over the references in key order (UTF-8), the url
    sources first, then the data sources (each distinct `Shared` and `Data` byte string, and one
    zero data source, as long as the largest `Zeros` part); one input that is a plain file is
    source 0 whether or not a value uses it. Every payload is at most 65519 bytes, and when the
    inputs were read with checksums, with each url range's checksum at its largest (6 bytes), so
    that whether an output is sealed never depends on its bytes. How many reads or bytes one value
    takes is its reader's limit (spec/refkv.md §6.7
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy> rule 5), which
    `seal` does not hold an output to: another reader may allow more."""
    if not isinstance(written, Mapping):
        raise SealError("value", None, f"an output is a mapping of keys to entries, not {type(written).__name__}")
    if policy is not None and not isinstance(policy, Policy):
        raise SealError("inputs", None, f"a policy is a refkv.Policy, not {type(policy).__name__}")
    if info is not None and not isinstance(info, Mapping):
        raise SealError("inputs", None, f"info is a mapping, not {type(info).__name__}")
    try:
        inputs = list(inputs)
    except TypeError as e:
        raise SealError("inputs", None, f"inputs are a sequence of what refkv.build.read returns: {e}") from e
    _check_inputs(inputs, policy, registry)
    try:
        entries = written if isinstance(written, Written) else Written(written)
    except (TypeError, ValueError, AttributeError) as e:
        raise SealError("value", None, f"an entry is bytes or a value: {e}") from e
    for k in entries:
        if type(k) is not str:
            raise SealError("key", repr(k), f"keys are str, not {type(k).__name__}")
        try:
            k.encode("utf-8")
        except UnicodeEncodeError:
            raise SealError("key", repr(k), "a key is valid UTF-8 (it holds a lone surrogate)") from None
    keys = sorted(entries, key=lambda k: k.encode())

    table = _Table(inputs)
    values: dict[str, Value] = {}
    for k in keys:
        v = entries[k]
        if isinstance(v, bytes):
            continue
        val = value(v)
        for p in val.parts:
            table.use(k, p)
        values[k] = val
    used = policy if policy is not None else (inputs[0].policy if inputs else None)
    rules = used or Policy()  # what the store's reader allows: refkv's limits, or fewer
    max_sources = min(rules.max_sources, MAX_SOURCES)
    checksums = bool(inputs) and inputs[0].checksums
    request = (table.urls, table.data, table.first, [k for k in keys if k not in values],
               [(k, [table.piece(p) for p in val.parts]) for k, val in values.items()], max_sources)
    # every check that refuses the output runs before the checksums' measure, which fails the call
    # instead: what a refusal is never depends on the order of the checks
    got, sealed = _core_seal(*request, False)
    sources = [Source(url=table.urls[i][0], size=table.urls[i][1], etag=table.urls[i][2]) if kind == "url"
               else Source(data=table.data[i] if kind == "data" else bytes(i)) for kind, i in got]
    resolver = registry if registry is not None else _first(inputs, "registry_given")
    used = rules = _check_policy(sources, used, resolver)
    for k in [*entries.at_open, *entries.stored]:
        if not isinstance(entries.get(k), bytes):
            raise SealError("open", k, "a placement names a key that is not a bytes entry of the output")
    documents: dict[str, bytes] = {}
    at_open: set[str] = set()
    compressed: set[str] = set()
    for k in keys:
        v = entries[k]
        if not isinstance(v, bytes):
            continue
        documents[k] = v
        if entries.at_open.get(k, k.rpartition("/")[2] in DOCUMENTS):
            at_open.add(k)
        elif k not in entries.stored and _deflate_bound(len(v)) < STORED:
            compressed.add(k)
    opened = sum(len(documents[k]) for k in at_open)
    if opened > MAX_OPEN:
        raise SealError("open", None, f"{opened} bytes read at open, more than {MAX_OPEN}")
    most = min(MAX_OPEN, rules.max_format_entry)

    def store_of(ranges_of: list[tuple[str, list[Range]]]) -> tuple[ReferenceStore, dict[str, Any]]:
        mapping: dict[str, bytes | Range | Concat] = {
            k: r[0] if len(r) == 1 else Concat(tuple(r)) for k, r in ranges_of}
        mapping.update(documents)
        try:
            store = ReferenceStore(mapping, sources, read_at_open=sorted(at_open), compressed=sorted(compressed),
                                   page_size=PAGE_SIZE, policy=used, registry=resolver, info=dict(info or {}))
        except (TypeError, ValueError, AttributeError) as e:
            raise SealError("inputs", None, f"the store cannot be made with these arguments: {e}") from e
        return store, mapping

    ranges = [(k, [Range(data=r) if isinstance(r, bytes) else Range(*r) for r in raw]) for k, raw in sealed]
    store, mapping = store_of(ranges)
    _check_archive(store, sources, mapping, most)
    if not checksums:
        return store
    _core_seal(*request, True)
    # the archive again, each url range's checksum at its largest: one past its limits only so
    # fails as its payloads do, whatever the bytes
    widest, mapping = store_of([(k, [_crc(r, sources, LARGEST_CRC) for r in rs]) for k, rs in ranges])
    try:
        _check_archive(widest, sources, mapping, most)
    except SealError as e:
        raise SealError("checksums", e.key, f"with its checksums, {e}") from e
    return store_of([(k, _checksummed(k, values[k].parts, r)) for k, r in ranges])[0]


def _core_seal(*request: Any) -> tuple[list[tuple[str, int]], list[tuple[str, list[tuple[int, int, int] | bytes]]]]:  # noqa: ANN401
    """The core's seal of `request` (`refkv._core.seal`'s arguments), its refusal a SealError."""
    try:
        return _core.seal(*request)
    except _core.SealRefused as e:
        code, key, message = e.args
        raise SealError(code, key, message) from None


def _check_policy(sources: Sequence[Source], policy: Policy | None, registry: object) -> Policy:
    """The store's reader policy: `policy`, which also reads the files of its inputs as the store
    zarrify builds does (`reading_own_files`), once it allows every url source (spec/refkv.md §6.7
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy> rules 1 to 3), as
    `save_vzip` checks them: an absolute URL as it is, a local path's as a `file:` URL."""
    try:
        resolver = registry if isinstance(registry, Resolver) else Resolver(registry)  # type: ignore[arg-type]
    except (TypeError, ValueError, AttributeError) as e:
        raise SealError("inputs", None, f"a registry is a mapping of URL prefixes to object stores: {e}") from e
    base = file_uri(str(Path.cwd())) + "/"
    try:
        rules = reading_own_files(policy, sources)
        for s in sources:
            if s.url is not None:
                url = resolve(base, s.url)
                rules.check(url, registered=resolver.registered(url) is not None)
    except (ResolutionError, ValueError) as e:
        raise SealError("inputs", None, f"the store's reader policy refuses a source: {e}") from e
    return rules


def _first(inputs: Sequence[Parsed], name: str) -> Any:  # noqa: ANN401 (a Parsed's attribute, by name)
    return getattr(inputs[0], name) if inputs else None


def _check_inputs(inputs: Sequence[Parsed], policy: object, registry: object) -> None:
    """That the inputs go together, as `refkv.compose` treats its operands: one reader policy and
    one registry (unless seal is given them), all read with checksums or none."""
    for p in inputs:
        if not isinstance(p, Parsed):
            raise SealError("inputs", None, f"inputs are what refkv.build.read returns, not {type(p).__name__}")
    if len({id(p) for p in inputs}) != len(inputs):
        raise SealError("inputs", None, "an input is given twice")
    if len({p.checksums for p in inputs}) > 1:
        raise SealError("inputs", None, "inputs read with and without checksums go together in no output")
    if any(p.checksums and p.closed for p in inputs):
        raise SealError("inputs", None, "an input read with checksums is closed, so its ranges cannot be checksummed")
    if policy is None and inputs:
        first = inputs[0].policy or Policy()
        if any((p.policy or Policy()) != first for p in inputs):
            raise SealError("inputs", None, "inputs read under different reader policies: give seal the policy")
    if registry is None and len({id(p.registry_given) for p in inputs}) > 1:
        raise SealError("inputs", None, "inputs read with registries that are not one object: give seal the "
                                        "registry")


def _natural(x: object) -> bool:
    return type(x) is int and x >= 0


class _Table:
    """What the core's `seal` numbers: each input source a value uses, as a url (url, size, etag),
    and each data string, with the parts as the core's pieces."""

    def __init__(self, inputs: Sequence[Parsed]) -> None:
        self.inputs = list(inputs)
        self.urls: list[tuple[str, int | None, str | None]] = []
        self.url_at: dict[tuple[int, int], int] = {}  # (input id, source) -> index into urls
        self.read_from: dict[tuple[str, int | None, str | None], str] = {}  # each source's place, as read
        self.data: list[bytes] = []
        self.data_at: dict[bytes, int] = {}
        self.first: int | None = None
        if len(inputs) == 1 and inputs[0].plain_file:
            self.first = self._url(inputs[0], 0, None)

    def _url(self, parsed: Parsed, source: int, key: str | None) -> int:
        at = self.url_at.get((id(parsed), source))
        if at is not None:
            return at
        s = parsed.sources[source]
        ident = (s.url or "", s.size, s.etag)
        place = parsed._read_place(source)  # noqa: SLF001 (where the input reads it)
        if self.read_from.setdefault(ident, place) != place:
            raise SealError("inputs", key, f"{ident[0]} names two different files ({self.read_from[ident]} and "
                                           f"{place}) in two inputs")
        self.urls.append(ident)
        self.url_at[(id(parsed), source)] = len(self.urls) - 1
        return len(self.urls) - 1

    def use(self, key: str, p: Part) -> None:
        """Registers what part `p`, of the value at `key`, reads, refusing a malformed part."""
        if isinstance(p, Copy):
            raise SealError("copy", key, "a Copy left unfilled: fill it with parsed.bytes")
        if isinstance(p, Ref):
            if not any(p.parsed is q for q in self.inputs):
                raise SealError("inputs", key, "a Ref to an input seal was not given")
            if not (type(p.source) is int and 0 <= p.source < len(p.parsed.sources)):
                raise SealError("range", key, f"source {p.source!r} is not among the input's")
            if not (_natural(p.offset) and _natural(p.length)):
                raise SealError("value", key, "a Ref's offset and length are natural numbers")
            if p.offset + p.length > U64:
                raise SealError("range", key, f"bytes {p.offset} to {p.offset + p.length}, past 2^64 - 1")
            self._url(p.parsed, p.source, key)
        elif isinstance(p, (Shared, Data)):
            if type(p.data) is not bytes:
                raise SealError("value", key, f"a {type(p).__name__} part's data is bytes, not {type(p.data).__name__}")
            if isinstance(p, Data):
                if not (_natural(p.offset) and (p.length is None or _natural(p.length))):
                    raise SealError("value", key, "a Data part's offset and length are natural numbers")
                if p.offset > len(p.data) or p.offset + p.size > len(p.data):
                    raise SealError("range", key, "a Data part's bytes lie outside its data")
            if p.data not in self.data_at:
                self.data_at[p.data] = len(self.data)
                self.data.append(p.data)
        elif isinstance(p, Zeros):
            if not _natural(p.length):
                raise SealError("value", key, "Zeros of a natural number of bytes")
            if p.length > MAX_OPEN:  # the core's limit, said here of a length it cannot take
                raise SealError("archive", None, f"a zero data source of {p.length} bytes, more than the {MAX_OPEN} "
                                                 "of a source table")
        elif isinstance(p, Literal):
            if type(p.data) is not bytes:
                raise SealError("value", key, f"a Literal's data is bytes, not {type(p.data).__name__}")

    def piece(self, p: Part) -> bytes | tuple[int, ...]:
        """Part `p`, which `use` took, as the core's piece."""
        if isinstance(p, Literal):
            return p.data
        if isinstance(p, Ref):
            return (0, self.url_at[(id(p.parsed), p.source)], p.offset, p.length)
        if isinstance(p, Shared):
            return (1, self.data_at[p.data], 0, len(p.data))
        if isinstance(p, Data):
            return (1, self.data_at[p.data], p.offset, p.size)
        if isinstance(p, Zeros):
            return (2, p.length)
        raise TypeError(f"no piece of {type(p).__name__}")  # `use` took every other part


def _checksummed(key: str, parts: Sequence[Part], ranges: list[Range]) -> list[Range]:
    """`ranges` with the CRC-32C of each url range, read through the input of its own part."""
    out = []
    for p, r in zip(parts, ranges, strict=True):
        if not isinstance(p, Ref):
            out.append(r)
            continue
        try:
            data = p.parsed._read_ranges([(p.source, p.offset, p.length)])[0] if p.length else b""  # noqa: SLF001
        except (ResolutionError, OSError, ValueError, RuntimeError) as e:
            raise SealError("checksums", key, f"reading a range to checksum it: {e}") from e
        out.append(Range(r.source, r.offset, r.length, crc32c=crc32c(data)))
    return out


def _crc(r: Range, sources: Sequence[Source], crc: int) -> Range:
    """`r` with checksum `crc`, when it is a range of a url source."""
    if r.data is not None or sources[r.source].url is None:
        return r
    return Range(r.source, r.offset, r.length, crc32c=crc)


def _check_archive(store: ReferenceStore, sources: list[Source], mapping: Mapping[str, object], most: int) -> None:
    """The archive's structure, built once as `save_vzip` would build it, and its limits: a
    format entry of at most `most` bytes, as its reader reads them."""
    try:
        sink = cast("BinaryIO", _Counting())
        _, _, sizes = store._write_vzip(sink, sources, mirror_refs=True, page_size=PAGE_SIZE,  # noqa: SLF001
                                        compress=False)
    except ValueError as e:
        raise SealError("archive", None, f"the archive cannot be written: {e}") from e
    for key in (SOURCES_KEY, INDEX_KEY):
        if sizes.get(key, 0) > most:
            raise SealError("archive", None, f"its {key} would be {sizes[key]} bytes, more than the {most} its "
                                             "reader reads")
    keys = sum(len(k.encode()) + PAGE_ENTRY for k in mapping)
    if keys > MAX_OPEN:
        raise SealError("archive", None, f"its keys and their page entries total {keys} bytes, more than the "
                                         f"{MAX_OPEN} a phkv reader reads at open")
