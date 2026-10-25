# refkv documentation

refkv defines reference semantics for key-value storage: a value is either bytes or a reference to
byte ranges of other objects, and any key-value store that can tell the two
apart can hold references ([spec](../spec/refkv.md)). vzip is its single-file
binding: a vzip archive keeps such a store in one ordinary ZIP file
([spec/bindings/vzip.md](../spec/bindings/vzip.md)).

[COMPARISON.md](../design/COMPARISON.md) compares vzip with kerchunk JSON, kerchunk Parquet and Icechunk on the same virtual dataset, read over HTTP.

## Demos

Live demos are at https://d-v-b.github.io/vzip-demo/. For example, [image files to zarr](https://d-v-b.github.io/vzip-demo/image-to-zarr/) zarrifies a remote TIFF (including OME-TIFF and Aperio SVS), Hamamatsu NDPI, Akoya QPTIFF or Nikon ND2 file into a vzip archive in the browser and opens it in Neuroglancer. The demo's code is in [demo/](../demo/), and [js/README.md](../js/README.md) says how to build and serve it.

## Zarrifying image files and stores

refkv **zarrifies** image files and chunked array stores ([the formats](zarrify/README.md#formats)): it presents them as OME-Zarr (a Sentinel-2 SAFE product as GeoZarr), without copying their pixels, through a Rust core ([rust/refkv-ir](../rust/refkv-ir/)) that runs natively from Python and as WebAssembly in the browser. [zarrify/](zarrify/README.md) has a page per format: who it is for, what you get, and how to run it from Python or in the browser. In Python, `refkv.zarrify.zarrify(location)`, for a local path or a URL, returns a `refkv.ReferenceStore`, which reads right away, and `store.save("image.vzip")` writes it as one vzip archive. Without Python, `vzip convert <path or URL> image.vzip` ([rust/refkv-cli](../rust/refkv-cli/), built by `just cli-test`) writes the same archive, byte for byte, with the options and exit statuses of `python -m refkv.zarrify`.

## Installing

`pip install "refkv @ git+https://github.com/d-v-b/refkv#subdirectory=python"` installs the Python
package's base: it reads and writes vzip archives, local or at `http(s)` URLs,
and zarrifies files and stores through refkv's Rust core, which the install
compiles into the same package (this needs a Rust toolchain, such as
[rustup](https://rustup.rs)'s). Extras add the rest:
`refkv[virtualizarr]` for virtual datasets from VirtualiZarr (below; refkv
itself does not import xarray), `refkv[images]` for the JPEG, JPEG 2000 and JPEG XR codecs that
archives of image files use, `refkv[zarr]` for `refkv.zarr`, and
`refkv[obstore]` for obstore's `S3Store` and the other cloud stores, to pass
as `registry=` for `s3://`, `gs://` or `az://` sources (`refkv[virtualizarr]`
includes it). In a clone, `uv sync` installs them all, and the
repository's test and benchmark tools.

## Virtual datasets from xarray and VirtualiZarr

The Python package `refkv` stores a [VirtualiZarr](https://virtualizarr.readthedocs.io/) virtual dataset, such as many netCDF or HDF5 files concatenated along time with xarray, without copying their data. `refkv.ReferenceStore.from_virtualizarr(vds)` builds a store that `xr.open_zarr(refkv.zarr.store(store))` reads right away, `store.save("data.vzip")` writes it as one vzip archive, and `xr.open_zarr(refkv.zarr.open("data.vzip"))` reopens it, reading each chunk from the file it came from. [virtual-datasets.md](virtual-datasets.md) walks through it with five years of NOAA sea surface temperature: install, a runnable example ([examples/oisst.py](examples/oisst.py)), scaling up, publishing, private data, and troubleshooting.
