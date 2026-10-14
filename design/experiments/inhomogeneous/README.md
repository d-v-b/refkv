# Inhomogeneous files: one array with refkv, where VirtualiZarr can't combine them

An executable companion to
[zarr-developers/zarr-specs#399](https://github.com/zarr-developers/zarr-specs/issues/399)
("Varying codec parameters per-chunk"). [demo.py](demo.py) writes small netCDF4
files, each valid and consistent on its own, in the five shapes the issue
describes. It virtualizes each file with VirtualiZarr (which works). It then
combines them along time into one logical array twice:

- **VirtualiZarr:** concatenating the files' ManifestArrays, as
  `open_virtual_mfdataset` does.
- **refkv:** building one array from the same per-file manifests, with
  [switch.py](switch.py). The result is saved as a vzip archive, reopened, and read
  through zarr-python and xarray.

Both are compared with the files read one by one.

```bash
uv run --with "cast-value-rs>=0.4.2" python design/experiments/inhomogeneous/demo.py
```

| case | the files | VirtualiZarr | refkv |
|---|---|---|---|
| 1. codec eras (GOES-16) | gzip, gzip, shuffle + gzip, uncompressed | fails: one codec chain per array | ✅ 3 cases |
| 2. rectilinear chunks (monthly files) | 31, 28 and 31 days, one chunk each | fails: needs variable-length chunks | ✅ rectilinear grid |
| 3. ragged inner chunks (tiled cubes) | 10 steps each, chunked by 4 (4, 4, 2) | fails: partial chunks break the regular grid | ✅ rectilinear grid, `crop` |
| 4. per-file `scale_factor` / `add_offset` (BOM BARPA) | int16, each file with its own CF scaling | **combines, then 48 of 72 values are silently wrong** | ✅ 3 cases |
| 5. varying dtype | int16, int16, float32 | fails: one array, one dtype | ✅ 2 cases |

In every refkv case, every value equals the files' values. The archive holds a
few kilobytes of metadata; the chunks are read in place from the netCDF files.
[output.txt](output.txt) is a full run.

## How: one codec per transformation, and one that chooses

Each thing that varies between the files is its own, independent codec:

| what varies per file | codec |
|---|---|
| compression, shuffle | the file's own codecs, as VirtualiZarr reports them (`numcodecs.zlib`, `numcodecs.shuffle`) |
| stored dtype | [`cast_value`](https://github.com/zarr-developers/zarr-extensions/tree/main/codecs/cast_value) (zarr-extensions, in zarr-python 3.4) |
| CF `scale_factor` / `add_offset` | [`scale_offset`](https://github.com/zarr-developers/zarr-extensions/tree/main/codecs/scale_offset) (zarr-extensions, in zarr-python 3.4) |
| stored chunk shape (HDF5 stores edge chunks whole) | `crop` (new: decodes by keeping the cell, encodes by padding) |

One codec, `switch`, chooses per chunk which composition of them applies.

- **What `switch` is.** It is an array-to-bytes codec, `{"cases": [chain, ...]}`,
  where each case is an ordinary Zarr codec chain. A chunk's bytes start with a
  varint, the index of its case. `switch` decodes the rest with that chain, the way
  `sharding_indexed` decodes inner chunks with its inner chain.
- **Where the index comes from.** refkv's reference values carry it. A chunk is
  `Concat(Range(data=<varint>), Range(source=<file>, offset, length))`: one literal
  byte in the archive, then the file's own bytes.
- **Grids.** Month-length and ragged files use the zarr-extensions
  [`rectilinear`](https://github.com/zarr-developers/zarr-extensions/tree/main/chunk-grids/rectilinear)
  chunk grid.

Case 4's array, with two of its three cases:

```json
"data_type": "float32",
"chunk_grid": {"name": "rectilinear", "configuration": {"kind": "inline", "chunk_shapes": [[4, 4, 4], [6]]}},
"codecs": [{"name": "switch", "configuration": {"cases": [
  [{"name": "scale_offset", "configuration": {"offset": 280.0, "scale": 100.00000223517424}},
   {"name": "cast_value", "configuration": {"data_type": "int16",
     "scalar_map": {"encode": [["NaN", -32767]], "decode": [[-32767, "NaN"]]}}},
   {"name": "crop", "configuration": {"shape": [4, 6]}},
   {"name": "bytes", "configuration": {"endian": "little"}},
   {"name": "numcodecs.zlib", "configuration": {"level": 4}}],
  [{"name": "scale_offset", "configuration": {"offset": 270.0, "scale": 50.00000111758712}},
   ...]]}}]
```

## Why the chooser is generic, and the formats live in the transformations

The array-to-bytes codec could be scoped to the source product instead: a
netCDF-4/HDF5 codec whose variants are written in HDF5's own terms (datatype,
filter pipeline, chunk dimensions, CF packing), decoding with HDF5's semantics.
The design here splits that apart:

- **The chooser carries no format knowledge.** `switch` only selects a chain. The
  same codec combines TIFF tiles with different compressions, GRIB messages with
  different packing, or HDF5 files with different filters. One codec, not one per
  product.
- **Format knowledge goes where it varies: in single-purpose codecs** that any case
  can use. HDF5's deflate and shuffle are numcodecs codecs already. CF packing is
  `scale_offset`, dtypes are `cast_value`, and HDF5's whole edge chunks are `crop`.
  A product transformation with no Zarr equivalent (HDF5's Fletcher-32 checksum, its
  N-bit or scale-offset filters, SZIP) becomes one more single-purpose codec, usable
  in any case and outside `switch` too, rather than a branch inside a per-product
  codec.
- **Each piece is reviewable on its own.** A reader implements `switch` once.
  Everything inside a case is a codec it either knows or rejects as unknown.

A product-scoped codec would be the better choice only where a product's chunks
can't be described as a chain of independent transformations. That happens when a
per-chunk header has to be parsed: refkv's `n5_default` exists for exactly that
reason.

## Composition

- **Codecs outside `switch`.**
  - Array-to-array codecs listed before it (`transpose`, a uniform `scale_offset`)
    apply to every chunk, whatever its case.
  - Bytes-to-bytes codecs after it would apply to the index *and* the source's bytes,
    so a virtual array has none. A materialized array may.
- **Nesting.** A case is a full chain, so it may end in another `switch`. The chunk
  then starts with the outer index, then the inner one. This composes independent
  axes of variation, for example a codec era (outer) and a per-file scaling (inner),
  at the cost of repeating the inner table in each outer case.
- **Sharding.** A case may be `sharding_indexed` (a source stored as shards), and a
  shard's inner chain may use `switch` (inner chunks that vary).
- **Precedent:** [`conditional`](https://github.com/zarr-developers/zarr-extensions/tree/main/codecs/conditional)
  (zarr-extensions) already puts a per-chunk header in the chunk bytes, to turn codecs
  of one chain on or off. `switch` chooses among chains, so it also covers what
  `conditional` can't: a different dtype, scaling, or stored shape per chunk.
- **Concatenating two such arrays** (VirtualiZarr's manifest-level operations) means
  joining their case lists and renumbering each chunk's index: one literal byte per
  chunk, rewritten by tooling. No source bytes change.

## Limits

- **`switch` and `crop` are defined here**, not registered as zarr extensions.
- **`scale_offset` stores the encode multiplier, CF the decode one.** CF's
  `scale_factor` of 0.01 becomes `scale` 100.00000223517424, the reciprocal of
  float32 0.01. Decoding through `x / scale` matches `x * scale_factor` to within an
  ulp, not bit for bit. Losing no information would need `scale_offset` to accept the
  factor in either direction.
- **The fill value must round-trip through every case.** The array's fill value is
  NaN, and an int16 case can't hold NaN. Each such case's `cast_value` therefore maps
  NaN to the file's CF `_FillValue` and back (`scalar_map`), which is what
  `_FillValue` means. A file without `_FillValue` has no lossless choice, so the demo
  refuses it. An earlier version used `out_of_range: "clamp"` with a fill of 0, which
  can't round-trip either; zarr-python 3.4 accepted it anyway. That is a zarr-python
  bug against the cast_value spec ("If the fill value cannot survive a round-trip
  cast, implementations MUST treat this as an error"), and it is being fixed
  separately.
- **zarr-python's `cast_value` needs the `cast-value-rs` package.**
- **Versions:** VirtualiZarr 2.7.3, zarr 3.4.0, xarray 2026.9.0, h5py 3.16.0.
