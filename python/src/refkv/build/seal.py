"""`seal`: the check that vouches for an output built with `refkv.build`, and the store it makes
(the design of d-v-b/refkv#278, "Building and writing"). The core numbers the source table and
checks every key, range, payload and read count (`refkv._core.seal`, the function every built-in
output passes through); this module checks what only the host knows (the caller's objects, the
inputs, the reader policy's URL rules, the placements and the archive) and reads the checksums."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, BinaryIO, cast

from refkv import Concat, Range, ReferenceStore, Source, _core
from refkv.errors import ResolutionError
from refkv.policy import MAX_SOURCES, Policy, crc32c
from refkv.reader import Registry, Resolver
from refkv.uri import file_uri, resolve
from refkv.vzip.names import INDEX_KEY, SOURCES_KEY
from refkv.zarrify.common import PAGE_SIZE
from refkv.zarrify.layouts import (
    DOCUMENTS,
    MAX_OPEN,
    PAGE_ENTRY,
    STORED,
    _Counting,
    _deflate_bound,
    chunk_coords,
)

from .parsed import Parsed
from .values import Binding, Check, Copy, Data, Literal, Part, Ref, Shared, Value, Written, Zeros, value

__all__ = ["CODES", "SealError", "seal"]

U64 = (1 << 64) - 1  # the largest offset or length a range holds
# what a SealError's code names
CODES = frozenset({"key", "value", "range", "payload", "checksums", "reads", "copy", "inputs", "open", "archive"})


class SealError(ValueError):
    """Why `seal` refuses an output: `code` is one of `CODES` (`key`; `value`, an entry or part
    of the wrong type; `range`; `payload`; `checksums`; `reads`; `copy`; `inputs`, including
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
    source 0 whether or not a value uses it. A value reads at most 1024 url ranges, counted as
    readers merge them, unless it carries a check of refkv's whose rule lifts that and whose
    binding holds where it is written. Every payload is at most 65519 bytes, and when the inputs
    were read with checksums, with each url range's checksum at its largest (6 bytes), so that
    whether an output is sealed never depends on its bytes."""
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
    max_sources = min(_limit("max_sources", rules.max_sources), MAX_SOURCES)
    max_reads = _limit("max_reads", rules.max_reads)
    checksums = bool(inputs) and inputs[0].checksums
    request = (table.urls, table.data, table.first, [k for k in keys if k not in values],
               [(k, [table.piece(p) for p in val.parts], _read_rule_lifted(val, k, entries))
                for k, val in values.items()], max_sources, max_reads)
    # every check that refuses the output runs before the checksums' measure, which fails the call
    # instead: what a refusal is never depends on the order of the checks
    got, sealed = _core_seal(*request, False)
    sources = [Source(url=table.urls[i][0], size=table.urls[i][1], etag=table.urls[i][2]) if kind == "url"
               else Source(data=table.data[i] if kind == "data" else bytes(i)) for kind, i in got]
    resolver = registry if registry is not None else _first(inputs, "registry_given")
    _check_policy(sources, rules, resolver)
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
    most = min(MAX_OPEN, _limit("max_format_entry", rules.max_format_entry))

    def store_of(ranges_of: list[tuple[str, list[Range]]]) -> ReferenceStore:
        mapping: dict[str, bytes | Range | Concat] = {
            k: r[0] if len(r) == 1 else Concat(tuple(r)) for k, r in ranges_of}
        mapping.update(documents)
        try:
            store = ReferenceStore(mapping, sources, read_at_open=sorted(at_open), compressed=sorted(compressed),
                                   page_size=PAGE_SIZE, policy=used, registry=resolver, info=dict(info or {}))
        except (TypeError, ValueError, AttributeError) as e:
            raise SealError("inputs", None, f"the store cannot be made with these arguments: {e}") from e
        _check_archive(store, sources, mapping, most)
        return store

    ranges = [(k, [Range(data=r) if isinstance(r, bytes) else Range(*r) for r in raw]) for k, raw in sealed]
    store = store_of(ranges)
    if not checksums:
        return store
    _core_seal(*request, True)
    return store_of([(k, _checksummed(k, values[k].parts, r)) for k, r in ranges])


def _core_seal(*request: Any) -> tuple[list[tuple[str, int]], list[tuple[str, list[tuple[int, int, int] | bytes]]]]:  # noqa: ANN401
    """The core's seal of `request` (`refkv._core.seal`'s arguments), its refusal a SealError."""
    try:
        return _core.seal(*request)
    except _core.SealRefused as e:
        code, key, message = e.args
        raise SealError(code, key, message) from None


def _check_policy(sources: Sequence[Source], rules: Policy, registry: object) -> None:
    """That the store's reader policy allows every url source (spec/refkv.md §6.7
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy> rules 1 to 3), as
    `save_vzip` checks them: an absolute URL as it is, a local path's as a `file:` URL."""
    try:
        resolver = registry if isinstance(registry, Resolver) else Resolver(registry)  # type: ignore[arg-type]
    except (TypeError, ValueError, AttributeError) as e:
        raise SealError("inputs", None, f"a registry is a mapping of URL prefixes to object stores: {e}") from e
    base = file_uri(str(Path.cwd())) + "/"
    for s in sources:
        if s.url is None:
            continue
        url = resolve(base, s.url)
        try:
            rules.check(base, url, registered=resolver.registered(url) is not None)
        except (ResolutionError, ValueError) as e:
            raise SealError("inputs", None, f"the store's reader policy refuses a source: {e}") from e


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


