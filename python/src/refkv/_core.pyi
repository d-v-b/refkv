# The stub of `refkv._core`, the Rust core's Python bindings (rust/refkv-python/src/lib.rs):
# the parsers as step/feed/finish objects, the runs, and the IR's columns, tables and
# per-element reads. Its docstrings are the `///` docs of lib.rs, which `help()` shows from the
# compiled module, and the ValueErrors each raises, which lib.rs leaves to its code; keep the two
# the same.
#
# The parsers and the runs are used up by `finish()` (and a run by `output()`): any call after
# it raises ValueError("finished").

from collections.abc import Callable, Sequence
from typing import Literal, final

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

class SealRefused(ValueError):  # noqa: N818 (the core's name)
    """`seal` refuses an output: `args` are (code, key or None, message)."""

class Failed(ValueError):  # noqa: N818 (the core's name)
    """A run's output is not written, by no fault of the input: a payload over the limit only with
    its checksums (spec/zarrify.md §1.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>)."""

class Violation(Exception):  # noqa: N818 (the core's name)
    """An IR broke an invariant (conventions §8.1
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#81-elements>)."""

class NoCodec(ValueError):  # noqa: N818 (the core's name)
    """No codec undoes the coding parameters a routine was given (`coding`)."""

def coding(name: str, params: str) -> str:
    """A coding routine of `refkv.zarrify.coding` (spec/layout.md
    <https://github.com/d-v-b/refkv/blob/main/spec/layout.md>), or a host rule
    (rust/refkv-ir/src/host): the routine `name` of the parameters `params` (JSON), its result as
    JSON. Raises NoCodec for parameters the format's section rejects, Rejected for an input a host
    rule rejects, ValueError for parameters that are not the routine's."""

@final
class ChunkMap:
    """An IR's chunk map as one Arrow record batch, for the Arrow PyCapsule interface: pyarrow,
    polars or nanoarrow read it without a copy. A requested schema is ignored.

    Only `Ir.chunk_map()` makes one: the class has no constructor (calling it raises TypeError),
    and an instance can be neither pickled nor copied; read it into pyarrow, polars or nanoarrow
    to keep it. Its length is its rows', so a map of no rows is falsy."""

    def __len__(self) -> int:
        """Its rows."""
    def __arrow_c_schema__(self) -> object:
        """The schema, a capsule named `arrow_schema`."""
    def __arrow_c_array__(self, requested_schema: object | None = None) -> tuple[object, object]:
        """The batch as a struct array: capsules named `arrow_schema` and `arrow_array`."""
    def __arrow_c_stream__(self, requested_schema: object | None = None) -> object:
        """A stream of the one batch, a capsule named `arrow_array_stream`."""

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
    def stored_arrays(self) -> str:
        """The arrays the source stores (spec/stored-array.md §2.5
        <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#25-the-json-form>): one JSON
        document, the chain table, the descriptions and the `undescribed` mark."""
    def units(self, array: int) -> list[tuple[list[int], int | None, int | None, tuple[int, int] | None,
                                              list[tuple[str | None, int, int]] | None, int | None, int | None]]:
        """The chunk map's rows of the stored array at position `array`, each (coords, element,
        member, range, ranges, chain, skips), as spec/stored-array.md §4.1
        <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#41-the-arrow-schema> gives
        the columns: `member` is the position of the run's member the element is in."""
    def chunk_map(self) -> ChunkMap:
        """The chunk map (spec/stored-array.md §4
        <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#4-the-chunk-map>): an object
        of the Arrow PyCapsule interface, one record batch."""
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

@final
class Chunking:
    """How refkv's convention cuts a source into chunks (`out::Chunking`): the reference payload of
    an edge chunk past which a projection copies it (`copy_threshold`, the payload limit), the
    frames of an MP3 chunk and the most of an MP4 or H5J one (`chunk_frames`, 256), and the frames
    of a FLAC shard (`shard_frames`, 512), each the convention's when not given, the frames at
    least 1. A run holds one from its start (`Run(..., chunking=)`, `StoreRun(..., chunking=)`);
    another than the convention's is for tests, so that small files reach the copy, several chunks
    or several shards, and its output is not the convention's."""

    def __init__(self, *, copy_threshold: int | None = None, chunk_frames: int | None = None,
                 shard_frames: int | None = None) -> None: ...
    @property
    def copy_threshold(self) -> int: ...
    @property
    def chunk_frames(self) -> int: ...
    @property
    def shard_frames(self) -> int: ...

