# Inhomogeneous files: one array with refkv, where VirtualiZarr can't combine them

An executable companion to
[zarr-developers/zarr-specs#399](https://github.com/zarr-developers/zarr-specs/issues/399)
("Varying codec parameters per-chunk"). [demo.py](demo.py) writes small netCDF4
files, each valid and consistent on its own, in the five shapes the issue
describes. It virtualizes each file with VirtualiZarr (which works), and then
combines them along time into one logical array twice:

- **VirtualiZarr:** by concatenating the files' ManifestArrays, as
  `open_virtual_mfdataset` does.
- **refkv:** from the same per-file manifests, with [variant.py](variant.py). The
  result is saved as a vzip archive, reopened, and read through zarr-python and
  xarray.

Every result is compared with the files read one by one.

```bash
uv run python design/experiments/inhomogeneous/demo.py
```

| case | the files | VirtualiZarr | refkv |
|---|---|---|---|
| 1. codec eras (GOES-16) | gzip, gzip, shuffle + gzip, uncompressed | fails: one codec chain per array | ✅ one array, 3 chunk variants |
| 2. rectilinear chunks (monthly files) | 31, 28 and 31 days, one chunk each | fails: needs variable-length chunks | ✅ rectilinear grid |
| 3. ragged inner chunks (tiled cubes) | 10 steps each, chunked by 4 (4, 4, 2) | fails: partial chunks break the regular grid | ✅ rectilinear grid, edge chunks cropped |
| 4. per-file `scale_factor` / `add_offset` (BOM BARPA) | int16, each file with its own CF scaling | **combines, then 48 of 72 values are silently wrong** (the first file's scaling is applied to all) | ✅ each chunk decoded with its own file's scaling |
| 5. varying dtype | int16, int16, float32 | fails: one array, one dtype | ✅ int16 chunks cast to the array's float32 |

In every refkv case, every value equals the files' values. The archive holds a few
kilobytes of metadata, and the chunks are read in place from the netCDF files.
[output.txt](output.txt) is a full run.

## How

Two pieces, and no data is copied:

1. **`chunk_variant`** is an array-to-bytes codec. Its configuration lists the ways a
   chunk is stored ("variants"). Each variant has a codec chain (numcodecs
   configurations), a stored dtype and byte order, the stored chunk shape, and
   optionally a CF `scale_factor` and `add_offset`. A chunk's bytes start with a
   varint, the index of its variant. The codec decodes the rest with that variant,
   crops it to the chunk's cell (HDF5 stores edge chunks whole), applies the scaling,
   and casts the result to the array's dtype. This is design 2.1 of the issue: the
   per-chunk context injected into the chunk bytes.
2. **refkv's reference values carry the varint.** Each chunk's value is
   `Concat(Range(data=<varint>), Range(source=<file>, offset, length))`: one literal
   byte stored in the archive, then the file's own bytes. Ragged and month-length
   files use the zarr-extensions
   [`rectilinear`](https://github.com/zarr-developers/zarr-extensions/tree/main/chunk-grids/rectilinear)
   chunk grid, which zarr-python 3.4 reads with
   `zarr.config.set({"array.rectilinear_chunks": True})`.

This answers the issue's objections to design 2.1:

- **No store does anything at read time.** The context lives in the reference, so
  the archive is an ordinary key-value store whose values happen to start with a
  byte. Arrays that don't use the codec aren't affected.
- **No codec does I/O.** The variant table is in `zarr.json`. Each chunk carries
  only an index into it: one byte here, a varint in general.
- **It scales to the pathological case.** One variant per file in case 4 costs one
  table entry per file plus one byte per chunk. A run of files that share a variant,
  such as GOES-16's four eras, shares one entry.
- **It fails loudly.** A reader without the codec refuses the array as an unknown
  codec. It does not return wrong values.

## Limits

- `chunk_variant` is defined here, not registered as a zarr extension. A reader
  needs this codec (as refkv's readers need `n5_default` and the image codecs).
- Each variant decodes with numcodecs, not with a nested zarr codec pipeline, which
  keeps the sketch short. A zarr extension would nest zarr codec configurations.
- Concatenating archives (VirtualiZarr's manifest-level operations) needs the variant
  tables merged and the chunks' indexes renumbered. That is a literal byte per chunk,
  but tooling would have to do it.
- Versions: VirtualiZarr 2.7.3, zarr 3.4.0, xarray 2026.9.0, h5py 3.16.0.
