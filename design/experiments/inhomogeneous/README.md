# Inhomogeneous files: where virtualization falls off the cliff

An executable companion to
[zarr-developers/zarr-specs#399](https://github.com/zarr-developers/zarr-specs/issues/399)
("Varying codec parameters per-chunk"). [demo.py](demo.py) writes small netCDF4
files that are each valid and consistent on their own, virtualizes each one with
VirtualiZarr (which works), and then tries to combine them along time into one
logical Zarr array, as you would a real archive.

```bash
uv run python design/experiments/inhomogeneous/demo.py
```

| case | the files | what happens |
|---|---|---|
| 1. codec eras (GOES-16) | gzip, gzip, shuffle + gzip, uncompressed | **fails**: a Zarr array has one codec chain |
| 2. rectilinear chunks (monthly files) | 31, 28 and 31 days, one chunk each | **fails**: needs variable-length chunks |
| 3. ragged inner chunks (tiled GeoTIFF / netCDF cubes) | 10 steps each, chunked by 4: 4, 4, 2 | **fails**: the short last chunk breaks the regular grid |
| 4. per-file `scale_factor` / `add_offset` (BOM BARPA) | int16 packed with each file's own scaling | **combines, then reads silently wrong values**: 48 of 72 values differ, because the first file's scaling is applied to every file |
| 5. varying dtype | int16, int16, float32 | **fails**: one array, one dtype |

Case 4 is the dangerous one: nothing errors. The CF attributes are array-level
metadata, so concatenating keeps the first file's `scale_factor` and `add_offset`
and decodes the other files' integers with them.

Versions: VirtualiZarr 2.7.3, zarr 3.4.0, xarray 2026.9.0, h5py 3.16.0. The full
output of a run is in [output.txt](output.txt).
