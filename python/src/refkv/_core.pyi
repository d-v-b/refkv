# The stub of `refkv._core`, the Rust core's Python bindings (rust/refkv-python/src/lib.rs):
# the parsers as step/feed/finish objects, the runs, and the IR's columns, tables and
# per-element reads. Its docstrings are the `///` docs of lib.rs, which `help()` shows from the
# compiled module, and the ValueErrors each raises, which lib.rs leaves to its code; keep the two
# the same.
#
# The parsers and the runs are used up by `finish()` (and a run by `output()`): any call after
# it raises ValueError("finished").

from collections.abc import Callable, Sequence
from typing import Literal

# A decoded IR value (`Ir.value`).
type Value = int | float | str | bytes | list[Value] | dict[str, Value]

# A part of a file output's reference: (offset, length) of source 0, (source, offset, length)
# of a data source, or literal bytes.
type FilePart = tuple[int, int] | tuple[int, int, int] | bytes

# A part of a store output's reference: ("object", key, offset, length) of a store's object,
# (source, offset, length) of a data source, or literal bytes.
type StorePart = tuple[Literal["object"], str, int, int] | tuple[int, int, int] | bytes

# An output (`out_py`): (url, entries [(key, bytes)], refs [(key, parts)], data sources, lazy
# prefixes, summary JSON).
type Out[P] = tuple[str, list[tuple[str, bytes]], list[tuple[str, list[P]]], list[bytes], list[str], str]

# `get(key)`: an archive's entry, or None.
type Get = Callable[[str], bytes | None]

# `read(offset, length)`: bytes of the source.
type Read = Callable[[int, int], bytes]

class Rejected(Exception):  # noqa: N818 (the core's name)
    """A parser, a run or a projection rejected its input."""

class Violation(Exception):  # noqa: N818 (the core's name)
    """An IR broke an invariant (conventions §8.1
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#81-elements>)."""

class Ir:
    """An IR: its columns, tables and per-element reads. A per-element read raises ValueError
    for no element `i`."""

    def __len__(self) -> int: ...
    @property
    def size(self) -> int: ...
    def columns(self) -> dict[str, bytes]:
        """The dense columns as little-endian bytes: kind u1, parent u4, name u4,
        nidx u8, type u4, space u4, start u8, len u8."""
    def names(self) -> list[str]: ...
    def types(self) -> list[str]: ...
    def forms(self) -> list[str]: ...
    def runs(self) -> list[tuple[int, int, int]]:
        """(run root, count, stride)"""
    def form_ids(self) -> list[tuple[int, int]]:
        """(element, form id)"""
    def targets(self) -> list[tuple[int, int]]:
        """(element, target)"""
    def was(self) -> list[tuple[int, int]]:
        """(element, kind it had): aliases finish() made"""
    def value_index(self) -> list[tuple[int, int, int]]:
        """(element, offset, length) into `value_bytes()`"""
    def value_bytes(self) -> bytes: ...
    def kind(self, i: int) -> int: ...
    def parent(self, i: int) -> int: ...
    def name(self, i: int) -> str: ...
    def nidx(self, i: int) -> int | None: ...
    def path(self, i: int) -> str: ...
    def type(self, i: int) -> str: ...
    def space(self, i: int) -> int: ...
    def extent(self, i: int) -> tuple[int, int]: ...
    def run(self, i: int) -> tuple[int, int] | None: ...
    def form(self, i: int) -> str | None: ...
    def target(self, i: int) -> int: ...
    def was_kind(self, i: int) -> int | None: ...
    def raw(self, i: int) -> bytes | None: ...
    def value(self, i: int) -> Value | None:
        """The decoded value: decode(type, its bytes), or None when the parser kept no bytes.
        Raises ValueError for no element `i`, or bytes its type cannot decode."""
    def children(self, i: int) -> list[int]: ...
    def child(self, i: int, name: str) -> int | None: ...
    def check(self) -> None:
        """Coverage, injectivity, the root and the names, and the other invariants
        (conventions §8.1
        <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#81-elements>); raises
        Violation."""
    def leaves(self) -> list[tuple[int, int]]:
        """The leaves in source order, as ranges (adjacent ones merged)."""
    def memory(self) -> int: ...
    def members(self, i: int) -> tuple[list[int], list[int], list[int], list[int]]:
        """The data members of element `i`, runs expanded: four lists (name index,
        start, length, form id)."""
    def shared(self) -> list[bytes]:
        """The shared data sources recipes name."""
    def object_bases(self) -> list[tuple[str, int, int]]:
        """A store IR's objects in id order: (key, virtual offset of its byte 0, size) (conventions
        §8.9 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#89-stores>), for reading
        an object described in place at virtual offsets; empty for a file IR."""
    def digest(self) -> int:
        """A digest of every row (for comparing builds)."""
    def runs_count(self) -> int: ...
    def budget(self) -> dict[str, tuple[int, int]]: ...
    @staticmethod
    def from_archive(get: Get, read: Read) -> Ir:
        """The IR a mirror's table describes (`refkv_source/ir/...`, its entries given by
        `get(key) -> bytes | None`), its column runs expanded; `read(offset, length)`
        gives bytes of the source, for the columns read from arrays in it. Raises
        Violation for a bad table."""
    @staticmethod
    def from_table(
        size: int,
        kind: bytes | Sequence[int],
        parent: Sequence[int],
        start: Sequence[int],
        len: Sequence[int],  # noqa: A002 (the core's name)
        space: Sequence[int],
        runs: Sequence[tuple[int, int, int]],
        targets: Sequence[tuple[int, int]],
        name: Sequence[str] | None = None,
        nidx: Sequence[int | None] | None = None,
    ) -> Ir:
        """An IR from a stored table (the mirror's), for checking and rebuilding: each
        element's name (default `""`) and name index (default none) are `name` and
        `nidx` when given. Raises ValueError for columns of different lengths."""

