"""`seal`: the check that vouches for an output built with `refkv.build`, and the store it makes
(design/proposals/profile-plugins.md, "Building and writing"). It numbers the source table, checks
every key and reference against the inputs `read` opened, and places the entries. In this first
step it is Python; the proposal moves it into the core."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, BinaryIO, cast

from refkv import Concat, Range, ReferenceStore, Source
from refkv.policy import Policy, crc32c
from refkv.reader import Registry
from refkv.vzip.names import INDEX_KEY, SOURCES_KEY
from refkv.zarrify.common import PAGE_SIZE
from refkv.zarrify.layouts import (
    DOCUMENTS,
    MAX_OFFSET,
    MAX_OPEN,
    MAX_PAYLOAD,
    MAX_READS,
    MERGE_GAP,
    PAGE_ENTRY,
    STORED,
    _Counting,
    _deflate_bound,
    _key_problem,
    chunk_coords,
)

from .parsed import Parsed
from .values import MAX_SOURCES, Binding, Copy, Data, Literal, Part, Ref, Shared, Value, Written, Zeros, value

__all__ = ["SealError", "seal"]


# the reader policy's rule 5, which bounds the reads of one value
READ_RULE = "spec/refkv.md §6.7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>"


class SealError(ValueError):
    """Why `seal` refuses an output: `code` is one of `key`, `range`, `payload`, `checksums`,
    `reads`, `copy`, `inputs`, `open` and `archive`, and `key` the entry's key, or None for the
    output as a whole."""

    def __init__(self, code: str, key: str | None, message: str) -> None:
        super().__init__(f"{key}: {message}" if key else message)
        self.code = code
        self.key = key


def seal(written: Mapping[str, Any], inputs: Sequence[Parsed], *, policy: Policy | None = None,
         registry: Registry | None = None, info: Mapping[str, object] | None = None) -> ReferenceStore:
    """The output `written` (a `Written`, or a mapping of keys to bytes or values) as a
    `refkv.ReferenceStore`, its references read from `inputs`. Raises `SealError` for an output it
    does not vouch for.

    The source table is numbered by first use over the references in key order (UTF-8), the url
    sources first, then the data sources (each distinct `Shared` and `Data` byte string, and one
    zero data source, as long as the most zeros a value holds); one input that is a plain file is
    source 0 whether or not a value uses it. A value reads at most 1024 url ranges, counted as
    readers merge them, unless it is checked and its binding holds where it is written. Every
    payload is at most 65519 bytes, with its checksums when the inputs were read with them."""
    inputs = list(inputs)
    _check_inputs(inputs, policy, registry)
    entries = written if isinstance(written, Written) else Written(written)
    keys = sorted(entries, key=lambda k: k.encode("utf-8", "surrogatepass"))
    for k in keys:
        why = _key_problem(k)
        if why:
            raise SealError("key", k if isinstance(k, str) else repr(k), why)

    table = _Table(inputs)
    values: dict[str, Value] = {}
    for k in keys:
        v = entries[k]
        if isinstance(v, bytes):
            continue
        val = value(v)
        for p in val.parts:
            if isinstance(p, Copy):
                raise SealError("copy", k, "a Copy left unfilled: fill it with parsed.bytes")
            table.use(k, p)
        values[k] = val
    sources = table.sources()
    if len(sources) > MAX_SOURCES:
        raise SealError("archive", None, f"{len(sources)} sources, more than an output's {MAX_SOURCES}")

    checksums = bool(inputs) and inputs[0].checksums
    mapping: dict[str, bytes | Range | Concat] = {}
    for k in keys:
        if k not in values:
            continue
        val = values[k]
        ranges = [table.range(k, p) for p in val.parts]
        total = sum(r.size for r in ranges)
        if total > MAX_OFFSET:
            raise SealError("range", k, "its ranges total more than 2^64 - 1 bytes")
        held = _binding_holds(val, k, entries)
        if not held:
            reads = _reads(ranges, sources)
            if reads > MAX_READS:
                raise SealError("reads", k, f"it reads {reads} ranges, more than the {MAX_READS} a reader makes for "
                                            f"one value by default ({READ_RULE})")
        bare = _encoded(ranges)
        if len(bare) > MAX_PAYLOAD:
            raise SealError("payload", k, f"a payload of {len(bare)} bytes, more than {MAX_PAYLOAD}")
        if checksums:
            ranges = table.checksummed(ranges)
            n = len(_encoded(ranges))
            if n > MAX_PAYLOAD:
                raise SealError("checksums", k, f"a payload of {n} bytes with its checksums, more than {MAX_PAYLOAD}")
        mapping[k] = ranges[0] if len(ranges) == 1 else Concat(tuple(ranges))

    at_open: set[str] = set()
    compressed: set[str] = set()
    for k in keys:
        v = entries[k]
        if not isinstance(v, bytes):
            continue
        mapping[k] = v
        opened = entries.at_open.get(k, k.rpartition("/")[2] in DOCUMENTS)
        if opened:
            at_open.add(k)
        elif k not in entries.stored and _deflate_bound(len(v)) < STORED:
            compressed.add(k)
    for k in list(entries.at_open) + list(entries.stored):
        if not isinstance(entries.get(k), bytes):
            raise SealError("open", k, "a placement names a key that is not a bytes entry of the output")
    opened_bytes = sum(len(mapping[k]) for k in at_open)  # type: ignore[arg-type]
    if opened_bytes > MAX_OPEN:
        raise SealError("open", None, f"{opened_bytes} bytes read at open, more than {MAX_OPEN}")

    store = ReferenceStore(mapping, sources, read_at_open=sorted(at_open), compressed=sorted(compressed),
                           page_size=PAGE_SIZE, policy=policy if policy is not None else _first(inputs, "policy"),
                           registry=registry if registry is not None else _first(inputs, "registry_given"),
                           info=dict(info or {}))
    _check_archive(store, sources, mapping)
    return store


def _first(inputs: Sequence[Parsed], name: str) -> Any:  # noqa: ANN401 (a Parsed's attribute, by name)
    return getattr(inputs[0], name) if inputs else None


def _check_inputs(inputs: Sequence[Parsed], policy: object, registry: object) -> None:
    for p in inputs:
        if not isinstance(p, Parsed):
            raise SealError("inputs", None, f"inputs are what refkv.build.read returns, not {type(p).__name__}")
    if len({id(p) for p in inputs}) != len(inputs):
        raise SealError("inputs", None, "an input is given twice")
    if len({p.checksums for p in inputs}) > 1:
        raise SealError("inputs", None, "inputs read with and without checksums go together in no output")
    # as refkv.compose treats its operands: one policy and one registry, unless seal is given them
    if policy is None and len({repr(p.policy) for p in inputs}) > 1:
        raise SealError("inputs", None, "inputs read under different reader policies: give seal the policy")
    if registry is None and len({id(p.registry_given) for p in inputs}) > 1:
        raise SealError("inputs", None, "inputs read with different object stores: give seal the registry")


class _Table:
    """The output's source table, numbered by first use: url sources, then data sources."""

    def __init__(self, inputs: Sequence[Parsed]) -> None:
        self.inputs = list(inputs)
        self.urls: dict[tuple[str, int | None, str | None], int] = {}
        self.where: dict[tuple[int, int], tuple[str, int | None, str | None]] = {}  # (input id, source) -> url key
        self.data: dict[bytes, int] = {}
        self.zeros = 0
        self.zero_used = False
        self.order: list[tuple[str, object]] = []  # ("data", bytes) or ("zero", None), by first use
        if len(inputs) == 1 and inputs[0].plain_file:
            self._url(inputs[0], 0, None)

    def _url(self, parsed: Parsed, source: int, key: str | None) -> tuple[str, int | None, str | None]:
        s = parsed.sources[source]
        ident = (s.url or "", s.size, s.etag)
        for (url, size, etag) in self.urls:
            if url == ident[0] and (size, etag) != (ident[1], ident[2]):
                raise SealError("inputs", key, f"{url} with different pins in two inputs: the file changed "
                                               "between two reads")
        self.urls.setdefault(ident, len(self.urls))
        self.where[(id(parsed), source)] = ident
        return ident

    def use(self, key: str, p: Part) -> None:
        if isinstance(p, Ref):
            if not any(p.parsed is q for q in self.inputs):
                raise SealError("inputs", key, "a Ref to an input seal was not given")
            if not (type(p.source) is int and 0 <= p.source < len(p.parsed.sources)):
                raise SealError("range", key, f"source {p.source!r} is not among the input's")
            self._url(p.parsed, p.source, key)
        elif isinstance(p, (Shared, Data)):
            if p.data not in self.data:
                self.data[p.data] = -1
                self.order.append(("data", p.data))
        elif isinstance(p, Zeros):
            if not self.zero_used:
                self.zero_used = True
                self.order.append(("zero", None))
            self.zeros = max(self.zeros, p.length)

    def sources(self) -> list[Source]:
        out = [Source(url=url, size=size, etag=etag) for (url, size, etag) in self.urls]
        self.zero_at = -1
        for kind, data in self.order:
            if kind == "data":
                self.data[data] = len(out)  # type: ignore[index]
                out.append(Source(data=data))  # type: ignore[arg-type]
            else:
                self.zero_at = len(out)
                out.append(Source(data=bytes(self.zeros)))
        self._sources = out
        return out

    def range(self, key: str, p: Part) -> Range:
        if isinstance(p, Literal):
            return Range(data=p.data)
        if isinstance(p, Ref):
            ident = self.where[(id(p.parsed), p.source)]
            size = ident[1]
            if not (type(p.offset) is int and type(p.length) is int and p.offset >= 0 and p.length >= 0):
                raise SealError("range", key, "a Ref's offset and length are natural numbers")
            if size is None:
                raise SealError("range", key, f"source {p.source}'s size is not known, so a range of it cannot be "
                                              "checked")
            if p.offset + p.length > size:
                raise SealError("range", key, f"bytes {p.offset} to {p.offset + p.length} of {ident[0]}, past its "
                                              f"{size}")
            return Range(self.urls[ident], p.offset, p.length)
        if isinstance(p, Shared):
            return Range(self.data[p.data], 0, len(p.data))
        if isinstance(p, Data):
            if p.offset < 0 or p.offset + p.size > len(p.data) or p.size < 0:
                raise SealError("range", key, "a Data part's bytes lie outside its data")
            return Range(self.data[p.data], p.offset, p.size)
        if not isinstance(p, Zeros):
            raise TypeError(f"a part of {type(p).__name__}")
        return Range(self.zero_at, 0, p.length)

    def checksummed(self, ranges: list[Range]) -> list[Range]:
        out = []
        for r in ranges:
            if r.data is not None or self._sources[r.source].url is None or not r.length:
                out.append(r)
                continue
            ident = next(i for i, n in self.urls.items() if n == r.source)
            parsed, source = next((self._input(k), k[1]) for k, v in self.where.items() if v == ident)
            data = parsed.read_ranges([(source, r.offset, r.length)])[0]
            out.append(Range(r.source, r.offset, r.length, crc32c=crc32c(data)))
        return out

    def _input(self, k: tuple[int, int]) -> Parsed:
        return next(p for p in self.inputs if id(p) == k[0])


