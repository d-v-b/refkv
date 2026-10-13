"""An in-memory vzip store, and its persistence as a vzip archive.

A vzip store maps each key to bytes or to a reference value: the
concatenation of byte ranges of other objects, named through the store's one
source table (spec/archive.md, Part 1
<https://github.com/d-v-b/vzip/blob/main/spec/archive.md#part-1-reference-values>). A
`ReferenceStore` holds that mapping in memory, and reads like any vzip store
(`vzip.store.ReferenceReader`); `vzip.zarr.store(store)` opens it for zarr and
xarray, as `vzip.zarr.open` does a saved archive. `save` writes it as a vzip
archive, one ZIP file (Part 2
<https://github.com/d-v-b/vzip/blob/main/spec/archive.md#part-2-the-zip-binding>), which
`vzip.open` reads back as a store with the same values.

`vzip.virtualize.virtualize` returns one too, for an image file or a store:

    import vzip
    from vzip.virtualize import virtualize

    store = virtualize("https://example.org/image.ome.tiff")
    store.info["format"]                # "tiff", and what the profile found
    store.get("zarr.json")              # readable now, in memory
    store.save("image.vzip")            # the archive: one serialization of the store

The workflow for virtual datasets, such as many netCDF files virtualized with
VirtualiZarr and concatenated with xarray:

    import xarray as xr
    import vzip

    store = vzip.ReferenceStore.from_virtualizarr(vds)  # vds: a virtual dataset
    ds = xr.open_zarr(vzip.zarr.store(store))  # readable now: chunks come from the files
    summary = store.save("data.vzip")   # one file: the metadata and the references
    ds = xr.open_zarr(vzip.zarr.open("data.vzip"))
"""

from __future__ import annotations

import bisect
import contextlib
import copy
import dataclasses
import gc
import os
import re
import types
from collections import Counter
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from vzip.archive import INDEX_KEY, RESERVED_PREFIX, SOURCES_KEY, VZipWriter
from vzip.errors import ResolutionError
from vzip.pb import Concat, Range, Reference, Source, parts
from vzip.policy import Policy
from vzip.store import DEFAULT_CONCURRENCY, Kind, ReferenceReader, Request, Stats, _abs_range
from vzip.uri import file_uri, is_uri_reference
from vzip.uri import resolve as resolve_reference


@dataclass(frozen=True)
class Summary:
    """What `ReferenceStore.save` wrote.

    - `path`: the archive.
    - `keys`: the keys of the store (bytes and references; hidden keys not counted).
    - `references`: how many of them are references.
    - `sources`: the entries of the source table: the files the references read,
      and any literal or key sources.
    - `bytes`: the size of the archive.
    """

    path: str
    keys: int
    references: int
    sources: int
    bytes: int

    def __str__(self) -> str:
        return (f"{self.path}: {self.keys:,} keys, {self.references:,} of them references "
                f"to {self.sources:,} sources; {self.bytes:,} bytes")


def _page_size(page_size):
    if page_size is not None and (type(page_size) is not int or page_size <= 0):
        raise ValueError(f"page_size is a positive number of bytes or None, not {page_size!r}")
    return page_size


@contextlib.contextmanager
def bulk():
    """Pauses the cyclic garbage collector while millions of acyclic objects (a
    store's ranges and sources, a writer's records) are built: each collection
    would scan them all again, which made saving a million references take 2.5
    times as long."""
    enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if enabled:
            gc.enable()


# An absolute URL's scheme and authority (RFC 3986 §3)
_ORIGIN = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^/?#]*")


def _rewrite(rewrite_urls: Mapping[str, str] | Callable[[str], str | None] | None):
    """`rewrite_urls` as a function of a URL."""
    if rewrite_urls is None:
        return lambda url: url
    if callable(rewrite_urls):
        return lambda url: rewrite_urls(url) or url
    prefixes = sorted(rewrite_urls, key=len, reverse=True)  # the longest prefix wins

    def f(url: str) -> str:
        for p in prefixes:
            if url.startswith(p):
                return rewrite_urls[p] + url[len(p):]
        return url

    return f


