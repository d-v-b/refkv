# Proposal: JPEG 2000 resolutions as multiscale levels

A JPEG 2000 codestream holds its lower resolutions already: a tile with `N` decomposition levels
decodes at `N + 1` sizes, and in a resolution-major stream each size's bytes are a prefix of the next
size's. This note proposes presenting those sizes as the levels of a multiscale image, with no
re-encoding and no new codec: each chunk of a lower level is a valid codestream, made of a few literal
header bytes and byte ranges of the file.

The technique comes from Mark Kittisopikul's
[zarr-virtual-pyramid-jp2k](https://github.com/mkitti/zarr-virtual-pyramid-jp2k), which showed it with
refkv's references on a 16-plane crop of `jrc_hela-2`. This note takes its construction, changes how
tiles are placed, and asks what real files need. It is a design note, not normative. The prototype is
[`design/experiments/jpeg2000_levels/measure.py`](../experiments/jpeg2000_levels/measure.py), and
[Checked by a prototype](#checked-by-a-prototype) gives its results.

The proposal, in short:

- A level's chunk keeps the tile where the file's reference grid puts it, scaled. This works for every
  tile size. Moving the tile to the origin does not.
- Packets are found in one of three ways, cheapest first: one tile-part a resolution, PLT markers, or
  a walk of the packet headers. Real files need all three.
- [Sentinel-2](#sentinel-2-the-first-target) comes first, since every band file sampled has PLT.
- [Slices in a zip](#slices-in-a-zip-the-human-organ-atlas), as the Human Organ Atlas publishes, come
  next, and need the walk.
- Quality layers and sub-bands are [left out](#left-out-quality-layers-and-sub-bands): they over-read,
  and they discard data without saying which data matters.

## The construction

The chunk of tile `t` at `n` levels below the file's, with the tile's area `[x0, x1) × [y0, y1)`, is
the concatenation of:

| part | value |
|---|---|
| SOC and SIZ | a literal, with the fields below |
| the main header's rest | a data source shared by the level's chunks: COD with its levels `N − n` and `N − n + 1` precinct sizes, QCD with its first `1 + 3(N − n)` bands, COC and QCC likewise, and comments; TLM and PLM left out |
| SOT and SOD | a literal: one tile-part, `Isot = 0`, its `Psot` the kept packets' length plus 14 |
| the packets | source ranges of the packets of resolutions `0 … N − n`, without the tile-part headers between them |
| empty tiles | as [`SafeChunk`](../../spec/zarrify/safe.md#86-chunks)'s tail, with `N − n` levels, when the tile is short of its cell of the chunk grid |
| EOC | a literal |

SIZ takes `a0 = ceil(x0 / 2^n)`, `a1 = ceil(x1 / 2^n)`, `b0 = ceil(y0 / 2^n)` and `b1 = ceil(y1 / 2^n)`:

| field | value |
|---|---|
| `Rsiz` | the file's, less every bit but bit 14 |
| `XOsiz`, `XTOsiz` | `a0` |
| `YOsiz`, `YTOsiz` | `b0` |
| `XTsiz` | `a1 − a0` |
| `YTsiz` | `b1 − b0` |
| `Xsiz`, `Ysiz` | the cell's far edges at this level |

Bit 14 of `Rsiz` says the stream is HTJ2K, and a decoder rejects an HT stream without it (OpenJPH does).
[`SafeChunk`](../../spec/zarrify/safe.md#86-chunks) sets `Rsiz` to 0, and rejects the CAP marker, since
Sentinel-2 is not HT; a general profile keeps both.

### Why the grid must be kept

The packets are kept byte for byte, so the new stream must give each band, precinct and code-block
the geometry the encoder gave it. It does, for every tile origin. A band of the file at decomposition
level `b` covers `ceil((x0 − 2^(b−1) o) / 2^b)` onward, `o` 0 or 1 by the band's orientation. At `n`
levels down, with `b' = b − n` and `a0 = ceil(x0 / 2^n)`:

```
ceil((a0 − 2^(b'−1) o) / 2^b') = ceil((x0 − 2^(b−1) o) / 2^b)
```

since `ceil(ceil(u / p) / q) = ceil(u / (p q))`, and subtracting an integer passes through the inner
`ceil`. So the band's coordinates do not change. The precinct and code-block partitions are anchored at
0, so they do not change either, and the transform's parity at the tile's edge, which depends on
whether its first coordinate is odd, is kept too.

Moving a tile to the origin, as the source project does, needs the tile's origin to be a
multiple of `2^N` for this to hold. The results below show the failure at tiles of 100 and 99: wrong
samples, or a decoder's refusal. Tiles of 256 at `N = 3`, the size that project tested, pass either way.

### Quantization

An expounded QCD (style 0 or 2) loses its last bands. A scalar derived one (style 1) needs no change:
ISO/IEC 15444-1 E.5 gives `ε_b = ε_0 − N_L + n_b`, and cutting `n` levels takes `n` from both `N_L` and
`n_b`. The source project rejects this form as dependent on the level count; by this formula it isn't.
No file or encoder at hand writes it, so this is not tested, and a first version can reject it.

### What a level is

A level is what a decoder gives for the codestream, which is the file's own lower resolution: for
reversible files the 5/3 low-pass, for others the 9/7. It is not a function of level 0's samples.
In one Sentinel-2 tile with 44,499 samples at the ends of the range, the level differs from a 5/3
transform of the decoded tile in 182 samples at `n = 1`, which is what a decoder clipping level 0 would
cause, though that cause is not proven. Two tiles with no such samples have no such differences. The level
equals OpenJPEG's reduced decode in every sample of all of them.

A level is also not a mean. In plane 0 of the `jrc_hela-2` crop, `s3` differs from an 8 × 8 box mean by
6.5% of the plane's range, and its standard deviation is 38% higher, so measurements on it differ
from those on a mean pyramid. The level's metadata should say how it was made
([Layout](#layout)).

## Finding the packets

The level's ranges need the length of each packet of the kept resolutions. The order must be
resolution-major: RLCP, RPCL, or LRCP with one layer, which is the same order. PCRL and CPRL interleave
the resolutions, and LRCP with several layers does too; a file in one of those has no such levels.

| way | what is read | when it applies |
|---|---|---|
| one tile-part a resolution | the SOT chain, which a profile reads anyway | `N + 1` tile-parts a tile, as OpenJPEG's `-TP R` and OpenJPH's `-tileparts R` write |
| PLT | each tile-part's header, through SOD | PLT present; its first packets' lengths are summed, and their count comes from [`SafeChunk`](../../spec/zarrify/safe.md#86-chunks)'s `nprec` |
| walking the packet headers | each kept packet's header, skipping its body by the lengths it gives | neither: needs tag trees and the geometry above |

What real files have, read by range requests:

| source | tiles | `N` | order, layers | transform, QCD | how packets are found |
|---|---|---|---|---|---|
| Sentinel-2 L1C and L2A, 2016 to 2025 processing, 4 files | 1024, precincts 256 | 4 | LRCP, 1 | 5/3, style 0 | PLT |
| Aperio JPEG 2000 slides (`CMU-1-JP2K-33005`, `JP2K-33003-1`, `-2`) | 240 or 256, a codestream each | 5 | LRCP, 1 | 9/7, style 2 | walk, RGB |
| Human Organ Atlas slices, OpenJPEG 2.5.2 | one tile of 2516 | 8 | LRCP, 1 | 9/7, style 2 | walk |
| the source project's encodes | none or 256 | 3 | RPCL, 1 or 4 | 5/3 | PLT (ndic), or one tile-part a resolution (OpenJPH, OpenJPEG) |

Every real file sampled has one layer and a resolution-major order, so the rejections above should be
rare. A sample this small does not show that of a whole archive.

A walk keeps only lengths. It does not rewrite a packet, so the limits the source project reports on
HT code-blocks with refinement passes apply to the walk alone. The first two ways do not depend on
them. A profile should check that the walk ends exactly at the tile-part's end, which the prototype's
does on both slices tried.

## Layout

**Levels.** Level 0 is the chunk the format's profile writes now. Levels `n = 1 … N_min` are written
until the array's longest side is at most 256, `N_min` the least `N` of any component and tile, so that
the set depends only on the bytes. Without a stop, the eight levels of a 2516-sample slice would reach
10 samples.

**Chunk grid.** A level's chunk is its tile's area. The grid is regular when `2^n` divides the tile size,
and otherwise is rectilinear, since tile `u` covers `[ceil(u T / 2^n), ceil((u + 1) T / 2^n))`. So
Sentinel-2's 1024 and a single-tile image are regular at every level, and Aperio's 240 only up to
`n = 4`. Tiles that are independent codestreams, as TIFF's and DICOM's are, are a stricter case: each
starts at its own origin, so their areas do not join unless `2^n` divides the tile size. For these,
levels stop there.

**Position.** A low-pass sample `k` at `n` levels down sits at the file's sample `2^n k`, not at the
center of a block. OME-NGFF's scale-only transform ([conventions §4](../../spec/conventions.md#4-images))
already says this: `k × 2^n × s` is the file's sample `2^n k`, so the levels need no translation of
their own, and a format's [translation](../../spec/conventions.md#5-units) stays the same at every level.
SAFE's `spatial:transform` is by pixel corner, so a level's origin moves. With resolution `s` and level 0's
corner `c`, level `n` has `s' = 2^n s` and `c' = c − s (2^n − 1) / 2`, in the x axis's direction and
likewise in y, so that each sample's center is right.

**How it was made.** The zarr-conventions `multiscales` item has `derived_from` and `transform`, and
OME-NGFF's `multiscales` has `type` and `metadata`. The level should name its method, such as
`jpeg2000 5/3 low-pass` or `jpeg2000 9/7 low-pass`, wherever the format's convention allows. Each
convention's document says which member.

**Keys.** An array that gains levels has to become a group, which changes keys such as SAFE's
`r10m/b02/c/0/0` to `r10m/b02/0/c/0/0`. refkv has no users to break, so the proposal takes this. A format
with its own levels keeps them, and gains none here: Aperio has steps of 4, and DICOM's slides have
theirs ([Other formats](#other-formats)).

## Sentinel-2: the first target

[spec/zarrify/safe.md](../../spec/zarrify/safe.md#5-georeferencing-geozarr) says the resolution levels
inside each band file are "not presented as multiscales levels" in this version. This is the missing
step, and the profile has most of what it needs: tiles kept on the grid, `nprec`, the tail, and shared
main-header data sources. It needs COD and QCD cut per level, and the PLT lengths.

The cost, for one 10 m band file (`T10SEG`, 2024, `B02`):

| | |
|---|---|
| tiles | 121 |
| tile-part headers to read | 6,140 bytes, about 50 a tile, where the profile reads 12 now |
| codestream | 22,418,078 bytes |
| a level's share of a tile's bytes, `n = 1 … 4` | 27–29%, 7.6–8.2%, 2.2–2.3%, 0.7% |
| a chunk's literal bytes | 59, plus 134 of main-header rest shared by the level, plus an edge tile's tail |

A Level-1C product has about 3100 tiles, so four more levels add about 12,400 chunks. At the
roughly 520 bytes a chunk that the demo archives of the source project cost, that is about 6 MB in a vzip
sidecar, against about 1.6 MB now. This is an estimate: no such sidecar has been built.

A viewer gets the most from this: a 10 m band at `n = 4` needs 0.7% of the file, and there are no overview
files to fetch.

## Slices in a zip: the Human Organ Atlas

The [Human Organ Atlas](https://human-organ-atlas.esrf.fr) publishes hierarchical phase-contrast
tomography, such as [the heart of LADAF-2021-17 at 25.08 µm](https://human-organ-atlas.esrf.fr/datasets/1773966096),
in two forms.

**The N5 pyramid.** A public bucket holds `n5://gs://ucl-hip-ct-35a68e99feaae8932b1d44da0358940b/LADAF-2021-17/heart/25.08um_complete-organ_bm05/`,
with `uint16` blocks of 128³ in Blosc with zstd and bit shuffle, at five levels halved in all three axes.
refkv's [N5 profile](../../spec/zarrify/n5.md) has the rows for this, so these volumes should need no new
work, and this proposal does not apply to them. It has not been run on this store.

**The JPEG 2000 slices.** The portal offers a zip of `.jp2` slices. The one examined,
`50.16um_LADAF-2021-17_heart_complete-organ_pag-0.10_0.03_jp2_.zip`, has these properties:

| | |
|---|---|
| size | 4,081,963,319 bytes, ZIP64 |
| entries | 3224 slices, all stored (method 0), so each `.jp2` is a contiguous range of the zip |
| a slice | 2516 × 2516 `uint16`, one tile, `N = 8`, LRCP, 1 layer, 9/7, style 2 QCD, no precincts, no PLT |
| a slice's size | 1,265,042 to 1,266,068 bytes, rate-controlled to a ratio of 10 |
| central directory | 382,734 bytes |

The file is exactly the case this proposal is for: the [ratio of 10](https://www.biorxiv.org/content/10.1101/2025.07.31.667856.full.pdf)
is lossy, the slices are the archive's own bytes, and refkv's [stack](../../docs/tiff-stack.md) and
[store operators](store-operators.md) already handle a stack of files. It would need a `.jp2` profile, read
from a zip's members as [SAFE](../../spec/zarrify/safe.md#88-the-source-table) reads its own, and the walk.

Its packets, in slice 1612 (offset, header length, body length):

| resolution | offset | header | body | a level's share |
|---|---|---:|---:|---:|
| 0 | 167 | 4 | 167 | `n = 8`: 0.01% |
| 4 | 5,361 | 42 | 10,407 | `n = 4`: 1.24% |
| 5 | 15,810 | 97 | 28,609 | `n = 3`: 3.50% |
| 6 | 44,516 | 266 | 85,254 | `n = 2`: 10.26% |
| 7 | 130,036 | 991 | 296,729 | `n = 1`: 33.78% |
| 8 | 427,756 | 3,405 | 834,720 | level 0 |

Slice 2, at the top of the volume, has a similar shape, with shares of 31.24%, 8.83%, 2.72% and 0.94%. The
headers are 4,840 bytes of the slice, and levels 1 to 4 need those of resolutions 0 to 7. The first seven
are inside its first 45 KB, so a zarrifier reads about 45 KB and one short range a slice, about 150 MB
and 6,500 requests for the volume, of 4 GB. Levels 1 to 4 are 3224 × 4 = 12,896 more chunks.

**What it does not give.** The slices are one tile with no precincts, and a Zarr chunk is decoded whole,
so:

- Any crop of level 0 reads and decodes the whole slice, 1.27 MB and 6.3 million samples, where the N5
  pyramid reads a few blocks.
- Nothing is halved in `z`. A level of 315 × 315 is still 3224 slices. An `xz` section needs one request a
  slice, where the N5 store's level 4 needs 12 blocks.

So levels over slices are a light browsing layer for the archive's own bytes, not a replacement for a
pyramid of 3D blocks. Taking every `2^n`-th slice at level `n` closes much of the gap:

| | size | bytes |
|---|---|---:|
| N5 level 3 | 628 × 628 × 806 | 321 MB |
| slices at `n = 2`, every 4th (estimate, from slice 1612) | 629 × 629 × 806 | 105 MB |
| N5 level 4 | 314 × 314 × 403 | 44 MB |
| slices at `n = 3`, every slice (estimate) | 315 × 315 × 3224 | 143 MB |
| slices at `n = 3`, every 8th (estimate) | 315 × 315 × 403 | 18 MB |

These are not the same data. The N5 blocks are compressed without loss of whatever they hold, the slices
are lossy at a ratio of 10, and a slice taken without averaging aliases in `z` where the `xy` low-pass does
not. Like the `xy` levels, every `2^n`-th slice sits at the file's sample `2^n k`, so the scale-only transform
is right. The stack operator could take this as an option, with every slice as the default
([Open questions](#open-questions)).

**The portal is not a source.** The link examined carries a session in its URL, so a reference to it stops
working. Its range support also does not meet [spec/refkv.md §5.2](../../spec/refkv.md#52-reading-over-http):
`bytes=0-99` gets a `200` and the whole file, and `bytes=100000-100099` gets a `206` through the end of the
file. It delivered about 70 KB/s. A reference store over these slices needs a stable host that honors ranges.
The offsets can be computed now and the host given afterward with `--url`.

## Other formats

| format | what it has | proposal |
|---|---|---|
| TIFF, compressions 33003, 33004, 33005, 34712 | a codestream a tile; Aperio's have `N = 5`, no PLT, RGB | later: the walk reads about the whole file, and a slide has levels already, in steps of 4. A level between them is of modest use |
| DICOM JPEG 2000 and HTJ2K frames | a codestream a frame | not sampled. A slide has its own levels; a volume of frames could gain them as a stack does |
| plain `.j2c` and `.jp2` files | the source project's and the Human Organ Atlas's | a new file profile, with the stack and zip reading above |
| GRIB template 5.40 | decoded to values through its own codec | none: its codestreams are decoded to values, and are not presented as chunks |

HTJ2K needs one more thing in the browser: the fork's `imagecodecs_jpeg2k` has no HT counterpart yet
([d-v-b/refkv#223](https://github.com/d-v-b/refkv/issues/223)), and levels have not been tried with that
fork's decoder.

## Left out: quality layers and sub-bands

The source project also presents the first `K` quality layers, and drops sub-bands by rewriting packet
headers. Both work, and this proposal leaves both out.

They over-read. A layer subset in RPCL is scattered through the file, and refkv's reader merges ranges up
to 64 KiB apart. At `s0` of plane 0 of its `hela2_quality` archive, the reader fetched:

| layers | bytes needed | bytes read |
|---|---:|---:|
| 1 | 16,078 | 36,141 |
| 2 | 32,524 | 115,796 |
| 3 | 65,165 | 145,126 |

They also need a judgment that no file records. A resolution is the file's own representation, while a
quality group or a dropped band is a choice of what to discard, and it belongs to the caller, built with
the [store operators](store-operators.md) from a parser, if anyone needs it. Every file sampled above has one
layer anyway.

## Checked by a prototype

`measure.py` builds each stream as above and decodes it with imagecodecs. Tools come from
`pixi exec --spec openjpeg --spec openjph`.

**Tiles, against the encoders' own reduced decoders** (`opj_decompress -r`, `ojph_expand -skip_res`), a
synthetic 512 × 512 `uint16`, `N = 3`, every tile of every level, stitched:

| tile | encoder | grid kept, `n = 1, 2, 3` | moved to the origin, `n = 1, 2, 3` |
|---|---|---|---|
| 512, 256 | OpenJPEG, one tile-part a resolution; OpenJPEG, PLT; OpenJPEG, 9/7 at a ratio of 10, PLT; OpenJPH | equal, equal, equal | equal, equal, equal |
| 100, 99 | OpenJPEG, one tile-part a resolution | equal, equal, equal | wrong, wrong, equal |
| 100, 99 | OpenJPEG, PLT | equal, equal, equal | wrong, wrong, equal |
| 100, 99 | OpenJPEG, 9/7 at a ratio of 10, PLT | equal, equal, equal | wrong, wrong, equal |
| 100, 99 | OpenJPH | equal, equal, equal | decoder error, wrong, equal |

(At `n = 3`, these streams hold only the LL band, whose geometry has nothing to get wrong.)

**Real files, against OpenJPEG's reduced decode of the whole tile:**

| file | case | levels | result |
|---|---|---|---|
| Sentinel-2 `T10SEG` `B02`, 2024 | tiles at (0, 0) and (1024, 0) | 1–4 | equal |
| | tile 10: 740 wide, on the right edge, with a tail, nearly empty | 1–4 | equal |
| | tile 110: 740 high, on the bottom edge, with a tail, with data | 1–4 | equal |
| Human Organ Atlas, the zip above | slices 2 and 1612, by the walk, which ends at the tile-part's end | 1–8 | equal |

**Reads of the source project's archives over HTTP**, one plane of `hela2_pyramid_ojph`, with
zarr-python: one request a level, 410,658, 107,974, 28,377 and 7,532 bytes, and `s0` equal to the reference
data. `s1` to `s3` also equal an independent 5/3 transform of that plane.

**Not checked:**

- A tile on the right edge with data under a tail, since the one tried is nearly empty.
- A style 1 QCD, COC or QCC, several components, and SOP and EPH markers.
- Any browser decoder, and any implementation in Rust or a reference store built this way for these files.
- The N5 profile on the Human Organ Atlas store.
- Slices other than these two, and any Human Organ Atlas zip other than this one.

## Plan

1. **Sentinel-2.** Add levels to the SAFE profile, from PLT, with the key change and the `spatial:transform`
   rule. Its fixtures need PLT lengths that match their packets. The fork needs a re-vendor.
2. **A `.jp2` file profile and the walk,** then slices in a stored zip, through the stack operator. The
   walk is in the Rust core, so that every host has it.
3. **Others, if wanted.** TIFF and DICOM frames, and HTJ2K in the fork.

## Open questions

1. **Key change.** Making a band a group breaks SAFE's keys, and moves it away from a shape that tools that
   read a band as an array expect. The alternative is a sibling group of levels, which keeps level 0's key.
   The proposal takes the group, since refkv has no users yet.
2. **Stop rule.** A longest side of 256 is arbitrary. It gives Sentinel-2 all 4 levels and the 2516-sample slices
   4 of 8. Should this be a function of the bytes alone, as here, or something a caller can choose?
3. **Slices in `z`.** Taking every `2^n`-th slice makes levels nearer to isotropic, and saves 8 times the bytes
   at `n = 3`, at the cost of aliasing in `z`. It belongs in the stack operator rather than the profile. Should it
   be offered, and should it be the default?
4. **Sharing the codestream's parts.** The main-header rest is shared by a level. The packets of level `n` are also
   a prefix of those of level `n − 1`, so a byte-range cache could reuse them when a viewer zooms in. Neither
   refkv's reader nor an HTTP cache keyed by exact range does so now, and this has not been measured.
