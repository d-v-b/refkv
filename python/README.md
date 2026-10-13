# vzip: virtual zip

vzip is ZIP plus a reference value type: a key-value store in which every
value is either bytes or a reference to byte ranges of other objects, such
as the chunks inside HDF5, netCDF or TIFF files. A vzip archive keeps such a
reference store in one ordinary ZIP file
([spec](https://github.com/d-v-b/refkv/blob/main/spec/refkv.md)).

This package reads and writes vzip archives, and virtualizes TIFF, NDPI, ND2,
CZI, DICOM, NIfTI and Imaris files, N5, Zarr v2 and OME-Zarr stores, and
Sentinel-2 SAFE products, through its Rust core, without copying their pixels
([docs](https://github.com/d-v-b/refkv/blob/main/docs/README.md)). For netCDF, HDF5 and the other files
[VirtualiZarr](https://virtualizarr.readthedocs.io/) reads, it turns a virtual
dataset into a store that xarray opens, and saves it as a vzip archive
([example](https://github.com/d-v-b/refkv/blob/main/docs/virtual-datasets.md)).

```bash
pip install vzip               # read, write, virtualize
pip install "vzip[zarr]"       # vzip.zarr: open an archive with zarr-python and xarray
pip install "vzip[images]"     # and the JPEG, JPEG 2000 and JPEG XR codecs
pip install "vzip[virtualizarr]"  # virtual datasets from VirtualiZarr
```

Virtualizing returns a `vzip.ReferenceStore`, which reads right away, in
memory; saving it writes the vzip archive:

```python
from vzip.virtualize import virtualize

store = virtualize("https://example.org/image.ome.tiff")
store.info["format"]      # "tiff", and the profile's summary
store.save("image.vzip")  # one ZIP file: the hierarchy and the references
```

Wheels are built for Linux and macOS (Python 3.12 and later); Windows is not
supported yet.

Status: experimental, anything can change. Source, spec and issues:
<https://github.com/d-v-b/refkv>.
