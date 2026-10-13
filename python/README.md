# refkv: reference-aware key-value storage

refkv defines reference semantics for key-value storage. This allows an object
in one key-value store to refer to a byte range, or sequence of byte ranges,
in other storage backends ([spec](https://github.com/d-v-b/refkv/blob/main/spec/refkv.md)). refkv is used to define
vzip, a single-file reference-aware storage solution: a `.vzip` archive keeps
a reference store in one ordinary ZIP file
([its binding](https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md)).

This package holds reference stores in memory, reads and writes vzip archives,
and virtualizes TIFF, NDPI, ND2, CZI, DICOM, NIfTI and Imaris files, N5,
Zarr v2 and OME-Zarr stores, and Sentinel-2 SAFE products through its Rust
core, without copying their pixels ([docs](https://github.com/d-v-b/refkv/blob/main/docs/README.md)).

```python
from refkv.virtualize import virtualize

store = virtualize("https://example.org/slide.ndpi")  # a refkv.ReferenceStore
store.save("slide.vzip")                               # one vzip archive
```

For netCDF, HDF5 and the other files
[VirtualiZarr](https://virtualizarr.readthedocs.io/) reads,
`refkv.ReferenceStore.from_virtualizarr(vds)` turns a virtual dataset into a
store that xarray opens ([example](https://github.com/d-v-b/refkv/blob/main/docs/virtual-datasets.md)).

```bash
pip install refkv                  # read, write, virtualize
pip install "refkv[zarr]"          # refkv.zarr: open a store with zarr-python and xarray
pip install "refkv[images]"        # and the JPEG, JPEG 2000 and JPEG XR codecs
pip install "refkv[virtualizarr]"  # virtual datasets from VirtualiZarr
```

Wheels are built for Linux and macOS (Python 3.12 and later); Windows is not
supported yet.

Status: experimental, anything can change. Source, spec and issues:
<https://github.com/d-v-b/refkv>.