class Nd2Parser:
    """The ND2 parser: `step()` gives the next batch of ranges (None when done),
    `feed(offset, bytes)` gives one range's bytes, `finish()` the IR and the facts (JSON)."""

    def __init__(self, size: int) -> None: ...
    def step(self) -> list[tuple[int, int]] | None: ...
    def feed(self, offset: int, data: bytes) -> None: ...
    def finish(self) -> tuple[Ir, str]: ...
    def memory(self) -> int: ...

class TiffParser:
    """The TIFF parser: as `Nd2Parser` (`finish()` gives the IR and the facts as JSON)."""

    def __init__(self, size: int) -> None: ...
    def step(self) -> list[tuple[int, int]] | None: ...
    def feed(self, offset: int, data: bytes) -> None: ...
    def finish(self) -> tuple[Ir, str]: ...

class CziParser:
    """The CZI parser: as `Nd2Parser`."""

    def __init__(self, size: int) -> None: ...
    def step(self) -> list[tuple[int, int]] | None: ...
    def feed(self, offset: int, data: bytes) -> None: ...
    def finish(self) -> tuple[Ir, str]: ...

class Run:
    """A run: a format's parser and the read planner (sans-IO). The host loops
    `poll()` (the requests to make now, each a list of (start, end) spans: one
    span a plain range request, several a multi-range one) and `complete(k,
    parts, seconds)` (each span's bytes, or None for a multi-range request the
    server did not answer with its ranges), until `poll()` gives None; then
    `finish()` gives the IR, the facts (JSON) and the planner's counts (JSON).

    The constructor raises ValueError for an unknown `format`; `poll` and `complete` raise
    Rejected when the parser rejects the input."""

    def __init__(
        self,
        format: str,  # noqa: A002 (the core's name)
        size: int,
        remote: bool,
        concurrency: int,
        amplification: float | None = None,
        byte_cost: float | None = None,
        whole_below: int | None = None,
    ) -> None: ...
    def poll(self) -> list[list[tuple[int, int]]] | None: ...
    def complete(self, k: int, parts: Sequence[bytes] | None, seconds: float) -> None: ...
    def observe(self, n: int, seconds: float) -> None:
        """A request the host made itself (n bytes in `seconds`), for the cost model."""
    def seed(self, offset: int, data: bytes) -> None:
        """Bytes the host holds already (from offset): ranges in them need no request."""
    @property
    def multirange(self) -> bool | None: ...
    @multirange.setter
    def multirange(self, v: bool | None) -> None: ...
    @property
    def format(self) -> str:
        """The run's format: "tiff" becomes "ndpi" once its parser finds the file is NDPI."""
    def stats(self) -> str:
        """The planner's counts and model (JSON)."""
    def output(self, url: str, mirror: bool = True, layouts: list[str] | None = None,
               checksums: bool = False) -> tuple[Out[FilePart], Ir]:
        """Finishes, projects and (with `mirror`) mirrors: (the output in `out_py`'s form,
        the IR). `layouts`, when given, names the layouts the projection writes beside the
        image (a format's dataset layout, "dataset"; conventions §9
        <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#9-dataset-layouts>); by
        default, every one. `checksums`: the host records the ranges' checksums, so that a
        sharded array's ranges are not joined across inner chunks (spec/zarrify.md §2.1
        <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#21-virtual-shards>)."""
    def finish(self) -> tuple[Ir, str, str]: ...