def _encoded(ranges: list[Range]) -> bytes:
    """A value's payload, as written (spec/refkv.md §4.4
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#44-reference-payloads>)."""
    return ranges[0].encode() if len(ranges) == 1 else Concat(tuple(ranges)).encode()


def _reads(ranges: list[Range], sources: list[Source]) -> int:
    """The reads of url sources ranges make, ranges of one source at most `MERGE_GAP` apart
    counted once, as refkv's readers merge them."""
    spans: dict[int, list[tuple[int, int]]] = {}
    for r in ranges:
        if r.data is None and sources[r.source].url is not None and r.length:
            spans.setdefault(r.source, []).append((r.offset, r.offset + r.length))
    n = 0
    for got in spans.values():
        got.sort()
        end = -MERGE_GAP - 1
        for a, b in got:
            if a > end + MERGE_GAP:
                n += 1
            end = max(end, b)
    return n


def _lead(codecs: list[dict[str, Any]], n: int) -> tuple[list[int], list[dict[str, Any]]]:
    """The leading transpose of an array's codecs, as an order (the identity when there is none), and
    the codecs without it: first outside any `sharding_indexed` codec, or first in its inner codecs
    (as `copy` writes it for a sharded array), with the inner chunk shape put back in stored order
    and the index codecs' transpose dropped."""
    identity = list(range(n))
    if codecs and codecs[0].get("name") == "transpose":
        return list(codecs[0]["configuration"]["order"]), codecs[1:]
    if codecs and codecs[0].get("name") == "sharding_indexed":
        conf = dict(codecs[0].get("configuration", {}))
        inner = list(conf.get("codecs", []))
        if inner and inner[0].get("name") == "transpose":
            order = list(inner[0]["configuration"]["order"])
            conf["codecs"] = inner[1:]
            if isinstance(conf.get("chunk_shape"), list):
                conf["chunk_shape"] = [conf["chunk_shape"][order[i]] for i in range(n)]
            index = list(conf.get("index_codecs", []))
            if index and index[0].get("name") == "transpose":
                conf["index_codecs"] = index[1:]
            return order, [{**codecs[0], "configuration": conf}, *codecs[1:]]
    return identity, codecs