class Run:
    """A run: a format's parser and the read planner (sans-IO). The host loops
    `poll()` (the requests to make now, each a list of (start, end) spans: one
    span a plain range request, several a multi-range one) and `complete(k,
    parts, seconds)` (each span's bytes, or None for a multi-range request the
    server did not answer with its ranges), until `poll()` gives None; then
    `finish()` gives the IR, the parser's description (JSON) and the planner's counts, with the
    parser's as `parser` (JSON).

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
        chunking: Chunking | None = None,
    ) -> None: ...
    @property
    def chunking(self) -> Chunking:
        """How the run's projection cuts the source, given at its start (default: the convention's)."""
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
    def beside(self) -> bool:
        """Whether the file names siblings, once `poll` gave None: the run has no output, and
        the host starts over with a store run of `format`."""
    @property
    def format(self) -> str:
        """The run's format: "tiff" becomes "ndpi" once its parser finds the file is NDPI."""
    def stats(self) -> str:
        """The planner's counts and model (JSON)."""
    def output(self, url: str, mirror: bool = True, layouts: list[str] | None = None,
               checksums: bool = False, written: bool = True) -> tuple[Out[FilePart], Ir]:
        """Finishes, projects and (with `mirror`) mirrors: (the output in `out_py`'s form,
        the IR). `layouts`, when given, names the layouts the projection writes beside the
        image (a format's dataset layout, "dataset"; conventions §9
        <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#9-dataset-layouts>); by
        default, every one. `checksums`: the host records the ranges' checksums, so that a
        sharded array's ranges are not joined across inner chunks (spec/zarrify.md §2.1
        <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#21-virtual-shards>)."""
    def finish(self) -> tuple[Ir, str, str]: ...
    def defer_projection(self) -> None:
        """Leaves the built-in projection's reads out of the run, before its first poll:
        `poll()` gives None once the IR is whole, `ir()` gives it, and a host that wants the
        built-in output calls `begin_projection()`, polls again until None, then `project(...)`,
        as often as it likes."""
    def begin_projection(self) -> None:
        """Asks for the built-in projection's reads, of a run that left them out and is done."""
    def description(self) -> str:
        """The profile's description of the source (JSON), of a run that is done."""
    def ir(self) -> Ir:
        """The IR of a run that is done, shared with the run, which is kept."""
    def view(self) -> object | None:
        """The input as its format's own class gives it (rust/refkv-python/src/formats/<profile>.rs),
        its IR and description shared with the run; None for a format that has none. A caller
        narrows it with `isinstance`."""
    def mirror(self) -> Mirror:
        """What makes the mirror of a run that is done: its IR and description, shared with the run,
        which is kept and may be used while the mirror is made."""
    def project(self, url: str, mirror: bool = True, layouts: list[str] | None = None,
                checksums: bool = False, written: bool = True) -> Out[FilePart]:
        """`output`, of a run that is kept: the output alone."""

@final
class Mirror:
    """What makes the IR mirror of an input a run parsed: its IR and its description, shared with the
    run and never changed, so `entries()` may be called from any thread, as often as the host
    likes, while the run is used for something else (its output) or after it is gone."""

    def entries(self) -> Out[FilePart] | Out[StorePart]:
        """The mirror under `refkv_source` (the url empty), with what the input's convention
        records beside it from the run's description, the keys and directories a listing ignored: what
        the built-in output writes there, but for a store's objects; or Rejected."""

