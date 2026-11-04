# Neuroglancer precomputed

The Zarr v3 layout of a Neuroglancer precomputed volume, and its `info` document. What all of
refkv's conventions share is in [spec/conventions.md](../conventions.md), cited here as
"conventions §n". How refkv produces this layout as a reference store from a listed store is
the precomputed profile, [Part 2](#part-2-the-profile) below. The format is the one Neuroglancer's
[precomputed documentation](https://github.com/google/neuroglancer/tree/master/src/datasource/precomputed)
defines, cited here as "the format", with its
[sharded format](https://github.com/google/neuroglancer/blob/master/src/datasource/precomputed/sharded.md)
and its
[compressed segmentation format](https://github.com/google/neuroglancer/blob/master/src/sliceview/compressed_segmentation/README.md).

Convention version: 0 (until release, [conventions §1](../conventions.md#1-conventions)) · UUID: `c499987a-9518-4b2b-a804-a70a8de1a911` ·
Schema: [schema.json](precomputed/schema.json)

Changed 2026-11-04: scales of the encodings `compresso`, `crackle`, `jxl`, `fpzip`, `zfpc` and
`kempressed` are presented, where they were kept and not presented, each for the data types and
channels its format holds ([§3.3](#33-scales-that-are-not-presented), [§6.2](#62-decoding),
[§6.3](#63-content-codings)); a chunk of another data type than the array's is an error.

Added 2026-11-03.

This document has two parts ([conventions §11.1](../conventions.md#111-parts)).

# Part 1. The convention

This convention gives a layout only to the volumes that meet its requirements. Where it says that
"the input is rejected", a store that fails has no layout under this convention, and the profile
rejects it. A scale it cannot present does not reject the store: the scale is recorded, with its
reason, and its objects are kept ([§3.3](#33-scales-that-are-not-presented)).

A precomputed volume is a store with an `info` document at its root and one directory per
**scale**. Each scale is a 3-D grid of **chunks** along x, y and z, with channels as a fourth
dimension inside each chunk. A scale stores its chunks in one of two ways:

- **unsharded**: one object per chunk, named for the voxels it holds;
- **sharded**: many chunks in one **shard** object, found through the shard's own two-level index.

Connectomics volumes hold very many chunks. BANC has 690 million, 518 million of them in its finest
scale, so a layout with a reference per chunk would be a store of billions of bytes that takes
hundreds of thousands of requests to build. This layout has one reference per **object** instead, and reads a shard's index
when a chunk is read, as Neuroglancer does. A shard is one Zarr chunk, read by the codec
`refkv.precomputed` ([§6](#6-the-codec-refkvprecomputed)). The output comes from `info` and the
store's listing alone: the profile reads no chunk and no shard.

## Layout

The hierarchy's entries ([conventions §11.5](../conventions.md#115-layout-tables)):

| prefix | type | structure | description |
|---|---|---|---|
| `zarr.json` | group | [`PrecomputedRoot`](#type-precomputedroot) | the root |
| `zarr.json#/attributes/ome` | attribute | [`PrecomputedOme`](#type-precomputedome) | the presented scales as one image ([§5](#5-the-image)), when any is presented |
| `zarr.json#/attributes/zarr_conventions` | attribute | array of [`ConventionMetadata`](../conventions.md#type-conventionmetadata) | `[C]`, `C` the CMO of [§1](#1-declaration) |
| `zarr.json#/attributes/refkv_zarrified` | attribute | [`RootProperty`](../conventions.md#type-rootproperty) | `profile` `"precomputed"`, `version` `0`, `source.url` the store's URL, ending in `/` |
| `zarr.json#/attributes/refkv_zarrified/precomputed` | attribute | [`PrecomputedInfo`](#type-precomputedinfo) | the root's source metadata ([§7](#7-source-metadata)) |
| `<n>/zarr.json` | array | [`PrecomputedArray`](#type-precomputedarray) | presented scale `n` ([§3](#3-scales)) |
| `<n>/zarr.json#/attributes/refkv_zarrified/precomputed` | attribute | [`PrecomputedScale`](#type-precomputedscale) | the scale's source metadata ([§7](#7-source-metadata)) |
| `<n>/c/<coords>` | chunk | [`PrecomputedChunk`](#type-precomputedchunk) | each chunk object ([§3.2](#32-chunk-objects)) or shard ([§4.3](#43-shards-as-zarr-chunks)) of the scale |
| `refkv_source/**` | subtree | [the mirror's entries](../conventions.md#table-mirror) | the store's IR mirror ([§7.1](#71-the-ir-mirror)): every entry under `refkv_source/` but those under `refkv_source/objects/` |
| `refkv_source/zarr.json#/attributes/refkv_zarrified/precomputed` | attribute | [`PrecomputedMirror`](#type-precomputedmirror) | `refkv_source`'s source metadata ([§7.1](#71-the-ir-mirror)) |
| `refkv_source/objects/<k>` | object | whole object | each other object ([§8](#8-other-objects)) |

| placeholder | value |
|---|---|
| `<n>` | a presented scale's place in the image, `0`, `1`, ..., full resolution first ([§5](#5-the-image)) |
| `<coords>` | the Zarr chunk's indexes: `<z>/<y>/<x>`, or `0/<z>/<y>/<x>` with more than one channel, each in decimal without leading zeros; it stands for 3 or 4 segments |
| `<k>` | the other object's key, its last segment escaped ([§8](#8-other-objects)); it stands for one or more segments |

## 1. Declaration

The root and `refkv_source` declare the convention
([conventions §11.2](../conventions.md#112-declaration)), the root with its source metadata
([§7](#7-source-metadata)) as the member `"precomputed"`. Each array declares it with
`{"precomputed": S}`. Its CMO is:

```json
{
  "uuid": "c499987a-9518-4b2b-a804-a70a8de1a911",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/zarrify/precomputed/schema.json",
  "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/zarrify/precomputed.md",
  "name": "refkv_zarrified",
  "description": "The Zarr layout of a Neuroglancer precomputed source zarrified by refkv, and the source's metadata"
}
```

## 2. The info document

The store's object `info` is read as [spec/zarrify.md §1.6](../zarrify.md#16-json-documents)
says. These members hold for every volume, or the input is rejected:

| member | type | requirement |
|---|---|---|
| `@type` | string | absent, or `"neuroglancer_multiscale_volume"`. A store of meshes, skeletons or annotations has another `@type` and no volume to present |
| `type` | string | any string; `"image"` adds the image's channel windows ([§5](#5-the-image)), and `"segmentation"` is the other value real volumes use |
| `data_type` | string | one of `uint8`, `int8`, `uint16`, `int16`, `uint32`, `int32`, `uint64`, `int64`, `float32`, `float64`, the Zarr `data_type` of the same name |
| `num_channels` | integer | from 1 to 65535 |
| `scales` | array | not empty |

Every other member (`mesh`, `skeletons`, `segment_properties` and those a writer adds) is kept in
the source metadata ([§7](#7-source-metadata)).

## 3. Scales

Each element of `scales` is a **scale**. It is **presented** as an array when all of the
conditions of [§3.3](#33-scales-that-are-not-presented) hold. Its members:

| member | type | meaning |
|---|---|---|
| `key` | string | the scale's directory in the store, a path of one or more segments |
| `size` | 3 integers | the volume's size in voxels along x, y and z |
| `resolution` | 3 numbers | a voxel's size in nanometers along x, y and z |
| `voxel_offset` | 3 integers | the coordinates of the volume's first voxel; 0 when absent |
| `chunk_sizes` | array of 3 integers | the chunk sizes; the first is the one the layout uses. The format lets a scale be read in any of them, each holding the same voxels |
| `encoding` | string | how a chunk's voxels are coded: `raw`, `jpeg`, `png`, `compressed_segmentation`, `compresso`, `crackle`, `jxl`, `fpzip`, `zfpc` or `kempressed` are presented |
| `compressed_segmentation_block_size` | 3 integers | that encoding's block size |
| `sharding` | object | the shards ([§4](#4-shards)); absent for an unsharded scale |

### 3.1 The array

With `(sx, sy, sz)` the `size`, `(cx, cy, cz)` the first chunk size, `C` the `num_channels` and
`(kx, ky, kz)` the chunks a Zarr chunk holds (1 each for an unsharded scale, its box for a sharded
one, [§4.2](#42-a-shard-is-a-box-of-chunks)), the array's axes run from the slowest to the fastest
as the format stores them, so that no codec has to transpose a raw chunk. The channel axis is left
out of a volume of one channel.

<a id="type-precomputedarray"></a>**`PrecomputedArray`**, an [`ArrayDocument`](../conventions.md#type-arraydocument)
with:

| member | value |
|---|---|
| `shape` | `[sz, sy, sx]`, or `[C, sz, sy, sx]` when `C > 1` |
| `data_type` | `info`'s `data_type` |
| `chunk_grid`'s chunk shape | `[kz × cz, ky × cy, kx × cx]`, with `C` first when `C > 1` |
| `fill_value` | `0`, what the format's readers give for a missing chunk |
| `codecs` | `[{"name": "refkv.precomputed", "configuration": X}]`, `X` a [`PrecomputedCodecConfiguration`](#type-precomputedcodecconfiguration) |
| `dimension_names` | `["z", "y", "x"]`, or `["c", "z", "y", "x"]` when `C > 1` |
| `attributes` | a [`Declaration`](../conventions.md#type-declaration), its source metadata a [`PrecomputedScale`](#type-precomputedscale) |

Index `i` of an axis is the voxel `voxel_offset + i`: the grid of chunks starts at `voxel_offset`.

### 3.2 Chunk objects

An unsharded scale's chunk at position `(i, j, k)` on the grid is the object at
`<key>/<x0>-<x1>_<y0>-<y1>_<z0>-<z1>`, where `x0 = voxel_offset_x + i × cx`, and `x1` is the lesser
of `x0 + cx` and `voxel_offset_x + sx`, likewise for y and z. Each bound is a decimal without
leading zeros, with a leading `-` for a negative value. The name may end in a suffix that names its
content coding, `.gz`, `.br` or `.zstd`, which [§6.3](#63-content-codings) does not rely on. A name
whose bounds are not exactly those of a chunk of the grid is not a chunk object: it is an other
object ([§8](#8-other-objects)), as are the objects of a second chunk size. When two objects name
one chunk (`x` and `x.gz`), the first by its key's UTF-8 bytes is the chunk, and the second is an
other object. An object of size 0 is not used ([spec/zarrify.md §1.4](../zarrify.md#14-store-inputs)),
so that chunk reads as the fill value.

### 3.3 Scales that are not presented

A scale that fails one of these, tested in order, has no array. It is recorded, with the first
reason that applied, in [`PrecomputedInfo`](#type-precomputedinfo)'s `not_presented`, and its
objects are other objects, referenced whole.

| condition | reason recorded |
|---|---|
| it is an object | `the scale is not an object` |
| `key` is a string | `its key is not a string` |
| `size` is 3 integers from 1 to 2^53 − 1 | `its size is not 3 positive integers` |
| `resolution` is 3 positive numbers | `its resolution is not 3 positive numbers` |
| `chunk_sizes` is a nonempty array | `its chunk_sizes is not a nonempty array` |
| its first element is 3 integers from 1 to 2^32 − 1 | `its first chunk size is not 3 integers from 1 to 4294967295` |
| `voxel_offset` is absent, or 3 integers | `its voxel_offset is not 3 integers` |
| `encoding` is a string | `its encoding is not a string` |
| `encoding` is one the codec decodes | `its encoding E has no decoder` |
| `jpeg` holds `uint8`, in 1 or 3 channels | `jpeg holds uint8 in 1 or 3 channels, not T in N` |
| `png` holds `uint8` or `uint16`, in at most 4 channels | `png holds uint8 or uint16 in at most 4 channels, not T in N` |
| `compressed_segmentation` holds `uint32` or `uint64` | `compressed_segmentation holds uint32 or uint64, not T` |
| `compressed_segmentation_block_size` is 3 positive integers, for that encoding | `its compressed_segmentation_block_size is not 3 positive integers` |
| `compresso` holds `uint8`, `uint16`, `uint32` or `uint64`, in 1 channel | `compresso holds uint8 to uint64 in 1 channel, not T in N` |
| `crackle` holds `uint8`, `uint16`, `uint32` or `uint64`, in 1 channel | `crackle holds uint8 to uint64 in 1 channel, not T in N` |
| `jxl` holds `uint8`, in 1 channel | `jxl holds uint8 in 1 channel, not T in N` |
| `fpzip` holds `float32` or `float64` | `fpzip holds float32 or float64, not T` |
| `kempressed` holds `float32` or `float64` | `kempressed holds float32 or float64, not T` |
| `zfpc` holds `int32`, `int64`, `float32` or `float64` | `zfpc holds int32, int64, float32 or float64, not T` |
| `key` is a path of nonempty segments, none `.` or `..` | `its key K is not a path within the store` |
| `resolution`'s members are finite | `its resolution is not 3 finite numbers` |
| the grid has fewer than 2^32 chunks along each axis | `its chunk grid is 2^32 chunks or more along an axis` |
| `sharding`, if present, meets [§4.1](#41-the-sharding-object) | the reasons there |
| no earlier presented scale has its `key` | `its key K is an earlier scale's` |

## 4. Shards

### 4.1 The sharding object

| member | type | requirement, or the reason recorded |
|---|---|---|
| (itself) | object | `its sharding is not an object` |
| `@type` | string | `neuroglancer_uint64_sharded_v1`; else `its sharding @type T is not neuroglancer_uint64_sharded_v1` |
| `preshift_bits` | integer | 0 to 64; else `its preshift_bits is not an integer from 0 to 64` |
| `minishard_bits` | integer | 0 to 32; else `its minishard_bits is not an integer from 0 to 32` |
| `shard_bits` | integer | 0 to 64; else `its shard_bits is not an integer from 0 to 64` |
| (those three) | | at most 64 together; else `its preshift_bits, minishard_bits and shard_bits are more than 64 bits` |
| `minishard_index_encoding` | string | `raw` (the default) or `gzip`; else `its minishard_index_encoding E is not raw or gzip` |
| `data_encoding` | string | `raw` (the default) or `gzip`; else `its data_encoding E is not raw or gzip` |
| `chunk_sizes` | | of the scale: one element, as the format requires; else `a sharded scale has more than one chunk size` |
| `hash` | string | `identity`; else `its hash H scatters chunks across shards, so no shard is a box of chunks` |

Then two conditions on the bits, with `b` the bits of the chunk id
([§4.2](#42-a-shard-is-a-box-of-chunks)): `b` is at most 64 (else `its chunk ids need B bits, more
than 64`), and `preshift_bits + minishard_bits + shard_bits` is at least `b` (else `its shards wrap:
chunk ids have B bits, and preshift_bits, minishard_bits and shard_bits name N of them, so that one
shard holds several boxes of chunks`).

### 4.2 A shard is a box of chunks

A chunk's **id** is its compressed Morton code: with the grid `(gx, gy, gz)`, each axis has
`ceil(log2(g))` bits, and the id takes their bits from the lowest, x, then y, then z, in turn,
leaving out an axis whose bits are used up. With the `identity` hash, the format places a chunk by
its id alone:

- its **minishard** is `(id >> preshift_bits) & (2^minishard_bits − 1)`;
- its **shard** is `(id >> (preshift_bits + minishard_bits)) & (2^shard_bits − 1)`.

So the id's lowest `L = preshift_bits + minishard_bits` bits say where a chunk lies in its shard,
and the bits past them say which shard it is in. The lowest `L` bits of a compressed Morton code
are `ax` bits of x, `ay` of y and `az` of z, whatever the other bits are, so every shard holds the
chunks of one box of `(kx, ky, kz) = (2^ax, 2^ay, 2^az)` chunks, aligned to that box's size on the
grid. That box, the shard's own, is the Zarr chunk: a box that runs past the grid is a Zarr chunk
that runs past the array, and every scale of a volume has one. Shards a writer sizes for a scale
holding 8 192 chunks are boxes of 32 × 16 × 16.

A shard's **number** `s` gives its box `(bx, by, bz)` through those same bits: bit `j` of `s` is bit
`L + j` of the id, which goes to the axis and the bit that the id's order gives it, counted from
past that axis's `ax`, `ay` or `az`. A set bit of `s` past the id's bits names no box.

### 4.3 Shards as Zarr chunks

A shard object is at `<key>/<h>.shard`, `<h>` the shard's number in lowercase hexadecimal with
`ceil(shard_bits / 4)` digits (`0` when `shard_bits` is 0). Its Zarr chunk is at the box's
coordinates: `<n>/c/<bz>/<by>/<bx>`, with `0/` first when `C > 1`. An object of that directory whose
name is not of that form, is not canonical, or whose number names no box within the grid is an
other object ([§8](#8-other-objects)). A shard the store does not have has no entry, and reads as
the fill value, like a chunk its shard does not hold.

## 5. The image

The presented scales are the levels of one OME-NGFF image, at `<n>` in ascending order of the
product of `resolution`'s members in binary64, scales of one product in the order of `scales`. A
level's resolution is along z, y, x, with 1 for `c`.

A voxel `i` covers `[i, i + 1) × resolution` in the format, and OME-NGFF places a voxel's center.
So a level's translation along an axis is `(voxel_offset + 0.5) × resolution`, in binary64, and 0
for `c`: the first voxel of a level downsampled by 2 lies over the first two of the level before.

<a id="type-precomputedroot"></a>**`PrecomputedRoot`**, the root's `zarr.json`, a
[`GroupDocument`](../conventions.md#type-groupdocument) whose attributes are a
[`Declaration`](../conventions.md#type-declaration), and `ome`, a
[`PrecomputedOme`](#type-precomputedome), when any scale is presented. A root with no presented
scale is a plain group.

<a id="type-precomputedome"></a>**`PrecomputedOme`**, an [`OmeImage`](../conventions.md#type-omeimage) with:

| member | value |
|---|---|
| `axes` | `c` of type channel, when `C > 1`; `z`, `y` and `x` of type space, with the unit `nanometer`, which the format defines |
| `datasets` | one per presented scale: `path` `"<n>"`, then a [`Scale`](../conventions.md#type-scale) and a [`Translation`](../conventions.md#type-translation) as above |
| `omero` | when `type` is `"image"`: one channel each, `label` its index, `color` `FF0000`, `00FF00` and `0000FF` in a volume of 3 channels and `FFFFFF` otherwise, and for an integer `data_type` the [`Window`](../conventions.md#type-window) of the type's whole range, shown whole |

A `segmentation` has no `omero`: its values are labels, not intensities.

## 6. The codec `refkv.precomputed`

An array-to-bytes codec, which reads a Zarr chunk that is one object of the volume. No other codec
fits: the format's edge chunks are clipped, in a shape only their position gives; its JPEG chunks
hold a volume as one image; and a shard's chunks are found through an index no Zarr sharding codec
reads ([§10](#10-what-the-layout-does-not-express-and-what-was-weighed)).

<a id="type-precomputedcodecconfiguration"></a>**`PrecomputedCodecConfiguration`**, in `info`'s own
terms, x, then y, then z:

| member | type | presence | value |
|---|---|---|---|
| `encoding` | string | required | the scale's `encoding` |
| `size` | 3 integers | required | the scale's `size` |
| `chunk_size` | 3 integers | required | the scale's first chunk size |
| `compressed_segmentation_block_size` | 3 integers | when that is the encoding | the scale's |
| `sharding` | object | when the scale is sharded | `preshift_bits`, `minishard_bits`, `minishard_index_encoding` and `data_encoding`, each given even where `info` leaves the default |

### 6.1 A Zarr chunk's value

<a id="type-precomputedchunk"></a>**`PrecomputedChunk`**, the reference of `<n>/c/<coords>`:

| part | range | value |
|---|---|---|
| the position | literal | the position on the chunk grid of the first chunk the Zarr chunk holds, as 3 little-endian `uint32`, x, y and z |
| the object | source | the chunk object or shard, whole |

A Zarr codec is not told where its chunk lies, and a clipped chunk's shape depends on it, as does a
chunk's id. The position makes each value self-describing, in 12 bytes that no read needs a request
for, as FITS's tile parameters do ([fits §3.3](fits.md#33-tile-compressed-images)).

### 6.2 Decoding

The decoder reads the position, and for each chunk the selection touches:

1. **Finds its bytes.** In an unsharded array, they are the object. In a sharded one, with `m` the
   `minishard_bits`: the first `16 × 2^m` bytes of the object are the **shard index**, for each
   minishard two little-endian `uint64`, the start and the end of its minishard index, relative to
   the end of the shard index. A minishard index, inflated if `minishard_index_encoding` is `gzip`,
   is `n` little-endian `uint64` three times: the chunks' ids, delta coded; their offsets, each
   relative to the end of the chunk before it (to the end of the shard index for the first); and
   their sizes. A chunk whose id is not in its minishard reads as the fill value. Its bytes are
   inflated if `data_encoding` is `gzip`.
2. **Takes off a content coding**, as [§6.3](#63-content-codings) says.
3. **Decodes the voxels** of a chunk of its **clipped** shape, `x = min(cx, sx − i × cx)` and so on,
   `i` its position on the grid:
   - `raw`: little-endian values, x fastest, then y, z and the channel. A raw chunk of the whole
     chunk size, from a writer that pads its edge chunks, is accepted too, cut back to the clipped
     shape.
   - `jpeg`, `png`, `jxl`: one image of width `x` and height `y × z`, its samples the channels, its
     rows the volume's in the same order.
   - `compressed_segmentation`: the stream the format defines, with a block grid of the clipped
     shape.
   - `compresso`, `crackle`: a stream of that format, its own header giving `x`, `y` and `z` and its
     voxels' width, x fastest.
   - `fpzip`: an fpzip stream of 4 dimensions, `x`, `y`, `z` and the channel, x fastest.
   - `zfpc`: a [zfpc](https://github.com/seung-lab/zfpc) stream of dimensions as for `fpzip`: a
     header of 23 bytes, whose byte 5's low 3 bits are the data type, 1 to 4 for `int32`, `int64`,
     `float32` and `float64`, whose 4 little-endian `uint32`s from byte 6 are the dimensions, and
     whose byte 22 has bit `a` set when dimension `a` is correlated; then a little-endian `uint64`,
     the offset of the first stream, and one the size of each stream; then the ZFP streams, each
     with its header, one per position along the dimensions not correlated, the first of them
     fastest.
   - `kempressed`: an fpzip stream of dimensions `x`, `y`, the channel and `z`, each value 2 more
     than the voxel's, as [cloud-volume](https://github.com/seung-lab/cloud-volume/blob/master/cloudvolume/chunks.py)
     writes it.

   An encoded shape or data type other than the clipped shape and the array's is an error.
4. **Fills the rest of the chunk** with the fill value, past the volume's end.

These encodings need decoders that only a reader of those chunks needs: the profile reads no chunk ([§11.1](#111-reading)), so a hierarchy is the same with or
without one.

A reader of one chunk of a shard makes at most three requests: the shard index, one minishard index
and the chunk. Chunks read together from a minishard may be one request when they lie near each
other in the shard.

### 6.3 Content codings

An unsharded chunk object may be coded as a whole, which its name may say and an HTTP header usually
does: a store of the format serves an object with `Content-Encoding: gzip` or `br`, and Neuroglancer
reads it through a browser that undoes the coding. The listing does not tell, and the profile reads
no chunk, so the decoder finds the coding from the bytes, one object at a time:

| the bytes | are |
|---|---|
| of a raw chunk's clipped or whole length, for `raw` | not coded |
| beginning `FF D8`, for `jpeg` | not coded |
| beginning `89 50 4E 47`, for `png` | not coded |
| whose first little-endian `uint32` is `num_channels`, for `compressed_segmentation` | not coded: the first of a stream's channel offsets |
| beginning `cpso`, for `compresso` | not coded |
| beginning `crkl`, for `crackle` | not coded |
| beginning `FF 0A` or `00 00 00 0C 4A 58 4C 20 0D 0A 87 0A`, for `jxl` | not coded: a codestream, or a container |
| beginning `fpy)`, for `fpzip` and `kempressed` | not coded |
| beginning `zfpc`, for `zfpc` | not coded |
| beginning `1F 8B` | gzip (RFC 1952) |
| beginning `28 B5 2F FD` | Zstandard (RFC 8878) |
| anything else | brotli (RFC 7932), which has no signature; bytes that do not decode are an error |

The order keeps a chunk that is not coded from being taken for one that is. A compressed stream
exactly as long as the raw chunk would be read as raw, which no compressor produces: a stream that
does not shrink its data grows it. A reader follows [spec/refkv.md §5.2](../refkv.md#52-reading-over-http)
to read such an object: a 206 whose total is the object's `size` pin gives the stored bytes.

## 7. Source metadata

<a id="type-precomputedinfo"></a>**`PrecomputedInfo`**, the root's source metadata:

| member | type | presence | value |
|---|---|---|---|
| `info` | object | required | the `info` document, every member, with its numbers as written |
| `not_presented` | array of [`PrecomputedNotPresented`](#type-precomputednotpresented) | optional | the scales [§3.3](#33-scales-that-are-not-presented) left out, in the order of `scales`; absent when there is none |

<a id="type-precomputednotpresented"></a>**`PrecomputedNotPresented`**:

| member | type | presence | value |
|---|---|---|---|
| `index` | integer | required | its index in `scales` |
| `reason` | string | required | the reason [§3.3](#33-scales-that-are-not-presented) gives |

<a id="type-precomputedscale"></a>**`PrecomputedScale`**, an array's source metadata, so that an
array copied out of the hierarchy still describes itself:

| member | type | presence | value |
|---|---|---|---|
| `index` | integer | required | the scale's index in `scales` |
| `scale` | object | required | the scale, every member |

### 7.1 The IR mirror

`refkv_source` is the store's **IR mirror** ([conventions §8.9](../conventions.md#89-stores)): its
table, from which `info` is rebuilt byte for byte and the listing (every object's key and size) is
read back, and its view, `refkv_source/tree`.

<a id="type-precomputedmirror"></a>**`PrecomputedMirror`**, `refkv_source`'s source metadata: a
[`Mirror`](../conventions.md#type-mirror) with these members, and no `empty_dirs`:

| member | type | presence | value |
|---|---|---|---|
| `ir` | [`IrDescription`](../conventions.md#type-irdescription) | required | the mirror's description, a store IR's, with `objects` ([conventions §8.2](../conventions.md#82-the-table), [§8.9](../conventions.md#89-stores)) |
| `ignored` | array of string | optional | the recorded ignored keys of [spec/zarrify.md §1.4](../zarrify.md#14-store-inputs), in ascending order of their UTF-8 bytes; present only when there is one |

### 7.2 Elements

Paths are relative to the IR's root; the root and the directories are the listing's, so the path of
an object is its key, each segment named by [conventions §8.9](../conventions.md#89-stores).

| path | kind | type | what |
|---|---|---|---|
| `""` | struct | | the root: the store's root directory (no extent) |
| a directory's path | struct | | each directory of the store (no extent) |
| `info` | object | | the document: held, extent `(0, s)` |
| `info/<m>` | value | `ascii[n]` | the value of the document's top-level member named `<m>`, its bytes as written: the member's JSON text, named by the rule of [the N5 convention §5.2](n5.md#52-elements) |
| `info/gaps/<o>` | gap | | the rest of the document at offset `<o>`: braces, member names, colons, commas and whitespace |
| `info/json` | value | `ascii[s]` | in place of the members and gaps, the whole document, when it has more than 4096 top-level members |
| any other key | object | | every other object: referenced (one leaf, its whole source) when its size is not 0, empty otherwise |

### 7.3 Equivalence

Hierarchies of one store are compared entry for entry, `refkv_source` included
([conventions §11.4](../conventions.md#114-equivalence)).

## 8. Other objects

An **other object** is an object of the store whose size is not 0 and that is neither `info` nor a
chunk object of a presented scale ([§3.2](#32-chunk-objects)) nor a shard of one
([§4.3](#43-shards-as-zarr-chunks)): `provenance`, meshes, skeletons, segment properties, the objects
of a scale that is not presented, and anything else. Each has a reference
`refkv_source/objects/<k>` to the whole object, `<k>` its key with `~` appended to a last segment
that a Zarr reader would open, as [the Zarr v2 convention §5](zarr2.md#5-other-objects) says. No
listed object is dropped: an empty one is in the mirror's listing.

## 9. Example

[Harris15](https://neurodata.io/data/kharris15/) (`kharris15/oblique/em`, inspected 2026-11-03), a
raw `uint8` volume of 3 scales with chunks of 512 × 512 × 16, is unsharded and served with
`Content-Encoding: br`. The finest scale is `2.0_2.0_50.0`, of size 8192 × 8192 × 91, and has 550
chunk objects. The hierarchy has, among others:

```
zarr.json                               the root: its ome, and info in its source metadata
0/zarr.json                             shape [91, 8192, 8192], chunk shape [16, 512, 512]
0/c/5/0/0                               [0, 0, 5] as 3 uint32, then 2.0_2.0_50.0/0-512_0-512_80-91
2/zarr.json                             of 8.0_8.0_50.0, shape [91, 2048, 2048]
refkv_source/objects/provenance         the object, whole
```

The chunk at `0/c/5/0/0` is clipped along z: its stream inflates to 512 × 512 × 11 bytes, which the
codec places in the first 11 of 16 slices. The archive is 135 836 bytes for 766 chunks, and takes
1.4 s to write.

[BANC](http://banc.community) (`bates_phelps_kim_yang2025/banc/em`, inspected 2026-11-03) is JPEG in
shards, 7 scales in 87 767 objects. Its finest scale `8_8_45` has size 131072 × 147456 × 7010 and
chunks of 128 × 128 × 16. With `preshift_bits` 11, `minishard_bits` 2 and `shard_bits` 17, each shard
is a box of 32 × 16 × 16 chunks, so the array's chunk shape is `[256, 2048, 4096]`, and 64 512 of its
2^17 shard numbers have a box within the grid, each with its object. At the coarsest scale,
`512_512_45`, of size 2048 × 2304 × 7010 and `shard_bits` 5, the box is 16 × 32 × 16 chunks, and runs
past the 18 chunks the volume has along y: tensorstore gives that scale the same shape as its write
chunk, (2048, 2304, 256). The archive is 14 731 923 bytes for 690 million chunks, and took 23.6 s to
write. Reading a few chunks takes about a second, and the values equal
tensorstore's.

## 10. What the layout does not express, and what was weighed

*This section is informative.*

**Not expressed.** Encodings the codec has no decoder for are kept but not presented, as is a `jxl`
scale of 3 channels: cloud-volume, the writer these encodings have, writes 3 gray frames that its own
reader does not read back in order, and one RGB image is another layout of the same bytes, which a
reader that does not see the frames, as Neuroglancer's does not, would take for it. A scale whose
hash is `murmurhash3_x86_128`, whose shards are no boxes, is kept but not presented: all 8 sharded
volumes of the 13 surveyed (hemibrain, FAFB, FlyWire, H01, MICrONS and BANC among them, inspected
2026-11-03) have `identity`, and none wraps. A later layout could present such a scale with a
reference per chunk, read from every minishard index, at the cost below. Chunk sizes after the first
add no voxels, and meshes, skeletons and annotations stay objects, though a convention of their own
could present them.

**The alternatives to a reference per object.** At BANC's scale, the choices come to this:

| | references | requests to build | store | what a reader needs |
|---|---|---|---|---|
| a reference per chunk, from every minishard index | 690 million | at least 2 per shard (the shard index, then its minishard indexes), 175 000 | several gigabytes | `imagecodecs_jpeg`, but a clipped chunk still needs a codec |
| `sharding_indexed`, its index a literal per shard | 87 767, with 131 KiB of index each | the same | 11 GiB, and past the 65 519 bytes a payload may hold, so not allowed | a stock codec, but a clipped chunk still needs another |
| this layout | 87 767 | the listing, 88 requests | 15 MB | `refkv.precomputed` |

The cost of this layout is the codec. A reader without it opens the hierarchy and cannot decode a
chunk, where the first row would serve one for an unclipped chunk. The cost of a read is the
format's own: a reader finds a chunk through the shard's index, each read, where the first row has
the offset in the reference.

**Position in a literal, or a rectilinear grid.** A rectilinear grid would say a clipped chunk's
shape without a header. It breaks the consumers that
[docs/adding-a-format.md](../../docs/adding-a-format.md#3-decide-the-layout) lists, and does not give
a shard's position, which a chunk's id needs.

**Why not read a content coding from the header.** A profile that asked for each object's
`Content-Encoding` would make a request per chunk, which is what the layout is built to avoid, and
one object's header says nothing of the next's. The bytes say it, for no request.

# Part 2. The profile

## 11. Neuroglancer precomputed profile

**Choosing the profile.** A store whose root has the key `info`, and none of the keys
[spec/zarrify.md §1.4](../zarrify.md#14-store-inputs) tests first. Neuroglancer's `precomputed://`
and `|neuroglancer-precomputed:` spellings are not store URLs: give the URL they wrap, ending in `/`.

### 11.1 Reading

The profile lists the store ([spec/zarrify.md §1.5](../zarrify.md#15-listing-a-store)) and reads one
object, `info`, whole, in one request. It reads nothing else, however many chunks the volume has.

### 11.2 Rejection

These reject the input, in this order:

| check | message |
|---|---|
| the root has `info` | `not a Neuroglancer precomputed store: the root has no info` (the host's own message first, when the store was not named as precomputed) |
| `info` is at most 2^24 bytes | `info: JSON document of S bytes exceeds 16777216` |
| `info` is not empty | `info: invalid JSON: the document is empty` |
| `info` is JSON, by [spec/zarrify.md §1.6](../zarrify.md#16-json-documents) | `info: invalid JSON...` |
| it is an object | `info is not a JSON object` |
| `@type` | `info: @type T is not a volume` |
| `data_type` | `info: data_type T is not supported` |
| `num_channels` | `info: num_channels N is not 1 to 65535 channels` |
| `type` | `info: type T is not a string` |
| `scales` | `info: scales is not a nonempty array` |

Nothing about a scale rejects the input ([§3.3](#33-scales-that-are-not-presented)). A store of a
volume with no scale it can present has a root with no `ome`, and all its objects are other objects.

### 11.3 Chunks and other objects

Every chunk object, shard and other object has one reference, whole, its source a `url` source with
a `size` pin, its listed size. The url sources are one per object some reference uses, in the order
of first use, the references in ascending order of their keys' UTF-8 bytes, as for SAFE
([safe.md §8.8](safe.md#88-the-source-table)). A chunk's reference is a `Concat` of the literal
position and that range ([§6.1](#61-a-zarr-chunks-value)); an other object's is that range alone.
No other entry is a reference, and the output has no data sources.

### 11.4 Summary

The summary line holds `arrays` (presented scales), `notPresented`, `sharded` (the arrays with
shards), `chunks` (the references to chunk objects and shards), `emptyChunks`, `objects` and
`otherObjects`, then what every store's holds ([conventions §8.9](../conventions.md#89-stores)).
