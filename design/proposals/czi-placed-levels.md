# Proposal: CZI levels from clipped and overlapping tiles

A slide scan zarrified today shows only its smallest image. `same_czi_no_spaces.czi`, a 7-scene
slide, gives scene 0 one array of 948 × 475 pixels, at 14 µm per pixel, for a region of about
60,655 × 30,373 pixels. The rest of the scene is in the store as 891 of the file's 6,494 separate
tile arrays, which no multiscale names, so a viewer never opens them.

This proposal adds one codec, `refkv.placed`, and two kinds of level that use it. With both, that
scene's image goes down to full resolution. No pixels are copied and every chunk is still one read.

| | today | placed levels | placed and cropped levels |
|---|---|---|---|
| scene 0's largest array | 948 × 475 | 30,720 × 15,360 | 60,564 × 30,296 |
| its pixel size | 14.06 µm | 0.44 µm | 0.22 µm |
| its datasets | 1 | 5 | 6 |
| tile arrays in the file | 6,494 | 4,068 | 4,068 |

The pixel sizes are inferred from the layer factors, because the source file wasn't readable when
this was measured (see [Measurements](#measurements)).

## Why these subblocks are tiles today

[§3.5](../../spec/zarrify/czi.md#35-regular-levels) makes a layer a level only when its subblocks
are equal tiles on a lattice, and only the last column and row may be narrower. A layer that fails
goes to [tile arrays](../../spec/zarrify/czi.md#44-tiles). Slide scanners fail in two ways:

- **Pyramid layers of "minimal coverage."** ZEN clips these tiles around the scan region on any
  side. Most are 1024 × 1024 stored pixels, but others are as small as 1024 × 204. Every one of them
  does sit inside one cell of a lattice, and some start partway into the cell.
- **Layer 0.** Tiles are 1600 × 1200 at stage positions, about 10% overlapped, drifting a pixel or
  so per tile, so no lattice passes through their corners.

Both need something the layout can't say today: where in a chunk a subblock's pixels go.

## The constraint

A level's chunks have one codec chain, and the placement differs per chunk. It can't go in the
array's metadata, because [no document may grow with the number of
subblocks](../../spec/zarrify/czi.md#part-1-the-convention). It can go in the chunk's reference:
[spec/refkv.md](../../spec/refkv.md#45-where-bytes-that-no-source-holds-go-informative) says bytes
used by one value belong in a literal range in that value's reference, read only when that value
is. Readers already accept this, because a reference may be a `Concat` of ranges.

## The codec `refkv.placed`

An array-to-bytes codec that wraps another chain, as `sharding_indexed` does:

```json
{"name": "refkv.placed",
 "configuration": {"axes": [0, 1], "codecs": [{"name": "imagecodecs_jpegxr"}]}}
```

A chunk is a 32-byte header, then bytes the inner chain decodes:

| bytes | field | meaning |
|---|---|---|
| 0–7 | `stored_y`, `stored_x` | the shape the inner chain decodes to along `axes` |
| 8–15 | `src_y`, `src_x` | the first decoded pixel to keep |
| 16–23 | `size_y`, `size_x` | how many to keep |
| 24–31 | `dst_y`, `dst_x` | where they go in the chunk |

Each field is a `u32`, little-endian. Decoding fills a chunk with the fill value, decodes the inner
chain to the stored shape (other axes as in the chunk), and copies the kept pixels. A header whose
rectangle leaves the stored shape or the chunk is an error. `axes` are positions in the array as the
codec receives it, after any `transpose`.

The chunk's reference is `Concat[literal(header), source range]`, the source range being the one
[§7.5](../../spec/zarrify/czi.md#75-chunk-references) already gives. The codec never reads a second
key, knows nothing about CZI, and could serve any format with clipped tiles.

## Placed levels: pyramid layers

A layer that isn't regular becomes a **placed level** when all of these hold, `B` being its
subblocks:

1. All of `B` have the same codec form, and none of it needs row bands.
2. `Wl`, `Hl`, `W`, `H` and `F` are as in condition 2 of
   [§3.5](../../spec/zarrify/czi.md#35-regular-levels).
3. `x0` is the least X Start among subblocks of logical width `Wl`, and `y0` the least Y Start among
   those of logical height `Hl`. A subblock's cell is `i = ⌊(X − x0) / Wl⌋`, `j = ⌊(Y − y0) / Hl⌋`,
   and its inset is `u = X − x0 − i × Wl`, `v = Y − y0 − j × Hl`.
4. Every subblock stays inside its cell, `u + width ≤ Wl` and `v + height ≤ Hl` in logical pixels,
   and `u + F × W'`, with `W'` its stored width, likewise. `F` divides `u` and `v`.
5. No two subblocks of `B` have the same plane and cell.

The array has a regular chunk grid of `(H, W)`, and a shape of `m × W` by `r × H` from its least and
largest cells, up to one cell beyond the data. Its [origin and
translation](../../spec/zarrify/czi.md#43-image-arrays) start from `x0 + i_min × Wl`, in the same
global frame as every other level. Each chunk's header has `src = 0`, `size = stored`,
`dst = (v / F, u / F)`. No pixel is lost, so these subblocks are no longer tiles.

A layer that is regular stays regular and is written as today, without this codec.

## Cropped levels: layer 0

Tiles overlap, but each still holds a whole cell of a lattice whose step is a little under the
stage step. That gives a level with no blending: each chunk is one tile, cropped to its cell.

A layer `(1, 0)` that isn't regular becomes a **cropped level** when all of these hold:

1. All of `B` have the same codec form and the same logical and stored size `(W, H)`, and none needs
   row bands.
2. `Tx` is the largest integer with `W / 2 < Tx ≤ W` for which some `x0` in `[0, Tx)` puts a whole
   cell `[x0 + i × Tx, x0 + (i + 1) × Tx)` in every subblock; `x0` is the least such. `Ty` and `y0`
   likewise. Because `Tx > W / 2`, a subblock holds exactly one whole cell.
3. No two subblocks of `B` have the same plane and cell.
4. No hole: no cell in the level's extent is empty while a subblock covers its center.

Each array has a regular grid of `(Ty, Tx)`. Each chunk's header has `src` the cell's position in
the tile, `size = (Ty, Tx)`, `dst = 0`, `stored = (H, W)`. Finding the steps costs `O(W × (N + W))`.

Condition 4 is what makes the rule safe. Without it, the largest step can be far smaller than the
true one and leave most of the scan out: on `axioscan_bgr24_merged` it finds a step of 960 × 601 for
1600 × 1200 tiles, with 2,280 holes.

A layer that meets these conditions always becomes a cropped level; there is no option to turn it
off. Without one, a scan can't be seen at full resolution, and the tile arrays lose nothing.

This level is a choice of view, in three ways:

- **Each overlap shows one tile.** The lattice picks which one, and libCZI's own composite may pick
  the other. In the 24 pairs measured below, neighbors agree to within 1 pixel, so seams should be
  hard to see; a file with worse registration would show them.
- **The scan's outer edge loses a strip.** Margins that no neighbor shows fall outside every cell:
  at most 1.2% of the footprint in this slide's scenes, and 2.5% across the corpus.
- **Its subblocks stay tiles as well.** A cropped level hides pixels, so the tile arrays remain, and
  the two reference the same bytes. A reader that needs a margin, or wants its own stitching, still
  has every tile at its recorded position.

## What changes for the slide

Scene 0's image, by dataset:

| path | factor | shape (y, x) | chunks | kind |
|---|---|---|---|---|
| `0` | 1 | 30,296 × 60,564 | 540 | cropped |
| `1` | 2 | 15,360 × 30,720 | 245 | placed |
| `2` | 4 | 8,192 × 15,360 | 74 | placed |
| `3` | 8 | 4,096 × 8,192 | 23 | placed |
| `4` | 16 | 2,048 × 4,096 | 7 | placed |
| `5` | 64 | 475 × 948 | 1 | regular |

Factor 32 of this scene stays as 2 tiles: neither is a full-height tile, so the tallest of them sets
a cell they don't both fit. A viewer steps from factor 16 to 64 there, which costs little at that
size. Accepting a square cell would recover it, but adds a rule for one layer in 31.

## Cost

- **Store size.** 32 bytes and a little framing per chunk of these levels: about 6,500 chunks here,
  so about 300 KB. It's read only with its chunk, not at open.
- **Reads.** The same ranges as today, so one request per chunk and no change to coalescing. A
  cropped chunk decodes the whole tile, 1600 × 1200 for 1442 × 1082, about 1.23 times the pixels.
- **Readers.** A reader without `refkv.placed` can't open these levels. In a multiscale that means
  it can't open the dataset, where today it opens the one small array. zarr-python gets the codec
  from refkv's entry points, and the Neuroglancer fork needs a resolver and decoder beside
  `sharding_indexed`'s nesting. A third-party OME reader gets an error on them until the codec is
  registered.
- **Spec.** [§3.5](../../spec/zarrify/czi.md#35-regular-levels) gains both kinds and loses its note
  that minimal coverage gives tiles;
  [§4.3](../../spec/zarrify/czi.md#43-image-arrays),
  [§4.4](../../spec/zarrify/czi.md#44-tiles) and
  [§7.5](../../spec/zarrify/czi.md#75-chunk-references) gain their arrays and references, and
  [conventions §3](../../spec/conventions.md#3-arrays) the codec.

Existing regular levels don't change. Files with minimal-coverage pyramids or tile-scan layer 0s
do, and nothing needs to keep their old form, since
[version 0 promises nothing](../../spec/conventions.md#1-conventions).

## Alternatives

| alternative | why not |
|---|---|
| A table of placements in the codec's configuration | The document grows with subblocks, which the convention forbids |
| A rectilinear grid | Per axis, while the clipping varies per cell |
| Padding right and bottom clips only, using the coded size | Puts the 277 inset tiles in the wrong place, or leaves holes where they were |
| Widening the reference back to the subblock's copy of its entry, and parsing placement from it | Needs nothing in the store, and was the runner-up. But the codec has to learn CZI, each chunk reads `L + m` more bytes, and it can't express a crop, which is computed rather than recorded |
| A side array of placements | A codec can't read another key; readers would have to special-case the level |
| Per-tile Neuroglancer layers | Thousands of layers, with nothing to pick a scale from |
| Blending overlaps into new pixels | Copies data, which refkv doesn't do |
| The `switch` codec of [refkv#73](https://github.com/d-v-b/refkv/pull/73) | A superset that isn't merged. `refkv.placed` could later become one of its cases |

## Measurements

`design/experiments/czi_placement/measure.py` runs these over HTTP range requests. The slide's source
file wasn't readable (its recorded URL returns 404), so its numbers come from the tile records in
its store, and pixel checks ran on the corpus's `axioscan_bgr24_test`, a public slide from the same
kind of scanner.

**Which layers qualify.** Corpus files with neither kind of layer gain nothing, so aren't listed:

| file | pyramid layers: regular / placed / refused | tiles placed (inset) | layer 0: regular / cropped / refused |
|---|---|---|---|
| `same_czi_no_spaces` | 0 / 30 / 1 | 2,426 (277) | 0 / 7 / 0 |
| `axioscan_bgr24_test` | 7 / 19 / 3 | 905 (213) | 0 / 5 / 0 |
| `axioscan_bgr24_merged` | 0 / 0 / 5 | 0 | 0 / 0 / 1 |
| `axioscan_recognizedcode27` | 5 / 0 / 0 | 0 | 0 / 0 / 1 |
| `axioscan_background_subtraction`, `axioscan_deconvolution` | 4 / 0 / 1 | 0 | 0 / 0 / 1 |
| `zen395_offline` | 4 / 0 / 1 | 0 | 0 / 1 / 0 |
| `test_gray_tiles` | 0 / 0 / 0 | 0 | 0 / 0 / 1 |

The refused pyramid layers are small top layers: a fractional offset (`axioscan_bgr24_test`), a
logical size that isn't a multiple of the stored one (`32751 / 1024`, which condition 2 of
[§3.5](../../spec/zarrify/czi.md#35-regular-levels) already refuses), or tiles crossing cell
boundaries (the merged file, in all five layers). They stay tiles, as today. So do the refused layer
0s, for holes or a shared cell. The test-camera files and `zenblack_3d` also gain cropped levels,
at 4 to 45 tiles each.

**Where pixels land.** Ten inset factor-2 tiles of `axioscan_bgr24_test`, compared against the
layer-0 mosaic beneath them, minified. Nine align best within one full-resolution pixel of the
placement, one of those having almost no texture, so it says little. The tenth aligns best 5 pixels
away in y, with a low error at the placement either way (MSE 1.5, against a variance of 95). Most
of the nine prefer a shift of −1, half a stored pixel, which suggests how ZEN minifies rather than
anything about placement. An error in the rule would be off by hundreds of pixels, and would show
as a large error at the placement.

**How neighbors agree.** 24 overlapping layer-0 pairs of the same file, at their recorded positions:
median MSE 1.4 against a median variance of 1,254, and the best alignment is at the recorded position
in 15 pairs and 1 pixel away in the other 9.

Not measured: a rendered image in Neuroglancer, and any file from another scanner with minimal
coverage. The check against libCZI below would be the first to compare a whole level.

## Implementation

1. **Codec.** `python/src/refkv/zarr/codecs/placed.py` with its entry point, its TypeScript twin, the
   demo's codec list, and a Neuroglancer fork resolver and decoder.
2. **Placed levels.** Classification and references in `rust/refkv-ir/src/czi.rs` and
   `src/project/czi.rs`, with the changed files recorded in `conformance/zarrify/revised.json`, since
   the conformance reference is frozen.
3. **Cropped levels.** The same files, in a second PR, so that each can be reviewed on its own.

[refkv#241](https://github.com/d-v-b/refkv/pull/241) and
[refkv#251](https://github.com/d-v-b/refkv/pull/251) change the same profile, so these follow them.

**Tests**, by the house rule:

- One test over the accepted fixtures, `czi_placed_minimal_coverage` and `czi_cropped_overlap` among
  them.
- One test per refusal: `straddles`, `fractional_inset`, `shared_cell`, `mixed_codec_form`, and for
  cropped levels `holes` and `mixed_sizes`.
- One codec test over its decodings, and one per malformed header.
- `js/test/czi/verify.py` checks every level chunk against czifile's subblock, and a cropped level
  against pylibCZIrw outside its overlaps.

## Open question

**Where should the codec be registered?** `refkv.placed` is generic enough for zarr-extensions, and
that's what a third-party reader would need.