class StoreRun:
    """A store run (src/store.rs): `StoreRun(format, listing, concurrency)` over the listing
    [(key, size)]; the host loops `poll()` (the requests to make now, concurrently: (object
    index, key, spans [(start, end)])) and `complete(k, parts)` (each span's bytes) until
    `poll()` gives None; then `output(url, mirror)` or `finish()`. `ignore(keys, folders)`,
    before the first poll, gives the keys the listing ignores.

    The constructor raises ValueError for an unknown store `format`; it, `poll` and
    `complete` raise Rejected when the store is rejected."""

    def __init__(self, format: str, listing: Sequence[tuple[str, int]], concurrency: int,  # noqa: A002 (the core's name)
                 chunking: Chunking | None = None) -> None: ...
    @property
    def chunking(self) -> Chunking:
        """How the run's parser and projection cut the store's arrays, given at its start (default:
        the convention's)."""
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
    def found_as(self, size: int, key: str) -> None:
        """The answer to the next probe of the batch, found at another key by listing
        (`StoreRun::found_as`): its size and that key."""
    def ignore(self, keys: Sequence[str], folders: Sequence[str] = ...) -> None:
        """The keys the listing ignores, before the first poll (`StoreRun::ignore`): `keys`, the
        relative keys the store reader ignored and recorded (`Store.ignored`), which the
        projection records as its convention says, and `folders`, its empty `d/` keys
        (`Store.folders`), which a SAFE directory's parser reads as directories."""
    def scope(self, within: Sequence[str]) -> None:
        """The scope of the input, before the first poll (`StoreRun::scope`): the keys besides the
        listing's the host may be asked to probe, each a key or a directory's key followed by `/`."""
    def profile(self) -> str:
        """The profile the run's store is decided to be so far (`StoreRun::profile`)."""
    def output(self, url: str, mirror: bool = True, checksums: bool = False) -> tuple[Out[StorePart], Ir]: ...
    def finish(self) -> tuple[Ir, str, str]: ...
    def parse(self) -> Ir:
        """Finishes without an output: the IR, which the run keeps with its description and counts, so
        that `project(...)` gives the built-in output of it, as often as the host likes."""
    def description(self) -> str:
        """The profile's description of the input (JSON), of a run `parse` finished."""
    def mirror(self) -> Mirror:
        """What makes the mirror of a run `parse` finished: its IR and description, shared with the
        run."""
    def view(self) -> object | None:
        """The input as its format's own class gives it, of a run `parse` finished: its IR and
        description shared with the run, with the bytes the run read for its projection; None for
        a format that has none. A caller narrows it with `isinstance`."""
    def project(self, url: str, mirror: bool = True, checksums: bool = False) -> Out[StorePart]:
        """`output`, of a run `parse` finished: the output alone."""

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
# A part of a value for `seal`: literal bytes, (0, url, offset, length), (1, data, offset, length)
# or (2, length) of zero bytes.
type SealPiece = bytes | tuple[int, ...]

def seal(urls: list[tuple[str, int | None, str | None]], data: list[bytes], first: int | None,
         documents: list[str], values: list[tuple[str, list[SealPiece]]], max_sources: int, checksums: bool,
         ) -> tuple[list[tuple[str, int]], list[tuple[str, list[tuple[int, int, int] | bytes]]]]:
    """The core's seal (rust/refkv-ir/src/seal.rs) of the output whose bytes entries' keys are
    `documents` and whose values are (key, pieces), the pieces naming `urls` (url, size,
    etag) and `data`, `first` the url that is source 0: (its table, each source ("url", index),
    ("data", index) or ("zeros", length); its values in key order, each (key, ranges), a range
    (source, offset, length) or bytes). Raises SealRefused."""

def crc32c(data: bytes) -> int:
    """CRC-32C (Castagnoli, RFC 3720 §B.4) of `data`: a range's checksum (spec §4.2
    <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#42-range>)."""
def sniff(head: bytes) -> str | None:
    """The format a file's first bytes (up to 65,600 of them) give (`Format::sniff`), if any: its
    profile's name."""
def registry() -> str:
    """The profile registry (spec/zarrify/profiles/, as the core holds it): its entries, by
    name, as a JSON array."""
def h5_bitshuffle_decode(chunk: bytes, elementsize: int, block_size: int, lz4: bool) -> bytes:
    """The `refkv.h5_bitshuffle` codec (spec/zarrify/hdf5.md §3.4
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#34-the-codec-refkvh5_bitshuffle>): a chunk's bytes
    from HDF5's bitshuffle filter."""


def fits_tiles_decode(chunk: bytes, config: str, chunk_shape: Sequence[int]) -> bytes:
    """The `refkv.fits_tiles` codec (spec/zarrify/fits.md §3.3
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#33-tile-compressed-images>): a
    chunk's pixels, as little-endian bytes of the array's data type in C order over
    `chunk_shape`, from the chunk's bytes and the codec's configuration (JSON). Raises
    ValueError for a bad configuration or chunk."""
def grib_decode(chunk: bytes, config: str, chunk_shape: Sequence[int]) -> bytes:
    """The `refkv.grib` codec (spec/zarrify/grib.md §3.4
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/grib.md#34-the-codec-refkvgrib>): the values
    of a field of the GRIB message `chunk`, as little-endian float64 in C order over
    `chunk_shape`, NaN where missing, from the codec's configuration (JSON). Raises
    ValueError for a bad configuration or message."""
def grid_chunks(offset: int, shape: list[int], item: int, limit: int,
                copy_threshold: int) -> tuple[list[int], list[tuple[list[int], int, int, int, bool]]] | None:
    """The C-order array of `shape`, of `item`-byte elements contiguous at `offset`, cut as
    spec/conventions.md §7 cuts contiguous values into chunks of at most `limit` bytes: its chunk
    shape, and each chunk's coordinates, its range (offset, length), the zero bytes that pad it,
    and whether the range is to be copied, as it is past `copy_threshold` (a run's
    `Chunking.copy_threshold`); None when an element is larger than `limit` or the
    sizes overflow."""
