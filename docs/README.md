# refkv documentation

refkv defines reference semantics for key-value storage: a value is either bytes or a reference to
byte ranges of other objects, and any key-value store that can tell the two
apart can hold references ([spec](../spec/refkv.md)). vzip is its single-file
binding: a vzip archive keeps such a store in one ordinary ZIP file
([spec/bindings/vzip.md](../spec/bindings/vzip.md)).

[COMPARISON.md](../design/COMPARISON.md) compares vzip with kerchunk JSON, kerchunk Parquet and Icechunk on the same virtual dataset, read over HTTP.

## Demos

Live demos are at https://d-v-b.github.io/refkv-demo/. For example, [image files to zarr](https://d-v-b.github.io/refkv-demo/image-to-zarr/) zarrifies a remote file or store in any of [the formats](zarrify/README.md#formats) into a vzip archive in the browser, with an example of each, and opens it in Neuroglancer (a Sentinel-2 product on a map). It also shows the Python script that builds the same hierarchy with refkv. When the browser cannot read the input, the page says why: the server does not allow this site to read it (CORS), it cannot be reached, or it is an `http://` URL read from the `https://` page. For a CORS refusal it points to the Python script, which CORS does not restrict. The demo's code is in [demo/](../demo/), and [js/README.md](../js/README.md) says how to build and serve it.

## Zarrifying image files and stores

refkv **zarrifies** image files and chunked array stores ([the formats](zarrify/README.md#formats)): it presents them as OME-Zarr (a Sentinel-2 SAFE product as GeoZarr), without copying their pixels, through a Rust core ([rust/refkv-ir](../rust/refkv-ir/)) that runs natively from Python and as WebAssembly in the browser. [zarrify/](zarrify/README.md) has a page per format: who it is for, what you get, and how to run it from Python or in the browser. In Python, `refkv.zarrify.zarrify(location)`, for a local path or a URL, returns a `refkv.ReferenceStore`, which reads right away, and `store.save("image.vzip")` writes it as one vzip archive. Without Python, `vzip convert <path or URL> image.vzip` ([rust/refkv-cli](../rust/refkv-cli/), built by `just cli-test`) writes the same archive, byte for byte, with the options and exit statuses of `python -m refkv.zarrify`. [adding-a-format.md](adding-a-format.md) is the guide to adding a format: the research, the layout decisions, the files to write, and what went wrong before.

## Installing

`pip install refkv` installs the Python
package's base from PyPI: it reads and writes vzip archives, local or at `http(s)` URLs,
and zarrifies files and stores through refkv's Rust core, which the wheel
holds as the module `refkv._core`. Extras add the rest:
`refkv[zarr]` for `refkv.zarr` and refkv's other codecs,
`refkv[images]` for the JPEG, JPEG 2000 and JPEG XR codecs that
archives of image files use (imagecodecs-zarr's) and the LZW one (refkv's),
`refkv[media]` for the decoders of the `refkv.stream` codec, which reads
zarrified MP3, FLAC and MP4 files (ffmpeg's, through PyAV),
`refkv[virtualizarr]` for virtual datasets from VirtualiZarr (below; refkv
itself does not import xarray), and
`refkv[obstore]` for obstore's `S3Store` and the other cloud stores, to pass
as `registry=` for `s3://`, `gs://` or `az://` sources (`refkv[virtualizarr]`
includes it). `images` and `media` include `zarr`.

```bash
pip install "refkv[images]"   # zarrify image files and read them with zarr-python
pip install "refkv[media]"    # the same for MP3, FLAC and MP4
```

[python/README.md](../python/README.md) lists every extra and the platforms
with wheels (Linux, macOS and Windows, free-threaded CPython included); where
none fits, pip builds refkv from source, which needs a Rust toolchain, such as
[rustup](https://rustup.rs)'s. In a clone, `uv sync` installs refkv from the
clone with every extra, and the repository's test and benchmark tools.

## Virtual datasets from xarray and VirtualiZarr

The Python package `refkv` stores a [VirtualiZarr](https://virtualizarr.readthedocs.io/) virtual dataset, such as many netCDF or HDF5 files concatenated along time with xarray, without copying their data. `refkv.ReferenceStore.from_virtualizarr(vds)` builds a store that `xr.open_zarr(refkv.zarr.store(store))` reads right away, `store.save("data.vzip")` writes it as one vzip archive, and `xr.open_zarr(refkv.zarr.open("data.vzip"))` reopens it, reading each chunk from the file it came from. [virtual-datasets.md](virtual-datasets.md) walks through it with five years of NOAA sea surface temperature: install, a runnable example ([examples/oisst.py](examples/oisst.py)), scaling up, publishing, private data, and troubleshooting.