class StoreRun:
    """A store run (src/store.rs): `StoreRun(format, listing, concurrency)` over the listing
    [(key, size)]; the host loops `poll()` (the requests to make now, concurrently: (object
    index, key, spans [(start, end)])) and `complete(k, parts)` (each span's bytes) until
    `poll()` gives None; then `output(url, mirror)` or `finish()`. `ignore(keys, folders)`,
    before the first poll, gives the keys the listing ignores.

    The constructor raises ValueError for an unknown store `format`; it, `poll` and
    `complete` raise Rejected when the store is rejected."""

    def __init__(self, format: str, listing: Sequence[tuple[str, int]], concurrency: int) -> None: ...  # noqa: A002 (the core's name)
    def poll(self) -> list[tuple[int, str, list[tuple[int, int]]]] | None: ...
    def complete(self, k: int, parts: Sequence[bytes]) -> None: ...
    def stats(self) -> str:
        """The run's counts (JSON)."""
    def probes(self) -> list[str]:
        """The keys of a pending batch of probes (`StoreRun::probes`): `poll()` gave an empty
        list, and the host answers each key, in order, with `found(size)` (None: it does not
        exist) before polling again."""
    def found(self, size: int | None) -> None:
        """The answer to the next probe of the batch: the key's size, or None when it does not exist."""
    def ignore(self, keys: Sequence[str], folders: Sequence[str] = ...) -> None:
        """The keys the listing ignores, before the first poll (`StoreRun::ignore`): `keys`, the
        relative keys the store reader ignored and recorded (`Store.ignored`), which the
        projection records as its convention says, and `folders`, its empty `d/` keys
        (`Store.folders`), which a SAFE directory's parser reads as directories."""
    def profile(self) -> str:
        """The profile the run's store is decided to be so far (`StoreRun::profile`)."""
    def output(self, url: str, mirror: bool = True) -> tuple[Out[StorePart], Ir]: ...
    def finish(self) -> tuple[Ir, str, str]: ...

def rebuild_store(get: Get) -> list[tuple[str, int, bytes | None]]:
    """The rebuild of a store's mirror (conventions §8.9
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#89-stores>) from an archive's
    entries (`get(key)`: bytes or None): every object as (key, size, bytes), the bytes of a held
    object rebuilt from its leaves, None for a referenced or empty one; or Rejected."""
def plan_spans(
    ranges: Sequence[tuple[int, int]], threshold: int, cap: float, concurrency: int
) -> list[tuple[int, int]]:
    """The planner's merge of ranges (offset, length) into requests (start, end), for a
    gap threshold, an amplification cap and a concurrency (the policy's core, for tests)."""
