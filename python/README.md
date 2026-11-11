# refkv: reference-aware key-value storage

refkv defines reference semantics for key-value storage. This allows an object
in one key-value store to refer to a byte range, or sequence of byte ranges,
in other storage backends ([spec](https://github.com/d-v-b/refkv/blob/main/spec/refkv.md)). refkv is used to define
vzip, a single-file reference-aware storage solution: a `.vzip` archive keeps
a reference store in one ordinary ZIP file
([its binding](https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md)).

This package holds reference stores in memory, reads and writes vzip archives and
phkv stores (the draft perfect-hash binding, below),
and zarrifies image files and chunked array stores
([the formats](https://github.com/d-v-b/refkv/blob/main/docs/zarrify/README.md#formats))
through its Rust core, without copying their pixels
([docs](https://github.com/d-v-b/refkv/blob/main/docs/README.md)).

```python
from refkv.zarrify import zarrify

store = zarrify("https://example.org/slide.ndpi")  # a refkv.ReferenceStore
store.save_vzip("slide.vzip")                          # one vzip archive
```

The format comes from the file's first bytes, not its name. A source or store
class requires one format instead, and carries the input's read options
([more](https://github.com/d-v-b/refkv/blob/main/docs/zarrify/README.md)):
`zarrify(NdpiSource(url))` reads an NDPI slide or raises `Rejected`, and
`zarrify(Zarr2Store(url))` reads a store whose URL has no trailing `/`.

From async code, `await zarrify_async(url)` (and `zarrify_store_async`) takes the
same arguments and gives the same store, the event loop free while the reads
are in flight, so that one process can zarrify several inputs at once.

For netCDF, HDF5 and the other files
[VirtualiZarr](https://virtualizarr.readthedocs.io/) reads,
`refkv.ReferenceStore.from_virtualizarr(vds)` turns a virtual dataset into a
store that xarray opens ([example](https://github.com/d-v-b/refkv/blob/main/docs/virtual-datasets.md)).

## Bindings

A reference store can be kept in a vzip archive (`refkv.vzip`, `store.save_vzip(path)`) or in a phkv
store (`refkv.phkv`, `store.save_phkv(path)`); `refkv.open(url)` reads the first, and
`refkv.open(url, binding="phkv")` the second. Both read through the same policy, registry and
`Stats`, and both open as zarr stores with `refkv.zarr.open`. `refkv.vzip_to_phkv(src, dst)` and
`refkv.phkv_to_vzip(src, dst)` convert a saved store from one to the other without reading what its
references point to (`refkv convert src.vzip dst.phkv` on the command line; `--page-size`, `--relative-to`
and `--allow-scheme file`, for a store whose sources are local files, are its options). A source URL that is
relative stays relative if it resolves to the same place from the new file. The conversion drops what the
other binding has no place for: a vzip archive's hidden keys (but those a `key` source names, which a phkv
store lists), its page index and entry order, and a phkv store's properties (`mtime`, `mode`, `media_type`)
and metadata. A phkv key under `__vz__/`, which a vzip archive reserves, is refused unless a `key` source
names it. The whole store is read into memory, and a remote phkv store costs about one request per key.

| | vzip | phkv, standalone | phkv, hybrid |
|---|---|---|---|
| the file is | a ZIP archive | a phkv store | a ZIP archive with a phkv store inside |
| read by ZIP tools | yes | no | yes |
| read by vzip readers | yes | no | yes, unchanged |
| finding a key | bisect a page index and scan one page (about 64 KiB), or read the whole central directory | hash the key, read a 12-byte slot, read one record | as phkv readers do, or as vzip readers do |
| opening plus the first key | 4 requests | 3 requests (about 516 KiB read) | per reader |
| size, 100,000 single-range references | about 120 bytes per key | about 40 bytes per key | about 160 bytes per key |
| bytes values | stored or DEFLATE | none, DEFLATE or Zstandard; small values inline in the record | stored or DEFLATE |
| per-key properties | none | typed columns (`mtime`, `mode`, `media_type`, your own) | for phkv readers |
| reserved names | `__vz__/` | none | `__vz__/` |
| reference payload limit | 65,519 bytes | 16 MiB | 65,519 bytes |
| Python | `refkv.vzip`, `store.save_vzip(path)` | `refkv.phkv`, `store.save_phkv(path)`, `refkv.open(url, binding="phkv")` | not yet |
| status | provisional, with a conformance suite | draft | draft |

The sizes and request counts are the reference writers' and readers' on the same store
([spec §16.3](https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#163-comparison-with-the-zip-binding-informative), [benchmarks](https://github.com/d-v-b/refkv/blob/main/rust/refkv-phkv/bench/RESULTS.md)).
Write vzip for the widest compatibility, and when ZIP tools or vzip-only readers will read it. Write phkv
for the smallest file and the fewest requests per lookup, when every reader knows phkv; the hybrid
keeps both kinds of reader working, at the largest size.

```bash
pip install refkv                  # read (local or over http(s)), write, zarrify
pip install "refkv[zarr]"          # refkv.zarr: open a store with zarr-python and xarray
pip install "refkv[images]"        # and the JPEG, JPEG 2000, JPEG XR and LZW codecs
pip install "refkv[media]"         # and the refkv.stream decoders (PyAV), for MP3, FLAC and MP4
pip install "refkv[precomputed]"   # and the refkv.precomputed decoders of compresso, crackle, fpzip, kempressed
pip install "refkv[virtualizarr]"  # virtual datasets from VirtualiZarr (with obstore)
pip install "refkv[obstore]"       # obstore's S3Store and others, for registry= (s3://, gs://, az://)
```

The base install reads archives and sources over `http(s)` with refkv's own HTTP client,
which refuses loopback, private and link-local addresses unless
`Policy(allow_private_hosts=True)`, and requests through an HTTP proxy, whose
connections it cannot check, unless `Policy(allow_unchecked_proxy=True)`: an
archive's own URL included.

Wheels are built for Linux (glibc and musl), macOS and Windows, on x86_64 and arm64
(Python 3.12 and later), and for free-threaded CPython 3.14t and 3.15t and later, where
refkv leaves the GIL disabled. There every extra but `precomputed` installs from wheels on
3.14t; on 3.15t the base install and `refkv[obstore]` do, but numcodecs (which zarr needs, and
so `zarr`, `images` and `virtualizarr`) and PyAV (`media`) have no 3.15t wheels yet, so pip
builds numcodecs from source and cannot install PyAV. compresso and fpzip (`precomputed`) have
wheels for CPython 3.12 and 3.13 only, so pip builds them from source on later versions. One parser or run is driven by one thread at
a time: a second thread's call on it while one is under way raises `RuntimeError: Already
borrowed`. Separate runs go on in parallel.

Status: experimental, anything can change. Source, spec and issues:
<https://github.com/d-v-b/refkv>.
