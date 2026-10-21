# FITS

refkv turns an image file into a Zarr hierarchy by writing a small archive of
references to the file's bytes, without copying the pixels
([overview](../README.md)). This page is about FITS files.

## Who this is for

FITS (`.fits`, `.fz`) is the archive format of astronomy: HST and JWST
exposures and mosaics, Rubin and Euclid images, DECam, Pan-STARRS, Legacy
Surveys and ZTF survey products, spectra and light curves, and radio cubes.
A file is a sequence of HDUs (header-data units), each a header of 80-byte
cards and a block of data: an image, a table, or a tile-compressed image
(what `fpack` writes to `.fz` files, the ground-based archives' usual form).
Someone who wants to read one chip of a multi-extension file lazily over
HTTP, open a `.fz` file in a Zarr reader without `funpack`, or put a mosaic
next to other arrays in a Zarr store has to convert it today.

refkv reads the headers (and, for a tile-compressed image, its table of
tile descriptors), never the data, and writes an archive in which every HDU
is a node: an image is an array whose chunks are byte ranges of the file,
a tile-compressed image an array whose chunks are runs of its compressed
tiles, decoded by the `fits_tiles` codec, and a table its rows and heap as
bytes. Every header's cards come along.

## What you get

A Zarr v3 group at the archive root, with one child per HDU at the path of
its index (`0`, `1`, …), and no OME-NGFF metadata: FITS axes are sky
coordinates, spectral axes and table rows, which the world coordinate
system cards describe for astropy and the viewers.

- **Images** (the primary with data, `IMAGE` extensions): an array of the
  FITS axes reversed (`[NAXIS2, NAXIS1]` for a 2-D image), of the `BITPIX`
  type (`uint8`, `int16`, `int32`, `int64`, `float32`, `float64`),
  big-endian `bytes` codec, chunks of at most 1 MiB cut along the slowest
  axis that allows it. The stored values: `BSCALE`, `BZERO` and `BLANK` are
  recorded in the node's metadata, not applied (a `uint16` image stored as
  `int16` with `BZERO = 32768` is an `int16` array).
- **Tile-compressed images** (`BINTABLE` with `ZIMAGE = T`): an array of
  the image's shape and type, chunked by runs of tiles up to 1 MiB
  decoded (128 rows of a DECam chip, 18 tiles of a Legacy Surveys brick),
  with the `fits_tiles` codec: `RICE_1`, `GZIP_1`, `GZIP_2` and
  `NOCOMPRESS` tiles, quantized floats dequantized with cfitsio's dither.
  Each chunk is one byte range of the heap (tiles are stored one after the
  other) behind a small prefix that carries every tile's length, scale and
  zero, so the codec needs nothing but the chunk's bytes.
- **Tables** (`BINTABLE`, `TABLE`): a group with the arrays `rows`
  (`[NAXIS2, NAXIS1]` of `uint8`) and `heap` (the variable-length arrays),
  and the column definitions in the group's metadata. Any FITS table
  reader decodes the bytes; columns are not arrays yet.
- **Empty HDUs** (`NAXIS = 0`, the empty primary of a multi-extension file):
  a group with the header's cards.
- **Cards:** every HDU's node carries its header as `[keyword, value,
  comment]` triples under `refkv_virtualized.fits.cards`, typed (logicals,
  integers, reals, strings with `CONTINUE` joined, `HIERARCH` keywords), in
  order with duplicates, up to 64 KB of JSON; the root carries the table of
  HDUs (type, name, version, offsets, compression) and the primary's cards.
  The whole file is described on `refkv_source` by the Rust core's IR
  mirror, from which it is rebuilt byte for byte.

**What is not expressed.** The world coordinate system is kept as cards,
not interpreted (astropy.wcs reads them from the node's metadata); value
scaling is recorded, not applied; table columns are bytes.
[spec/virtualize/fits.md §8](../../../spec/virtualize/fits.md#8-what-the-layout-does-not-express)
lists them.

## Try it

A Legacy Surveys brick (DR10, `g` band): 3600 × 3600 float32, RICE
compressed in 100 × 100 tiles with subtractive dithering, 11 MB, from
[corpus_fits.txt](../../../conformance/virtualize/corpus_fits.txt):

```bash
uv run python -m refkv.virtualize https://portal.nersc.gov/cfs/cosmo/data/legacysurvey/dr10/south/coadd/000/0001m002/legacysurvey-0001m002-image-g.fits.fz brick.vzip
```

```
{"format": "fits", "hdus": 2, "kinds": {"empty": 1, "image": 1}, "arrays": [{"path": "1", "shape": [3600, 3600], "dataType": "float32", "chunkShape": [100, 1800], "compression": "RICE_1"}], "chunks": 72, "cards": 171, "tables": 0, ...}
```

The archive is 109 KB; virtualizing read 96 KB of the file (the two
headers and the 1296-row table of tile descriptors). Read the image:

```python
import zarr
import refkv.zarr

