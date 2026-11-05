# Viewing a contiguous array in blocks

A file whose pixels are one uncompressed block in row-major order (MRC, NIfTI, NRRD, NPY, FITS,
DICOM's native encoding) is zarrified as **contiguous values**, cut into slabs
([spec/conventions.md §7](../spec/conventions.md#7-source-values-as-arrays)):
each chunk is whole planes or whole rows, so each is one range of the file. `refkv.zarr.contiguous`
and `refkv.zarr.blocks` serve the same bytes on another chunk grid, such as cubes, without
rewriting the file.

## What the slab cut gives a viewer

The slab cut is the one that makes every chunk a single range, and it is thin along one axis. A
512³ uint16 NIfTI or NRRD file zarrifies to chunks `[1, 128, 512]`, and a 512³ uint16 MRC or NPY
file to `[2, 512, 512]`. A viewer such as Neuroglancer reads a small region by the chunks that
cover it, so with these it can only browse xy: a view along x or y reads every plane.

## Why a cube is not stored

A chunk of a flat file is one range because its bytes are adjacent. A 64³ block of a 512³ volume
is not: it is `64 × 64` row ranges, about 40 to 50 KB of payload per chunk against about 10 bytes for
a slab chunk. An index of such blocks would be thousands of times the size of the slab index (a 4096³
volume has 262144 of them, over 10 GB of payload), and a block of 128 × 128 rows exceeds a reference's
65519-byte payload cap outright. So a cube is not a stored reference: the view computes the ranges when
it reads, from where each element is in the file.

## The two calls

- `refkv.zarr.contiguous(store)` returns the arrays of a `refkv.zarr.ZarrStore` that are this cut,
  by path: each chunk one range of one source, at the offset the cut gives it, with a `bytes`
  codec alone.
- `refkv.zarr.blocks(store, {path: shape})` returns a read-only zarr store (`BlocksStore`) that
  serves each array of the mapping in chunks of its shape. A block past the array's end is
  zero-padded; every other key is the store's own. It raises `ValueError` for a path that is
  not contiguous, a shape of another rank than the array's, or an axis below 1.

```python
import refkv.zarr
from refkv.zarrify import zarrify

zs = refkv.zarr.store(zarrify("tomogram.mrc"))  # or refkv.zarr.open("tomogram.vzip")
found = refkv.zarr.contiguous(zs)
bs = refkv.zarr.blocks(zs, {p: (1,) * (len(c.shape) - 3) + (64, 64, 64) for p, c in found.items()})
```

`bs` is a zarr store: `zarr.open_group(bs, mode="r")` reads 64³ chunks, and Neuroglancer shows
them through a server that serves `bs`. A block shape has the array's rank, so a 4-D or 5-D array
takes 1 on its leading axes, as above.

## How a block is read

A block is read from the file by the ranges of its rows:

- When a row of the block is at least `row_bytes_threshold` bytes (default 4096), one range per
  row.
- Otherwise one range per plane, from the first row the block needs to the end of the last. A
  plane's range holds the whole rows of the array in between, so it reads up to `S[-1] / cx` times
  the bytes it returns, for a block `cx` wide of an array `S[-1]` wide, in one request instead of one
  per row.

The reference store reads a block's ranges in values of at most its policy's `max_reads` ranges,
and merges ranges within 64 KiB of each other into one read: read span-wise, a 64-plane cube takes
at most one request per plane, and fewer when its planes lie within 64 KiB of each other. The same merging
means the row-wise strategy reads fewer bytes only when the gap between a block's rows exceeds
64 KiB (`(S[-1] − cx) × e > 65536`, for `e`-byte elements), so on a 4096-wide uint16 volume (an 8 KiB row pitch)
both read about 33 MB per 64³ block for the 512 KiB it returns, the 64× over-read above;
`row_bytes_threshold` chooses the strategy, and cannot beat the merge gap.

## Limits

The view is made when you ask for it, and nothing is stored. A browser that reads the archive
directly, with no `blocks` view in between, still sees the slab grid.