def crc32c(data: bytes) -> int:
    """CRC-32C (Castagnoli, RFC 3720 §B.4) of `data`: a range's checksum (spec §4.2
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#42-range>)."""
def sniff(head: bytes) -> str | None:
    """The format a file's first bytes (up to 552 of them) give (`Format::sniff`), if any: its
    profile's name."""
def registry() -> str:
    """The profile registry (spec/zarrify/profiles/, as the core holds it): its entries, by
    name, as a JSON array."""
def fits_tiles_decode(chunk: bytes, config: str, chunk_shape: Sequence[int]) -> bytes:
    """The `refkv.fits_tiles` codec (spec/zarrify/fits.md §3.3
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#33-tile-compressed-images>): a
    chunk's pixels, as little-endian bytes of the array's data type in C order over
    `chunk_shape`, from the chunk's bytes and the codec's configuration (JSON). Raises
    ValueError for a bad configuration or chunk."""
def grib_decode(chunk: bytes, config: str, chunk_shape: Sequence[int]) -> bytes:
    """The `refkv.grib` codec (spec/conventions.md §12
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#12-grib-messages>): the values
    of a field of the GRIB message `chunk`, as little-endian float64 in C order over
    `chunk_shape`, NaN where missing, from the codec's configuration (JSON). Raises
    ValueError for a bad configuration or message."""
def _set_copy_threshold(n: int | None = None) -> None:
    """For tests: the reference payload past which a NIfTI or MRC edge chunk is copied (None:
    the payload limit), so that small files reach the copy (spec/zarrify/nifti.md §7.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nifti.md#72-chunks>,
    spec/zarrify/mrc.md §8.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mrc.md#82-chunks>)."""
def _set_chunk_frames(n: int | None = None) -> None:
    """For tests: the frames of an MP3 chunk (None: 256), so that small files have several chunks,
    each with its lead-in (spec/zarrify/mp3.md §8.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp3.md#82-chunks>)."""
def _set_shard_frames(n: int | None = None) -> None:
    """For tests: the frames of a FLAC shard (None: 512), so that small files have several shards
    (spec/zarrify/flac.md §8.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/flac.md#82-shards>)."""
def project(format: str, ir: Ir, facts: str, url: str) -> Out[FilePart]:  # noqa: A002 (the core's name)
    """The image projection of an IR (`format` a name of `Format::from_name`; facts as JSON) as
    Python objects (`out_py`), or Rejected. Raises ValueError for an unknown format, or facts
    that are not JSON."""
def mirror(ir: Ir, profile: str) -> tuple[Out[FilePart], str]:
    """The mirror of an IR (for the profile "nd2", "tiff", "czi", "dicom", "nifti", "ims", "ndpi",
    "mrc" or "mp3"): its entries and references under `refkv_source` (`out_py`'s form, the url empty), and what the
    table folded (JSON)."""
def canonical_problem(get: Get, read: Read) -> str | None:
    """What makes a stored mirror non-canonical (conventions §8.8
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#88-canonical-form>), or None:
    `get(key)` gives the archive's entries (bytes or None), `read(offset, length)` the source's
    bytes."""
def view_problem(keys: Sequence[str], get: Get, read: Read) -> str | None:
    """Where a stored mirror's view differs from the one its table and the source give
    (conventions §8.7 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#87-the-view>, the
    validator's check), or None: `keys` are the archive's entry
    names, `get(key)` its entries, `read(offset, length)` the source's bytes."""
def synthetic(n: int, fold: bool, distinct: bool) -> tuple[int, int]:
    """A synthetic stream of `n` segments (a struct and a 32-byte header value each),
    emitted into a compact IR, folded into runs or not: the per-element cost."""
def schema_text() -> str: ...
def czi_xml_values(data: bytes) -> str:
    """The values the CZI layout reads from a metadata XML (JSON), as the parser streams them."""
def czi_bands(h: int, h2: int, r: int, w: int, q: int, uncompressed: bool) -> tuple[str, int, int]:
    """The y chunk lengths (JSON) and bands (rows a band holds, bands a tile row) of a
    CZI level of r tile rows of height h (the last h2), w pixels of q bytes wide."""