type FitsPiece = (tuple[Literal["src"], int, int] | tuple[Literal["lit"], bytes] | tuple[Literal["zeros"], int]
                 | tuple[Literal["copy"], int, int])

@final
class Fits:
    """A FITS file a run parsed (`Run.view()`): its IR and its description, shared with the run and
    never changed, and its heaps' elements, found once."""

    def arrays(self, index: int) -> list[tuple[str, str, list[tuple[list[int], list[FitsPiece]]]]]:
        """The arrays of HDU `index` as the FITS convention writes them, but for their
        declarations: each `(path, document JSON, chunks)`, `path` under the HDU's node (`""`,
        `rows`, `heap`), each chunk `(coords, parts)`: `("src", offset, length)` of the file,
        `("lit", bytes)`, `("zeros", length)` of an edge chunk's padding, or `("copy", offset,
        length)` of the file, to be copied. ValueError for no HDU `index`."""
    def cards(self, index: int) -> str:
        """The cards of HDU `index` as the FITS convention writes them: JSON, each `[keyword,
        value, comment]`. ValueError for no HDU `index`."""

class Zarr:
    """A Zarr v2 or v3 store a run parsed (`Run.view()` of a zipped Zarr v3 store,
    `StoreRun.view()`): its IR and its description, shared with the run and never changed."""

    def hierarchy(self) -> tuple[str, Out[FilePart | StorePart]]:
        """The Zarr convention's hierarchy but for refkv's declarations and its `refkv_source/`
        objects: its nodes as JSON, each its path, kind and source metadata as JSON text, and its
        documents and chunks (`out_py`'s form, the url empty); or Rejected."""

class Hdf5:
    """An HDF5 input a run parsed (`Run.view()`, `StoreRun.view()`): its IR and its description,
    shared with the run and never changed."""

    def hierarchy(self) -> tuple[str, Out[FilePart | StorePart], list[tuple[str, int, int, int]]]:
        """The HDF5 convention's hierarchy: its nodes as JSON, its documents and chunks (`out_py`'s
        form, the url empty) but for the edge chunks it copies, and those, each `(key, offset,
        length, padding)` of the file; or Rejected."""
    def named_key(self) -> str | None:
        """The key of the file a store run was named by; None for a file run."""

@final
class OmeImage:
    """A TIFF, QPTIFF, NDPI or IMS input a run parsed (`Run.view()`, `StoreRun.view()`):
    its IR and its description, shared with the run and never changed."""

    @property
    def profile(self) -> str:
        """`tiff`, `qptiff`, `ndpi` or `ims`."""
    def reads(self) -> list[tuple[int, int]]:
        """The file's ranges the image's `omero` windows sample, `(offset, length)`; or Rejected."""
    def image(self, copied: list[tuple[tuple[int, int], bytes]],
              checksums: bool = False) -> tuple[str, Out[FilePart | StorePart]]:
        """The OME image: its nodes as JSON, its documents and chunks (`out_py`'s form, the url
        empty), its arrays sharded; `copied` the bytes of the ranges `reads` gave; or Rejected."""
    def named_key(self) -> str | None:
        """The key of the file a store run was named by; None for a file run."""

@final
class Netcdf3:
    """A netCDF3 file a run parsed (`Run.view()`): its IR and its description, shared with the
    run and never changed."""

    def hierarchy(self) -> tuple[str, Out[FilePart], list[tuple[str, int, int, int]]]:
        """The netCDF3 convention's hierarchy: its nodes as JSON, its documents and chunks
        (`out_py`'s form, the url empty) but for the edge chunks it copies, and those, each `(key,
        offset, length, padding)` of the file; or Rejected."""

class Envi:
    """An ENVI raster a store run parsed (`StoreRun.view()`): its description, shared with the run
    and never changed."""

    def image(self) -> tuple[str, str, list[tuple[list[int], int, int]], str, str]:
        """The ENVI convention's image: the root's source metadata (JSON), the array's document
        (JSON, with no attributes), its chunks, each `(coords, offset, length)` of the data file,
        and the header and data files' keys."""

def gzip_frame(crc32: int, size: int) -> tuple[bytes, bytes]:
    """What frames a raw deflate stream as a gzip member, as the built-in projections write one:
    the 10-byte header, and the trailer, the stream's CRC-32 and the size of what it inflates to,
    modulo 2^32."""