g = zarr.open_group(refkv.zarr.open("brick.vzip"), mode="r")
print(g.attrs["refkv_virtualized"]["fits"]["hdus"][1]["compression"])
a = g["1"]
print(a.shape, a.dtype, a.chunks)
print(a[1800:1803, 1800:1804])
print([c for c in a.attrs["refkv_virtualized"]["fits"]["cards"] if c[0] in ("CTYPE1", "CRVAL1", "BUNIT")])
```

```
{'type': 'RICE_1', 'parameters': {'BLOCKSIZE': 32, 'BYTEPIX': 4}, 'zbitpix': -32, 'tile': [100, 100], 'quantize': 'SUBTRACTIVE_DITHER_2', 'dither_seed': 6049, 'zblank': None, 'tiles': 1296}
(3600, 3600) float32 (100, 1800)
[[ 0.00104921  0.00260768 -0.00169845 -0.00120833]
 [ 0.00286447 -0.00218325 -0.00076782 -0.00165043]
 [ 0.00144809  0.00024898  0.00051489 -0.00029411]]
[['CTYPE1', 'RA---TAN', 'TANgent plane'], ['CRVAL1', 0.125, 'Reference RA'], ['BUNIT', 'nanomaggy', 'AB mag = 22.5 - 2.5*log10(nanomaggy)']]
```

These are astropy's values (`fits.open(path)[1].data[1800:1803, 1800:1804]`)
to within the last bits of the dequantization: the convention computes
`(I − R + 0.5) × ZSCALE + ZZERO` in binary64 with each operation rounded,
where a build of cfitsio may fuse the multiply and the add. Reading those
three rows fetched one 1 MiB chunk: 18 tiles, one range of the heap.

**In the browser.** The demo page virtualizes a FITS file and serves it as
Zarr; the Neuroglancer fork displays uncompressed images, and
tile-compressed ones once it has the `fits_tiles` codec (the Rust core
exports it to wasm as `vz_fits_tiles_decode`).

## Supported and not supported

Supported:

- Images of every `BITPIX`, multi-extension files, `NAXIS = 0` HDUs,
  special records after the last HDU, a truncated final block.
- Tile-compressed images with `RICE_1` (`BLOCKSIZE` 16 or 32, `BYTEPIX` 1,
  2 or 4), `GZIP_1`, `GZIP_2` and `NOCOMPRESS`, tiles of any shape,
  quantized floats with `NO_DITHER`, `SUBTRACTIVE_DITHER_1` and
  `SUBTRACTIVE_DITHER_2`, `ZBLANK` as a card or a column, tiles stored as
  gzip fallbacks, absent tiles, unused heap bytes between tiles, `P` and
  `Q` descriptors.
- Binary and ASCII tables, as bytes.

Rejected:

- Whole-file gzip and bzip2 (`.fits.gz`, SDSS's `.fits.bz2`): a compressed
  stream has no byte ranges to reference; decompress the file first.
- Non-conforming files (`SIMPLE = F`), files whose first card is
  `XTENSION`, random groups (`GROUPS = T`), tile-compressed tables
  (`ZTABLE = T`), `HCOMPRESS_1` and `PLIO_1` compression, the deprecated
  `UNCOMPRESSED_DATA` column and `NULL_PIXEL_MASK`.
- Headers without `END` within 16 MiB, cards with bytes outside printable
  ASCII, mandatory cards out of order or not integers, data past the end
  of the file, tiles stored out of order in the heap, more than 65536
  HDUs, a tile table over 64 MiB.

## Performance

- Virtualizing reads each header in one 64 KiB window (several small HDUs
  share one), and a tile-compressed image's table of descriptors (8 to 24
  bytes per tile) in one range. A DECam exposure of 61 chips reads about
  10 MB of a 331 MB file; a plain multi-extension file reads its headers
  alone.
- Reading a chunk of an image is one range request of at most 1 MiB; of a
  tile-compressed image, one range of its tiles' streams (about 170 KB for
  128 DECam rows), decoded in Rust.
- The archive has one entry per chunk: 72 for a Legacy Surveys brick, about
  2000 for a DECam exposure.

## How it's verified

`js/test/fits/verify.py` virtualizes the synthetic FITS files (including
inputs to reject) with the browser code, reads them back through
`python/src/refkv` and zarr-python, and compares every HDU's values and
cards with astropy's. `python/tests/ir/test_fits.py` does the same through
Python, and the Rust core's tests check the RICE decoder and the
dequantizer against streams and values astropy's codec produced.
`compare.py` checks that the Python and browser outputs are equivalent on
those files and on the public files of
[corpus_fits.txt](../../../conformance/virtualize/corpus_fits.txt).

- Convention and profile: [spec/virtualize/fits.md](../../../spec/virtualize/fits.md)
- Rust core: [rust/refkv-ir/src/fits.rs](../../../rust/refkv-ir/src/fits.rs), the codec [rust/refkv-ir/src/fits_tiles.rs](../../../rust/refkv-ir/src/fits_tiles.rs)
- Fixtures: [fixtures/fits/](../../../fixtures/fits/)
- Design: [design/plans/fits.md](../../../design/plans/fits.md)