class ReferenceStore(ReferenceReader):
    """An in-memory vzip store: each key maps to bytes or to a reference value,
    resolved through one source table.

    It reads right away: `store.get(key)`, or, for zarr and xarray,
    `xr.open_zarr(vzip.zarr.store(store))`. References are resolved with the same
    reader as a saved archive (`vzip.open`), under the same `Policy`, and with
    the same options. `save` writes it as a vzip archive.

    Build one with a constructor:

    - `vzip.virtualize.virtualize(location)`, from an image file or an N5, Zarr v2,
      OME-Zarr or SAFE store;
    - `ReferenceStore.from_virtualizarr(vds)`, from a VirtualiZarr virtual dataset;
    - `ReferenceStore(mapping, sources)`, from a mapping of keys to `bytes`, `Range`
      or `Concat` values, and the `Source` table their ranges index.

    A store never changes once it is built: it has no method that writes,
    `mapping` and `sources` are read-only views, and through `vzip.zarr` every
    zarr write or delete raises the ValueError zarr raises for a read-only store.
    To change a store, build a new one.

    Keyword arguments:

    - `url`: the base URI that relative source URLs resolve against (spec §5
      <https://github.com/d-v-b/vzip/blob/main/spec/archive.md#5-source-table>). The default,
      the current directory, makes the store a local one for the reader policy:
      it may read `file:` sources, like an archive opened from a local path.
    - `read_at_open`: the keys a reader fetches when it opens the store, which
      `save` places where an archive's opening request reads them. The default
      is every `zarr.json` document.
    - `compressed`: the keys of bytes values that `save` writes DEFLATE-compressed
      (spec §10.1 <https://github.com/d-v-b/vzip/blob/main/spec/archive.md#101-structure>):
      a smaller archive, but a reader then fetches such a value whole to read any
      range of it. The default is none. Each must be a key of a bytes value.
    - `page_size`: the page index `save` writes when it is not given one (see
      `save`; the default, None, writes none).
    - `info`: what the store's builder says about it, as JSON-like values, read
      back as `store.info`; `save` does not write it. `vzip.virtualize` records
      the format and the profile's summary here.

    `save` writes the values in the order of `mapping`, but for those of
    `read_at_open`, which it writes last, in that order too: with `read_at_open`,
    `compressed` and `page_size`, a builder decides the whole archive, byte for byte.
    - `registry`, `stats`, `policy`, `max_concurrency`: how references are
      resolved, as for every vzip store (`vzip.store.ReferenceReader`). The
      default policy, `Policy()`, reads `http(s)` sources on public hosts and,
      here, local files; it refuses other schemes, such as `s3`, except under
      a prefix the `registry` has a store for, such as the
      `ObjectStoreRegistry` given to VirtualiZarr. At most 8 requests are in
      flight by default (`max_concurrency=None` for no cap).
    """

    def __init__(
        self,
        mapping: Mapping[str, bytes | Reference],
        sources: Sequence[Source] = (),
        *,
        url: str | None = None,
        read_at_open: Iterable[str] | None = None,
        compressed: Iterable[str] = (),
        page_size: int | None = None,
        info: Mapping | None = None,
        registry=None,
        stats: Stats | None = None,
        policy: Policy | None = None,
        max_concurrency: int | None = DEFAULT_CONCURRENCY,
    ) -> None:
        super().__init__(url if url is not None else file_uri(os.getcwd()) + "/",
                         registry=registry, stats=stats, policy=policy,
                         max_concurrency=max_concurrency)
        values: dict[str, bytes | Reference] = {}
        for key, v in mapping.items():
            if not isinstance(key, str) or not key:
                raise ValueError(f"keys are non-empty strings, not {key!r}")
            if key in (SOURCES_KEY, INDEX_KEY):
                raise ValueError(f"{key!r} is a format entry, written by `save` itself")
            if isinstance(v, (bytes, bytearray, memoryview)):
                values[key] = bytes(v)
            elif isinstance(v, (Range, Concat)):
                for r in parts(v):
                    if r.data is None and not 0 <= r.source < len(sources):
                        raise ValueError(f"{key!r}: a range uses source {r.source}; the "
                                         f"table has {len(sources)}")
                values[key] = v
            else:
                raise TypeError(f"{key!r}: a value is bytes, a Range or a Concat, "
                                f"not {type(v).__name__}")
        for s in sources:
            if not isinstance(s, Source):
                raise TypeError(f"sources are vzip.pb.Source, not {type(s).__name__}")
        self._values = values
        self._sources = list(sources)
        self._keys = sorted(k for k in values if not k.startswith(RESERVED_PREFIX))
        self._read_at_open = frozenset(
            k for k in values if k.endswith("zarr.json")) if read_at_open is None else frozenset(
            read_at_open)
        self._compressed = frozenset(compressed)
        for key in self._compressed:
            if not isinstance(values.get(key), bytes):
                raise ValueError(f"compressed names {key!r}, which is not a key of a bytes value")
        self._page_size = _page_size(page_size)
        self._info = copy.deepcopy(dict(info or {}))

    # ------------------------------------------------------------ constructors

    @classmethod
    def from_virtualizarr(
        cls,
        vds,
        *,
        rewrite_urls: Mapping[str, str] | Callable[[str], str | None] | None = None,
        **kwargs,
    ) -> ReferenceStore:
        """The store of a VirtualiZarr virtual dataset: its Zarr v3 metadata
        (consolidated in the root `zarr.json`) and its loadable variables as
        bytes, and each chunk of a virtual variable as a reference to the bytes
        of the file that holds it.

        The metadata is what VirtualiZarr's own writers produce
        (`vds.vz.to_icechunk`, `vds.vz.to_kerchunk`), so a dataset reads back as
        it does through them. One CF detail holds for each of them, and for
        `xarray.Dataset.to_zarr` too: Zarr attributes are JSON, so `scale_factor`
        and `add_offset` lose their dtype, and xarray decodes a packed `int16`
        variable that netCDF decodes to float32 as float64 instead (CF's rule,
        pydata/xarray#8713). Cast it back if it matters, `ds["sst"].astype("float32")`,
        or compare packed values with `xr.open_zarr(..., mask_and_scale=False)`.

        - `rewrite_urls`: changes the URLs the references name, before anything
          reads them: a mapping of URL prefixes to their replacements (the
          longest matching prefix wins), or a function of a URL that returns
          its new URL (or None to keep it). For files on a public S3 bucket,
          `rewrite_urls=vzip.public_s3_url` writes `s3://bucket/key` as
          `https://bucket.s3.amazonaws.com/key`, which the default reader
          policy allows, and any reader can read, a browser included.
        - `registry`: the stores to read sources with, such as the
          `ObjectStoreRegistry` given to `open_virtual_dataset`, for private
          data. The store then reads `s3://` sources too; `save` checks them
          against the archive's readers' policy, so pass it
          `policy=Policy(schemes={"s3"})`, and give its readers the registry.
        - Other keyword arguments go to the `ReferenceStore` constructor
          (`policy`, `stats`, `max_concurrency`, `url`).
        """
        from vzip.convert import read_at_open, split_virtual_dataset

        concrete, refs = split_virtual_dataset(vds)
        rewrite = _rewrite(rewrite_urls)
        refs = {k: (rewrite(u), o, n) for k, (u, o, n) in refs.items()}
        # most-used URL first: source index 0 costs nothing on the wire
        urls = [u for u, _ in Counter(u for u, _, _ in refs.values()).most_common()]
        index = {u: i for i, u in enumerate(urls)}
        mapping: dict[str, bytes | Reference] = dict(concrete)
        mapping.update((k, Range(source=index[u], offset=o, length=n))
                       for k, (u, o, n) in refs.items())
        kwargs.setdefault("read_at_open", [k for k in concrete if read_at_open(k, vds)])
        # `save` writes in the mapping's order: the bytes values, then the references, each sorted
        mapping = {k: mapping[k] for k in sorted(mapping, key=lambda k: (not isinstance(mapping[k], bytes), k))}
        return cls(mapping, [Source(url=u) for u in urls], **kwargs)

    # ------------------------------------------------------------ inspection

    @property
    def mapping(self) -> Mapping[str, bytes | Reference]:
        """Every key and its value, as a read-only view."""
        return types.MappingProxyType(self._values)

    @property
    def read_at_open(self) -> frozenset[str]:
        return self._read_at_open

    @property
    def compressed(self) -> frozenset[str]:
        return self._compressed

    @property
    def page_size(self) -> int | None:
        return self._page_size

    @property
    def info(self) -> dict:
        """What the store's builder says about it, as a copy: changing it leaves the
        store's own unchanged. For a store from
        `vzip.virtualize.virtualize`: `format`, the profile that virtualized the
        input (such as "tiff" or "zarr2"), and the profile's summary of what it
        found, as `python -m vzip.virtualize` prints it. Empty otherwise."""
        return copy.deepcopy(self._info)

    def __eq__(self, other: object) -> bool:
        return self is other

    __hash__ = object.__hash__

    def __repr__(self) -> str:
        n = sum(not isinstance(v, bytes) for v in self._values.values())
        return (f"<ReferenceStore: {len(self._keys)} keys, {n} references, "
                f"{len(self._sources)} sources>")

    # ------------------------------------------------------------ persistence

    def save(
        self,
        path: str | os.PathLike,
        *,
        page_size: int | None | types.EllipsisType = ...,
        mirror_refs: bool = True,
        relative_to: str | None = None,
        policy: Policy | None = None,
    ) -> Summary:
        """Write the store as a vzip archive at the local `path`, and return a
        `Summary` of it. `vzip.open(path)` reads it back with the same values.

        The entries are written in the store's order (see the class), the bytes
        values of `compressed` DEFLATE-compressed.

        - `page_size`: the store's `page_size` by default. None writes no page index, and a reader
          then loads the whole central directory, about 125 bytes per key, when
          it opens the archive. A number writes a page index (spec §12
          <https://github.com/d-v-b/vzip/blob/main/spec/archive.md#12-page-index-optional>) with
          pages of about that many bytes, so a reader loads only the pages that
          hold the keys it reads. `65536` is a good value once the directory
          grows past a few MB, and matters most for an archive read over the
          network. In design/COMPARISON.md, 259,200 references opened over
          HTTP in 2 requests (115 kB) with 64 KiB pages, against 18.8 MB without
          an index; but a time series that touches every page took 8.5 s with
          the index against 7.4–7.6 s without. For a few thousand keys, leave it None.
        - `mirror_refs`: True (the default) also writes each reference's
          payload as its entry's body, so a ZIP tool that ignores extra fields
          still sees it (spec §11.3
          <https://github.com/d-v-b/vzip/blob/main/spec/archive.md#113-reference-entries> says
          writers should); False leaves those bodies empty, a smaller archive.
        - `relative_to`: a URL prefix, such as the URL of the directory the
          archive will be served from. Source URLs under it are written relative
          to the archive, so the archive and its sources can move together.
        - `policy`: the reader policy the sources are checked against before
          anything is written (default `Policy()`, the default reader's). A
          source it refuses raises ValueError, which says how to fix it: write
          `s3://` URLs of a public bucket as HTTPS
          (`ReferenceStore.from_virtualizarr(vds, rewrite_urls=vzip.public_s3_url)`),
          or pass the policy the archive's readers will use, such as
          `Policy(schemes={"s3"})`.
        """
        path = os.fspath(path)
        base = file_uri(path)
        sources = self._sources
        if relative_to:
            sources = [dataclasses.replace(s, url=s.url.removeprefix(relative_to))
                       if s.url is not None and s.url.startswith(relative_to) else s
                       for s in sources]
        check = policy or Policy()
        refused = []
        # Without a prefix list, the policy decides a valid absolute URL by its scheme and
        # host alone: one check per origin, not per source (a store has a source per
        # object). Each URL is still checked to be a URI reference, as resolving it does.
        by_origin: dict[str, str | None] = {}
        for s in sources:
            if s.url is None:
                continue
            origin = _ORIGIN.match(s.url) if check.prefixes is None else None
            if origin is not None and origin[0] in by_origin and is_uri_reference(s.url):
                error = by_origin[origin[0]]
            else:
                try:
                    resolved = resolve_reference(base, s.url)
                except ValueError as e:  # not a URI reference: its own error, not its origin's
                    error = str(e)
                else:
                    try:
                        check.check(base, resolved)
                        error = None
                    except (ResolutionError, ValueError) as e:
                        error = str(e)
                    if origin is not None:
                        by_origin[origin[0]] = error
            if error is not None:
                refused.append(error)
        if refused:
            whose = "the default reader policy" if policy is None else "the reader policy given"
            raise ValueError(f"not saving {path}: {whose} refuses {len(refused)} of its "
                             f"{len(sources)} sources, so a reader could not read them. "
                             f"The first: {refused[0]}")
        page_size = self._page_size if page_size is ... else _page_size(page_size)
        refs = 0
        with bulk(), open(path, "wb") as f, VZipWriter(f, mirror_refs=mirror_refs,
                                                       page_size=page_size) as w:
            index = [w.source(s) for s in sources]  # equal sources share an index
            for k, v in self._values.items():
                if isinstance(v, bytes):
                    late, compress = k in self._read_at_open, k in self._compressed
                    if k.startswith(RESERVED_PREFIX):
                        w.add_hidden(k[len(RESERVED_PREFIX):], v, compress=compress, late=late)
                    else:
                        w.add_bytes(k, v, compress=compress, late=late)
                else:
                    w.add_ranges(k, [r if r.data is not None or index[r.source] == r.source else
                                     dataclasses.replace(r, source=index[r.source])
                                     for r in parts(v)])
                    refs += 1
            n_sources = len(w._source_list)
        return Summary(path, len(self._keys), refs, n_sources, Path(path).stat().st_size)

    # ------------------------------------------------------------ reading

    async def _key_bytes(self, key: str, start: int, end: int) -> bytes:
        v = self._values.get(key)
        if v is None:
            raise ResolutionError(f"key source {key!r} is missing")
        if not isinstance(v, bytes):
            raise ResolutionError(f"key source {key!r} is a reference")
        if end > len(v):
            raise ResolutionError(f"key source {key!r} is shorter than {end} bytes")
        return v[start:end]

    def _visible(self, key: str) -> bytes | Reference | None:
        return None if key.startswith(RESERVED_PREFIX) else self._values.get(key)

    async def get_async(self, key: str, request: Request | None = None) -> bytes | None:
        _abs_range(0, request)  # request errors, even for missing keys
        v = self._visible(key)
        if v is None:
            return None
        if isinstance(v, bytes):
            start, end = _abs_range(len(v), request)
            return v[start:end]
        start, end = _abs_range(v.size, request)
        return await self._ref_bytes(v, start, end)

    async def classify_async(self, key: str) -> Kind:
        v = self._visible(key)
        return "missing" if v is None else "bytes" if isinstance(v, bytes) else "reference"

    async def raw_async(self, key: str) -> bytes | None:
        """A bytes value, a hidden key's included; a reference's payload, as `save`
        writes it (spec §4.4
        <https://github.com/d-v-b/vzip/blob/main/spec/archive.md#44-reference-payloads>), with
        the source indexes of this store's table. The format entries an archive
        holds are written by `save`, and are missing here."""
        v = self._values.get(key)
        if v is None or isinstance(v, bytes):
            return v
        ranges = parts(v)
        return ranges[0].encode() if len(ranges) == 1 else Concat(ranges).encode()

    async def list_async(self, prefix: str = "") -> AsyncIterator[str]:
        i = bisect.bisect_left(self._keys, prefix)
        while i < len(self._keys) and self._keys[i].startswith(prefix):
            yield self._keys[i]
            i += 1

    async def list_dir_async(self, prefix: str = "") -> AsyncIterator[str]:
        prefix = prefix.rstrip("/")
        prefix = prefix + "/" if prefix else ""
        seen: set[str] = set()
        async for k in self.list_async(prefix):
            child = k[len(prefix):].split("/", 1)[0]
            if child not in seen:
                seen.add(child)
                yield child


__all__ = ["ReferenceStore", "Summary"]
