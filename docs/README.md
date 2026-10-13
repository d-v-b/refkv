# refkv documentation

refkv defines reference semantics for key-value storage: a value is either bytes or a reference to
byte ranges of other objects, and any key-value store that can tell the two
apart can hold references ([spec](../spec/refkv.md)). vzip is its single-file
binding: a vzip archive keeps such a store in one ordinary ZIP file
([spec/bindings/vzip.md](../spec/bindings/vzip.md)).

[COMPARISON.md](../design/COMPARISON.md) compares vzip with kerchunk JSON, kerchunk Parquet and Icechunk on the same virtual dataset, read over HTTP.

## Demos

Live demos are at https://d-v-b.github.io/vzip-demo/. For example, [image files to zarr](https://d-v-b.github.io/vzip-demo/image-to-zarr/) virtualizes a remote TIFF (including OME-TIFF and Aperio SVS), Hamamatsu NDPI or Nikon ND2 file into a vzip archive in the browser and opens it in Neuroglancer. The demo's code is in [demo/](../demo/), and [js/README.md](../js/README.md) says how to build and serve it.

## Virtualizing image files and stores

refkv can present TIFF, NDPI, ND2, CZI, DICOM, NIfTI and Imaris files, and N5, Zarr v2 and OME-Zarr 0.4 stores, as OME-Zarr, and Sentinel-2 SAFE products as GeoZarr, without copying their pixels. TIFF (NDPI included), ND2, CZI, DICOM, NIfTI, Imaris and SAFE, and Zarr v2 and OME-Zarr stores, are read by a Rust core ([rust/refkv-ir](../rust/refkv-ir/)) that runs natively from Python and as WebAssembly in the browser. [virtualize/](virtualize/README.md) has a page per format: who it is for, what you get, and how to run it from Python or in the browser. In Python, `refkv.virtualize.virtualize(location)`, for a local path or a URL, returns a `refkv.ReferenceStore`, which reads right away, and `store.save("image.vzip")` writes it as one vzip archive.

## Installing

`pip install "refkv @ git+https://github.com/d-v-b/refkv#subdirectory=python"` installs the Python
package's base: it reads and writes vzip archives, and virtualizes files and
stores through refkv's Rust core, which the install compiles into the same
package (this needs a Rust toolchain, such as [rustup](https://rustup.rs)'s). Two extras add the rest:
`refkv[virtualizarr]` for virtual datasets from VirtualiZarr (below; refkv
itself does not import xarray), and `refkv[images]` for the JPEG, JPEG 2000 and JPEG XR codecs that
archives of image files use. In a clone, `uv sync` installs both, and the
repository's test and benchmark tools.

## Virtual datasets from xarray and VirtualiZarr

The Python package `refkv` stores a [VirtualiZarr](https://virtualizarr.readthedocs.io/) virtual dataset, such as many netCDF or HDF5 files concatenated along time with xarray, without copying their data. `refkv.ReferenceStore.from_virtualizarr(vds)` builds a store that `xr.open_zarr(refkv.zarr.store(store))` reads right away, `store.save("data.vzip")` writes it as one vzip archive, and `xr.open_zarr(refkv.zarr.open("data.vzip"))` reopens it, reading each chunk from the file it came from. [virtual-datasets.md](virtual-datasets.md) walks through it with five years of NOAA sea surface temperature: install, a runnable example ([examples/oisst.py](examples/oisst.py)), scaling up, publishing, private data, and troubleshooting.
