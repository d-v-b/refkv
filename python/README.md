# refkv: reference-aware key-value storage

refkv defines reference semantics for key-value storage. This allows an object
in one key-value store to refer to a byte range, or sequence of byte ranges,
in other storage backends ([spec](https://github.com/d-v-b/refkv/blob/main/spec/refkv.md)). refkv is used to define
vzip, a single-file reference-aware storage solution: a `.vzip` archive keeps
a reference store in one ordinary ZIP file
([its binding](https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md)).

This package holds reference stores in memory, reads and writes vzip archives,
and zarrifies image files and chunked array stores
([the formats](https://github.com/d-v-b/refkv/blob/main/docs/zarrify/README.md#formats))
through its Rust core, without copying their pixels
([docs](https://github.com/d-v-b/refkv/blob/main/docs/README.md)).

```python
from refkv.zarrify import zarrify

store = zarrify("https://example.org/slide.ndpi")  # a refkv.ReferenceStore
store.save("slide.vzip")                               # one vzip archive
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

```bash
pip install refkv                  # read (local or over http(s)), write, zarrify
pip install "refkv[zarr]"          # refkv.zarr: open a store with zarr-python and xarray
pip install "refkv[images]"        # and the JPEG, JPEG 2000, JPEG XR and LZW codecs
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
refkv leaves the GIL disabled. There every extra installs from wheels on 3.14t; on 3.15t
the base install and `refkv[obstore]` do, but numcodecs (which zarr needs, and so `zarr`,
`images` and `virtualizarr`) and PyAV (`media`) have no 3.15t wheels yet, so pip builds
numcodecs from source and cannot install PyAV. One parser or run is driven by one thread at
a time: a second thread's call on it while one is under way raises `RuntimeError: Already
borrowed`. Separate runs go on in parallel.

Status: experimental, anything can change. Source, spec and issues:
<https://github.com/d-v-b/refkv>.