def _limit(name: str, x: object) -> int:
    """A reader policy's limit as the core takes it: an int, none below 0 or past 2^63 - 1, which
    are refkv's limits or fewer, as the core counts them."""
    if type(x) is not int:
        raise SealError("inputs", None, f"the policy's {name} is an int, not {type(x).__name__}")
    return max(0, min(x, (1 << 63) - 1))


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


def _order(codec: object, n: int) -> list[int] | None:
    """The order of a `transpose` codec over `n` axes, or None for a malformed one."""
    conf = codec.get("configuration") if isinstance(codec, dict) else None
    order = conf.get("order") if isinstance(conf, dict) else None
    if not isinstance(order, list) or len(order) != n or any(type(x) is not int for x in order) \
            or sorted(order) != list(range(n)):
        return None
    return order


def _named(codec: object, name: str) -> bool:
    return isinstance(codec, dict) and codec.get("name") == name


def _lead(codecs: object, n: int) -> tuple[list[int], list[Any], bool] | None:
    """The leading transpose of an array's codecs as an order (the identity when there is none),
    the codecs without it, and whether the array is sharded; None for codecs that cannot be read.
    The transpose sits first outside any `sharding_indexed` codec, or, as `copy` writes it for a
    sharded array, first in its inner codecs with `transpose([*order, n])` first in its index
    codecs, the inner chunk shape then put back in stored order."""
    if not isinstance(codecs, list) or not all(isinstance(c, dict) for c in codecs):
        return None
    identity = list(range(n))
    if codecs and _named(codecs[0], "transpose"):
        order = _order(codecs[0], n)
        if order is None:
            return None
        rest = codecs[1:]
        return order, rest, bool(rest) and _named(rest[0], "sharding_indexed")
    if codecs and _named(codecs[0], "sharding_indexed"):
        conf = codecs[0].get("configuration")
        if not isinstance(conf, dict):
            return None
        conf = dict(conf)
        inner, index = conf.get("codecs"), conf.get("index_codecs")
        if not isinstance(inner, list) or not isinstance(index, list):
            return None
        inner_first = bool(inner) and _named(inner[0], "transpose")
        index_first = bool(index) and _named(index[0], "transpose")
        if not inner_first and not index_first:
            return identity, codecs, True
        if not (inner_first and index_first):
            return None  # an inner transpose without its index's, or the other way: not copy's form
        order, index_order = _order(inner[0], n), _order(index[0], n + 1)
        if order is None or index_order != [*order, n]:
            return None
        chunk = conf.get("chunk_shape")
        if not isinstance(chunk, list) or len(chunk) != n:
            return None
        conf["codecs"], conf["index_codecs"] = inner[1:], index[1:]
        conf["chunk_shape"] = [chunk[order[i]] for i in range(n)]
        return order, [{**codecs[0], "configuration": conf}, *codecs[1:]], True
    return identity, codecs, False