def mirror(ir: Ir, profile: str) -> tuple[Out[FilePart], str]:
    """The mirror of an IR under `profile`'s convention (a name of spec/zarrify/profiles/): its
    entries and references under `refkv_source` (`out_py`'s form, the url empty), and what the
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
def czi_xml_values(data: bytes) -> str:
    """The values the CZI layout reads from a metadata XML (JSON), as the parser streams them."""
def czi_bands(h: int, h2: int, r: int, w: int, q: int, uncompressed: bool) -> tuple[str, int, int]:
    """The y chunk lengths (JSON) and bands (rows a band holds, bands a tile row) of a
    CZI level of r tile rows of height h (the last h2), w pixels of q bytes wide."""

# a phkv store could not be read: `.args` is (class, message), the class one of "archive", "entry",
# "body", "payload", "resolution", "request", "property" and "io" (spec §6.4
# <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#64-errors>)
class PhkvReadError(Exception): ...

# a bytes value a phkv store looked up, from `PhkvStore.key_found`
class PhkvFound:
    @property
    def size(self) -> int: ...
    @property
    def held(self) -> int | None: ...

# a phkv store (spec/bindings/phkv.md), read through `size()` and `read(offset, length)`; every
# method blocks on those, so a host calls them from a thread
class PhkvStore:
    def __init__(
        self,
        size: Callable[[], int],
        read: Callable[[int, int], bytes],
        sources: int | None = None,
        open_bytes: int | None = None,
        request_bytes: int | None = None,
    ) -> None: ...
    @staticmethod
    def tail() -> int: ...
    @property
    def revision(self) -> int | None: ...
    @property
    def key_count(self) -> int: ...
    @property
    def hybrid(self) -> bool: ...
    def sources(self) -> list[bytes]: ...
    def metadata(self) -> list[tuple[str, bytes]]: ...
    def classify(self, key: str) -> Literal["bytes", "reference"] | None: ...
    def entry(self, key: str) -> tuple[Literal["bytes", "reference"], int, bool] | None: ...
    def fetch(
        self, key: str, mode: int, a: int, b: int
    ) -> tuple[Literal["bytes"], bytes] | tuple[Literal["reference"], int, bytes] | None: ...
    def key_bytes(self, key: str, start: int, end: int) -> bytes: ...
    def key_found(self, key: str) -> PhkvFound | None: ...
    def found_bytes(self, found: PhkvFound, start: int, end: int) -> bytes: ...
    def raw(self, key: str) -> bytes | None: ...
    def list(self, prefix: str) -> list[str]: ...
    def property(self, key: str, name: str) -> int | str | bytes | None: ...

class PhkvWriter:
    def __init__(
        self,
        f: object,
        codec_: int = 0,
        inline_max: int = 4096,
        crc32c: bool = True,
        section_codec: int = 2,
        page_size: int = 4096,
    ) -> None: ...
    def source(self, message: bytes) -> int: ...
    def add_bytes(self, key: str, data: bytes, codec_: int | None = None) -> None: ...
    def add_file(self, key: str, path: str) -> None: ...
    def add_ranges(self, key: str, ranges: Sequence[tuple[int, int, int, int | None] | bytes]) -> None: ...
    def set_property(self, key: str, name: str, kind: str, value: int | str | bytes) -> None: ...
    def set_metadata(self, name: str, value: bytes) -> None: ...
    def close(self) -> None: ...

class VzipWriter:
    """rust/refkv-vzip's archive writer, writing to the binary file object `f` as each entry is
    added (`refkv.vzip.archive.VZipWriter` wraps it): sources as `refkv.pb.Source` messages,
    ranges as `(source, offset, length, crc32c)` or literal bytes."""
    def __init__(self, f: object, mirror_refs: bool = True, page_size: int | None = None) -> None: ...
    def source(self, message: bytes) -> int: ...
    def source_count(self) -> int: ...
    def add_bytes(self, key: str, data: bytes, compress: bool, late: bool) -> None: ...
    def add_hidden(self, name: str, data: bytes, compress: bool, late: bool) -> str: ...
    def add_file(self, key: str, path: str) -> None: ...
    def add_ranges(self, key: str, ranges: Sequence[tuple[int, int, int, int | None] | bytes]) -> None: ...
    def add_url_refs(self, items: Sequence[tuple[str, str, int, int | None]], pin_size: bool) -> None: ...
    def close(self) -> None: ...
    def format_sizes(self) -> dict[str, int]: ...