def _stored(x: Sequence[Any], order: Sequence[int]) -> list[Any]:
    """Presented axes' `x` in stored order: stored axis `i` is presented axis `order[i]`."""
    return [x[order[i]] for i in range(len(order))]


def _grid(doc: Mapping[str, Any]) -> object:
    grid = doc.get("chunk_grid", {})
    conf = grid.get("configuration", {})
    return conf.get("chunk_shape", conf.get("chunk_shapes"))


def _binding_holds(val: Value, key: str, entries: Mapping[str, Any]) -> bool:
    """Whether checked value `val`, written at `key`, sits where it was built: in an array document
    of the output whose shape, data type, chunk grid, codecs and fill value are its binding's,
    leading transposes undone, at its chunk key there."""
    if val.check is None:
        return False
    b: Binding = val.check.binding
    parts = key.split("/")
    for cut in range(len(parts) - 1, -1, -1):
        prefix = "/".join(parts[:cut])
        doc_key = f"{prefix}/zarr.json" if prefix else "zarr.json"
        raw = entries.get(doc_key)
        if not isinstance(raw, bytes):
            continue
        try:
            doc = json.loads(raw)
        except ValueError:
            return False
        if not isinstance(doc, dict) or doc.get("node_type") != "array":
            return False
        rel = key[len(prefix) + 1:] if prefix else key
        try:
            coords = chunk_coords(rel, doc)
        except (KeyError, ValueError, TypeError):
            return False
        return _same_array(doc, coords, b)
    return False


