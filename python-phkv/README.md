# refkv-phkv: the perfect-hash binding of refkv

[refkv](https://pypi.org/project/refkv/) defines reference semantics for key-value
storage: an object in one store can refer to a byte range, or a sequence of byte ranges,
in other storage backends
([spec](https://github.com/d-v-b/refkv/blob/main/spec/refkv.md)). **phkv** keeps such a
store in one file with a perfect hash of its keys, so that a lookup reads a few ranges of
the file however many keys it holds
([its binding](https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md), a draft).
This package reads and writes phkv stores from Python. It is a distribution of its own,
beside `refkv`, which keeps the ZIP binding (vzip archives): the draft binding changes
without the ZIP binding noticing.

```python
import refkv_phkv
from refkv.zarrify import zarrify

store = zarrify("https://example.org/slide.ndpi")  # a refkv.ReferenceStore
refkv_phkv.save(store, "slide.phkv")               # one phkv store

phkv = refkv_phkv.open("slide.phkv")  # a read-only store
phkv.get("zarr.json")                 # bytes, from the file
phkv.get("0/c/0/0/0")                 # a reference: bytes of the slide
```

`refkv.open(url, binding="phkv")` (and `refkv.zarr.open(url, binding="phkv")`, with the
`zarr` extra) finds this package through the entry-point group `refkv.bindings`, and
reads a phkv store with the same options as an archive: `policy`, `registry`, `stats`
and `max_concurrency`. `refkv.bindings()` lists the bindings that are installed.

```bash
pip install refkv-phkv           # with refkv
pip install "refkv-phkv[zarr]"   # and refkv.zarr, for zarr-python and xarray
```

- `refkv_phkv.save(store, path)`, or `store.save_phkv(path)`, which calls it, writes a
  `refkv.ReferenceStore`, checking its sources
  against the reader policy first, as `ReferenceStore.save_vzip` does. The ZIP binding's hidden
  values (`__vz__/`) are not written, unless a `key` source refers to one.
- `refkv_phkv.PhkvWriter` writes a store key by key: bytes values (none, DEFLATE or
  Zstandard, small ones inline in their record), references, per-key properties and
  metadata.
- `refkv_phkv.PhkvStore` reads one. A lookup reads the slot of the key's hash and then one
  record, after the end of the file, which opening reads.

A phkv store is not a ZIP file, so ZIP tools cannot open it, and vzip readers cannot read it.
The sizes and request counts against a paged vzip archive are in the
[benchmarks](https://github.com/d-v-b/refkv/blob/main/rust/refkv-phkv/bench/RESULTS.md).
Python does not read or write the hybrid profile (a vzip archive with a phkv store inside).

Wheels are built for Linux (glibc and musl), macOS and Windows, on x86_64 and arm64
(Python 3.12 and later), and for free-threaded CPython 3.14t and 3.15t.

Status: experimental, anything can change. Source, spec and issues:
<https://github.com/d-v-b/refkv>.