def _stored(x: Sequence[Any], order: Sequence[int]) -> list[Any]:
    """Presented axes' `x` in stored order: stored axis `i` is presented axis `order[i]`."""
    return [x[order[i]] for i in range(len(order))]


def _grid(doc: Mapping[str, Any]) -> list[Any] | None:
    grid = doc.get("chunk_grid")
    conf = grid.get("configuration") if isinstance(grid, dict) else None
    shape = conf.get("chunk_shape", conf.get("chunk_shapes")) if isinstance(conf, dict) else None
    return shape if isinstance(shape, list) else None


def _canonical(x: object) -> str:
    return json.dumps(x, sort_keys=True, separators=(",", ":"))


def _read_rule_lifted(val: Value, key: str, entries: Mapping[str, Any]) -> bool:
    """Whether checked value `val`, written at `key`, is exempt from the read rule: its check is
    refkv's for its very parts (`Check.issued_for`), its rule lifts the read rule there, and its binding holds. Rule
    `"builtin"` lifts it inside a sharded array alone, whose shards a reader reads in parts
    (spec/zarrify.md §2.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#21-virtual-shards>);
    any other value of a built-in output is read whole, and meets the rule as a caller's does."""
    check = val.check
    if not isinstance(check, Check) or not check.issued_for(val.parts) or check.rule != "builtin":
        return False
    return _binding_holds(val, key, entries) is True


def _binding_holds(val: Value, key: str, entries: Mapping[str, Any]) -> bool | None:
    """Whether checked value `val`, written at `key`, sits in a sharded array of the output that its
    binding names (True), in an unsharded one it names (False), or neither (None): the array
    document above `key` whose shape, data type, chunk grid, codecs and fill value are the
    binding's, leading transposes undone, with `key` its chunk key there."""
    if val.check is None:
        return None
    b: Binding = val.check.binding
    segments = key.split("/")
    for cut in range(len(segments) - 1, -1, -1):
        prefix = "/".join(segments[:cut])
        doc_key = f"{prefix}/zarr.json" if prefix else "zarr.json"
        if doc_key not in entries:
            continue
        raw = entries[doc_key]
        if not isinstance(raw, bytes):
            return None  # the nearest document is no document a reader parses: it holds no binding
        try:
            doc = json.loads(raw)
            if not isinstance(doc, dict) or doc.get("node_type") != "array":
                return None
            coords = chunk_coords(key[len(prefix) + 1:] if prefix else key, doc)
            return _same_array(doc, coords, b)
        except (ValueError, KeyError, TypeError, AttributeError, IndexError, RecursionError):
            return None  # a document that cannot be read holds no binding
    return None


def _same_array(doc: Mapping[str, Any], coords: Sequence[int], b: Binding) -> bool | None:
    """True or False as for `_binding_holds`, when `doc` is the binding's array at `coords`; else None."""
    n = len(b.shape)
    shape = doc.get("shape")
    if not isinstance(shape, list) or len(shape) != n or len(coords) != n:
        return None
    d, bb = _lead(doc.get("codecs"), n), _lead(json.loads(b.codecs), n)
    d_grid, b_grid = _grid(doc), _grid({"chunk_grid": json.loads(b.chunk_grid)})
    if d is None or bb is None or d_grid is None or b_grid is None or len(d_grid) != n or len(b_grid) != n:
        return None
    (d_order, d_codecs, sharded), (b_order, b_codecs, _) = d, bb
    same = (_canonical(doc.get("data_type")) == b.data_type
            and _canonical(doc.get("fill_value")) == b.fill_value
            and _canonical(d_codecs) == _canonical(b_codecs)
            and (doc.get("chunk_grid") or {}).get("name") == json.loads(b.chunk_grid).get("name")
            and _stored(shape, d_order) == _stored(list(b.shape), b_order)
            and _stored(d_grid, d_order) == _stored(b_grid, b_order)
            and _stored(list(coords), d_order) == _stored(list(b.key), b_order))
    return sharded if same else None


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