def _same_array(doc: Mapping[str, Any], coords: Sequence[int], b: Binding) -> bool:
    n = len(b.shape)
    if len(doc.get("shape", [])) != n or len(coords) != n:
        return False
    built = {"shape": list(b.shape), "data_type": json.loads(b.data_type), "chunk_grid": json.loads(b.chunk_grid),
             "codecs": json.loads(b.codecs), "fill_value": json.loads(b.fill_value)}
    d_order, d_codecs = _lead(list(doc.get("codecs", [])), n)
    b_order, b_codecs = _lead(built["codecs"], n)
    if sorted(d_order) != list(range(n)) or sorted(b_order) != list(range(n)):
        return False
    d_grid, b_grid = _grid(doc), _grid(built)
    if not (isinstance(d_grid, list) and isinstance(b_grid, list) and len(d_grid) == n == len(b_grid)):
        return False
    return (doc.get("data_type") == built["data_type"]
            and json.dumps(doc.get("fill_value")) == json.dumps(built["fill_value"])
            and json.dumps(d_codecs, sort_keys=True) == json.dumps(b_codecs, sort_keys=True)
            and doc.get("chunk_grid", {}).get("name") == built["chunk_grid"].get("name")
            and _stored(doc["shape"], d_order) == _stored(built["shape"], b_order)
            and _stored(d_grid, d_order) == _stored(b_grid, b_order)
            and _stored(list(coords), d_order) == _stored(list(b.key), b_order))


def _check_archive(store: ReferenceStore, sources: list[Source], mapping: Mapping[str, object]) -> None:
    """The archive's structure, built once as `save_vzip` would build it, and its limits."""
    try:
        sink = cast("BinaryIO", _Counting())
        _, _, sizes = store._write_vzip(sink, sources, mirror_refs=True, page_size=PAGE_SIZE,  # noqa: SLF001
                                        compress=False)
    except ValueError as e:
        raise SealError("archive", None, f"the archive cannot be written: {e}") from e
    for key in (SOURCES_KEY, INDEX_KEY):
        if sizes.get(key, 0) > MAX_OPEN:
            raise SealError("archive", None, f"its {key} would be {sizes[key]} bytes, more than the {MAX_OPEN} a "
                                             "reader reads by default")
    keys = sum(len(k.encode()) + PAGE_ENTRY for k in mapping)
    if keys > MAX_OPEN:
        raise SealError("archive", None, f"its keys and their page entries total {keys} bytes, more than the "
                                         f"{MAX_OPEN} a phkv reader reads at open")

