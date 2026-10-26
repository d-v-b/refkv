# NIfTI

The Zarr layout of a NIfTI-1 or NIfTI-2 single file (`.nii`), and the
translation of its header into JSON. What all of refkv's conventions share is
in [spec/conventions.md](../conventions.md), cited here as "conventions §n".
How refkv produces this layout as a reference store, and which files it
accepts, is the NIfTI profile, [Part 2](#part-2-the-profile) below.

Convention version: 0 (until release, [conventions §1](../conventions.md#1-conventions)) · UUID: `06e5809d-4d54-4b72-afd0-6bf61a7b4c85` ·
Schema: [schema.json](nifti/schema.json) · Changed 2026-10-25: an edge chunk's zero padding
is a range of the output's one zero data source, not a literal range of each reference, so that
the archive holds the zeros once ([§7.2](#72-chunks)); no chunk's values change, nor which
chunks are copied · Changed 2026-10-26, with no change to any output: the layout is tabulated,
its entries in [Layout](#layout) and its types in type tables
([conventions §11.5](../conventions.md#115-layout-tables))

This document has two parts ([conventions §11.1](../conventions.md#111-parts)).

# Part 1. The convention

## Layout

The hierarchy's entries ([conventions §11.5](../conventions.md#115-layout-tables)):

| prefix | type | structure | description |
|---|---|---|---|
| `zarr.json` | group | [`ImageGroup`](../conventions.md#type-imagegroup) | the root: the image ([§4](#4-the-image)) |
| `zarr.json#/attributes/ome` | attribute | [`OmeImage`](../conventions.md#type-omeimage) | `M`: one level, the array `0`, with the scales and translation of [§4.3](#43-scales-units-and-position), and `omero` as [§4.4](#44-channels) says |
| `zarr.json#/attributes/zarr_conventions` | attribute | array of [`ConventionMetadata`](../conventions.md#type-conventionmetadata) | the CMO of [§1](#1-declaration) |
| `zarr.json#/attributes/refkv_zarrified` | attribute | [`RootProperty`](../conventions.md#type-rootproperty) | `profile` `"nifti"`, `version` `0`, `source.url` the file's URL |
| `zarr.json#/attributes/refkv_zarrified/nifti` | attribute | [`NiftiRoot`](#type-niftiroot) | the root's source metadata ([§5.1](#51-the-root)) |
| `0/zarr.json` | array | [`NiftiArray`](#type-niftiarray) | the voxels ([§4.1](#41-the-array)) |
| `0/c/<coords>` | chunk | [`NiftiChunk`](#type-niftichunk) | every chunk of the array ([§7.2](#72-chunks)) |
| `refkv_source/**` | subtree | [the mirror's entries](../conventions.md#table-mirror) | the IR mirror ([§5.2](#52-the-ir-mirror)): its elements are [§5.3](#53-elements)'s |

| placeholder | value |
|---|---|
| `<coords>` | the chunk's coordinates along the array's axes, in decimal, one key segment each: `<t>/<c>/<z>/<y>/<x>`, each only for the axes present (so it stands for two to five segments) |

## 1. Declaration

The root and `refkv_source` declare the convention
([conventions §11.2](../conventions.md#112-declaration)), the root with its source
metadata of [§5.1](#51-the-root) as the member `"nifti"`. Its CMO is:

```json
{
  "uuid": "06e5809d-4d54-4b72-afd0-6bf61a7b4c85",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/zarrify/nifti/schema.json",
  "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nifti.md",
  "name": "refkv_zarrified",
  "description": "The Zarr layout of a NIfTI source zarrified by refkv, and the source's metadata"
}
```

The array has no attributes.

## 2. The header

The file's **header** is its first `L` bytes, `L = 348` for NIfTI-1 and
`L = 540` for NIfTI-2, in the file's byte order (the one in which
`sizeof_hdr` reads as `L`). Its fields, in the order of the file, are those
of the NIfTI-1 and NIfTI-2 standards (`nifti1.h`, `nifti2.h`), at these byte
offsets. Their types:

| type | value |
|---|---|
| `i16`, `i32`, `i64` | a two's complement integer |
| `u8` | an unsigned byte |
| `f32`, `f64` | an IEEE 754 binary32 or binary64 number (an `f32` value is converted to binary64 exactly) |
| `char[k]` | `k` bytes of text |
| `T[k]` | `k` consecutive values of type `T`, indexed from 0 |

NIfTI-1:

| field | offset | type |
|---|---|---|
| `sizeof_hdr` | 0 | `i32` |
| `data_type` | 4 | `char[10]` |
| `db_name` | 14 | `char[18]` |
| `extents` | 32 | `i32` |
| `session_error` | 36 | `i16` |
| `regular` | 38 | `char[1]` |
| `dim_info` | 39 | `u8` |
| `dim` | 40 | `i16[8]` |
| `intent_p1` | 56 | `f32` |
| `intent_p2` | 60 | `f32` |
| `intent_p3` | 64 | `f32` |
| `intent_code` | 68 | `i16` |
| `datatype` | 70 | `i16` |
| `bitpix` | 72 | `i16` |
| `slice_start` | 74 | `i16` |
| `pixdim` | 76 | `f32[8]` |
| `vox_offset` | 108 | `f32` |
| `scl_slope` | 112 | `f32` |
| `scl_inter` | 116 | `f32` |
| `slice_end` | 120 | `i16` |
| `slice_code` | 122 | `u8` |
| `xyzt_units` | 123 | `u8` |
| `cal_max` | 124 | `f32` |
| `cal_min` | 128 | `f32` |
| `slice_duration` | 132 | `f32` |
| `toffset` | 136 | `f32` |
| `glmax` | 140 | `i32` |
| `glmin` | 144 | `i32` |
| `descrip` | 148 | `char[80]` |
| `aux_file` | 228 | `char[24]` |
| `qform_code` | 252 | `i16` |
| `sform_code` | 254 | `i16` |
| `quatern_b` | 256 | `f32` |
| `quatern_c` | 260 | `f32` |
| `quatern_d` | 264 | `f32` |
| `qoffset_x` | 268 | `f32` |
| `qoffset_y` | 272 | `f32` |
| `qoffset_z` | 276 | `f32` |
| `srow_x` | 280 | `f32[4]` |
| `srow_y` | 296 | `f32[4]` |
| `srow_z` | 312 | `f32[4]` |
| `intent_name` | 328 | `char[16]` |
| `magic` | 344 | `char[4]` |
NIfTI-2:

| field | offset | type |
|---|---|---|
| `sizeof_hdr` | 0 | `i32` |
| `magic` | 4 | `char[8]` |
| `datatype` | 12 | `i16` |
| `bitpix` | 14 | `i16` |
| `dim` | 16 | `i64[8]` |
| `intent_p1` | 80 | `f64` |
| `intent_p2` | 88 | `f64` |
| `intent_p3` | 96 | `f64` |
| `pixdim` | 104 | `f64[8]` |
| `vox_offset` | 168 | `i64` |
| `scl_slope` | 176 | `f64` |
| `scl_inter` | 184 | `f64` |
| `cal_max` | 192 | `f64` |
| `cal_min` | 200 | `f64` |
| `slice_duration` | 208 | `f64` |
| `toffset` | 216 | `f64` |
| `slice_start` | 224 | `i64` |
| `slice_end` | 232 | `i64` |
| `descrip` | 240 | `char[80]` |
| `aux_file` | 320 | `char[24]` |
| `qform_code` | 344 | `i32` |
| `sform_code` | 348 | `i32` |
| `quatern_b` | 352 | `f64` |
| `quatern_c` | 360 | `f64` |
| `quatern_d` | 368 | `f64` |
| `qoffset_x` | 376 | `f64` |
| `qoffset_y` | 384 | `f64` |
| `qoffset_z` | 392 | `f64` |
| `srow_x` | 400 | `f64[4]` |
| `srow_y` | 432 | `f64[4]` |
| `srow_z` | 464 | `f64[4]` |
| `slice_code` | 496 | `i32` |
| `xyzt_units` | 500 | `i32` |
| `intent_code` | 504 | `i32` |
| `intent_name` | 508 | `char[16]` |
| `dim_info` | 524 | `u8` |
| `unused_str` | 525 | `char[15]` |
`n = dim[0]` is the number of dimensions. The **sizes** `X`, `Y`, `Z`, `T`,
`C` are `dim[1]` to `dim[5]`, each 1 when its index is more than `n`;
dimensions 6 and 7 have size 1. `v` is `vox_offset` as an integer.

## 3. Data types

| `datatype` | name | Zarr data type | bytes per voxel `b` | samples per voxel |
|---|---|---|---|---|
| 2 | UINT8 | `uint8` | 1 | 1 |
| 4 | INT16 | `int16` | 2 | 1 |
| 8 | INT32 | `int32` | 4 | 1 |
| 16 | FLOAT32 | `float32` | 4 | 1 |
| 64 | FLOAT64 | `float64` | 8 | 1 |
| 256 | INT8 | `int8` | 1 | 1 |
| 512 | UINT16 | `uint16` | 2 | 1 |
| 768 | UINT32 | `uint32` | 4 | 1 |
| 1024 | INT64 | `int64` | 8 | 1 |
| 1280 | UINT64 | `uint64` | 8 | 1 |
| 128 | RGB24 | `uint8` | 3 | 3 |
| 2304 | RGBA32 | `uint8` | 4 | 4 |

RGB24 and RGBA32 are the **color** data types: each voxel is its samples
(red, green, blue and, for RGBA32, alpha) in consecutive bytes. The file's
voxel data is `X × Y × Z × T × C × b` bytes at offset `v`, `x` varying
fastest, then `y`, `z`, `t` and `k`, the index along dimension 5.

## 4. The image

The hierarchy is one image at the root ([conventions §4](../conventions.md#4-images)),
without a name, with one array at path `"0"`: the root `zarr.json` is
`{"zarr_format": 3, "node_type": "group", "attributes": A}`, where `A` has
`ome` (`M`, below), `zarr_conventions` and `refkv_zarrified` ([§1](#1-declaration)).

### 4.1 The array

**Stored order.** The file holds the voxels as a row-major array `V` whose axes are,
outermost first: `c` (dimension 5) if `n ≥ 5`; `t` if `n ≥ 4`; `z` if `n ≥ 3`; `y`; `x`;
and, for a color type, `c` (the samples). Its sizes are those of the array's axes, and its
elements are `b` bytes, or 1 byte (a sample) for a color type. The axes of `V` are the
array's, in another order when `n ≥ 5` (dimension 5 is stored outside dimension 4) or the
data type is a color type (the samples are stored innermost). `V` is cut as
[conventions §7](../conventions.md#7-source-values-as-arrays) cuts contiguous values, with
the chunk limit `L = 131072` (2^17) bytes, which gives a chunk shape for `V` (1 along its
axes before the cut axis, whole along those after) and the bytes of each chunk.

<a id="type-niftiarray"></a>**`NiftiArray`**, the array's `zarr.json`: an
[`ArrayDocument`](../conventions.md#type-arraydocument) with these members:

| member | type | presence | value |
|---|---|---|---|
| `dimension_names` | array of string | required | the axes: `t` if `n ≥ 4`; `c` if `n ≥ 5` or the data type is a color type; `z` if `n ≥ 3`; then `y` and `x`. (The axes follow `n`, so a dimension of size 1 up to `n` still has its axis.) |
| `shape` | array of integer | required | `T`, `C` (the samples per voxel for a color type), `Z`, `Y` and `X`, each only for the axes present |
| `data_type` | string | required | from [§3](#3-data-types) |
| `chunk_grid` | object | required | regular, its chunk shape `V`'s chunk shape, each axis's size in the array's axis order |
| `codecs` | array of object | required | when the axes of `V` are in another order than the array's, `transpose` ([conventions §3](../conventions.md#3-arrays)), with `order` the index in the array's axes of each axis of `V`, in the order of `V` (for example `[1, 0, 2, 3, 4]` for `n ≥ 5`, and `[0, 2, 3, 4, 1]` for a color type with `t`, `c`, `z`, `y` and `x`); then `bytes`, with `"endian"` (`"little"` or `"big"`: the file's byte order) when the data type is larger than 1 byte. There is no compressor |
| `attributes` | object | required | `{}` |

**Chunks:** every chunk is present. The array's chunk with coords `q_a` along each axis `a`
is the chunk of `V` with the same coords along the same axes: the bytes that
[conventions §7](../conventions.md#7-source-values-as-arrays) gives it, in the order of `V`,
which the transpose codec puts in the array's order. An edge chunk along the cut axis is
followed by zero bytes up to the whole chunk (Zarr stores edge chunks whole).

  For example, a `64 × 64 × 33 × 200` int16 series (`n = 4`) has slices of
  8192 bytes and volumes of 270336, over `L`, so the cut axis is `z`: `k =
  ceil(33 / floor(131072 / 8192)) = 3` chunks of 11 slices each, the chunk
  shape `[1, 11, 64, 64]`. A single voxel's series of 100000 float32
  values is cut along `t` into 4 chunks of 25000.

The array holds the voxels' raw values. NIfTI's value scaling is not
applied; it is recorded in the source metadata ([§5](#5-source-metadata)).

### 4.2 Value scaling

For a data type that is not a color type, the **scaling** applies when
`scl_slope` is finite and not 0; its slope `s` is `scl_slope`, and its
intercept `i` is `scl_inter` if that is finite, else 0. (A color type, or
a `scl_slope` of 0, ±∞ or NaN, has no scaling.) The scaling is
**nontrivial** when `s ≠ 1` or `i ≠ 0`. The scaled value of a voxel is
`s × raw + i`.

### 4.3 Scales, units and position

**Units.** The spatial unit is given by `xyzt_units` modulo 8 (its bits
0–2), and the time unit by `xyzt_units` bitwise-and 56 (its bits 3–5):

| unit | bits | value | OME-NGFF unit |
|---|---|---|---|
| spatial | 0–2 | 1 | `meter` |
| spatial | 0–2 | 2 | `millimeter` |
| spatial | 0–2 | 3 | `micrometer` |
| time | 3–5 | 8 | `second` |
| time | 3–5 | 16 | `millisecond` |
| time | 3–5 | 24 | `microsecond` |

Any other value gives no unit (for time, including Hz, ppm and rad/s). For NIfTI-2 the bits
are those of the `i32` in two's complement.

**Affine.** The image's **affine** is the sform if `sform_code > 0`, else
the qform if `qform_code > 0`, else there is none. (A code of 0 or less is
unset. A set sform is used even when it is not representable; the qform is
then not considered.) OME-NGFF 0.5 places an image only by a scale and a
translation per axis, so an affine is used only when it is
**representable**: an axis-aligned map with a positive diagonal.

- The sform is representable when its 12 values are finite, the six values
  `srow_x[1]`, `srow_x[2]`, `srow_y[0]`, `srow_y[2]`, `srow_z[0]` and
  `srow_z[1]` are 0, and `srow_x[0]`, `srow_y[1]` and `srow_z[2]` are
  positive. Its diagonal is `(srow_x[0], srow_y[1], srow_z[2])` and its
  offset `(srow_x[3], srow_y[3], srow_z[3])`.
- The qform is representable when `quatern_b`, `quatern_c` and `quatern_d`
  are all 0 (the rotation is the identity), `pixdim[0]` is not negative
  (`qfac` is 1; a NaN `pixdim[0]` is not negative), `pixdim[1]`,
  `pixdim[2]` and `pixdim[3]` are finite and positive, and `qoffset_x`,
  `qoffset_y` and `qoffset_z` are finite. Its diagonal is `(pixdim[1],
  pixdim[2], pixdim[3])` and its offset `(qoffset_x, qoffset_y, qoffset_z)`.

An affine that flips or rotates an axis, such as the common radiological
sform with a negative `srow_x[0]`, is not representable: the image then has
no translation, and its scales come from `pixdim`. The affine itself is
always in the source metadata ([§5](#5-source-metadata)).

**Scales.** A number is **valid** if it is finite and positive. Each axis's scale and unit:

| axis | scale | unit |
|---|---|---|
| `x`, `y`, `z` | with a representable affine, the first, second and third values of its diagonal; otherwise `pixdim[1]`, `pixdim[2]` and `pixdim[3]` where valid, and 1 where not | the spatial unit, if any, when the scale is a value of the diagonal or a valid `pixdim`; none for scale 1 from an invalid `pixdim` |
| `t` | `pixdim[4]` if it is valid; else 1 | the time unit with a valid `pixdim[4]`; else none |
| `c` | 1 | none |

**Translation.** With a representable affine, the image has a translation
([conventions §5](../conventions.md#5-units)): the first, second and third values
of its offset for `x`, `y` and `z`, and 0 for `t` and `c` (each only for the
axes present). Otherwise it has none.

### 4.4 Channels

`M` has an `"omero"` member, an [`Omero`](../conventions.md#type-omero), in these two
cases, and in no other. Its `channels` are
[`OmeroChannel`](../conventions.md#type-omerochannel)s:

| case | when | channels | `label` and `color` | `window` |
|---|---|---|---|---|
| **color** | a color data type | one per sample | `("R", "FF0000")`, `("G", "00FF00")`, `("B", "0000FF")` and, for RGBA32, `("A", "FFFFFF")` | `{"min": 0, "max": 255, "start": 0, "end": 255}` |
| **display range** | another data type, with `cal_min` and `cal_max` finite, `cal_max > cal_min`, `lo` and `hi` (below) finite, and `C` at most 64 | one per index `k < C` of dimension 5 (one when `n < 5`) | `("C<k>", "FFFFFF")`, `k` in decimal | `{"min": lo, "max": hi, "start": lo, "end": hi}` |

The window in raw values is `lo = cal_min` and `hi = cal_max`; when the scaling applies
([§4.2](#42-value-scaling)), `lo` and `hi` are instead the smaller and the larger of
`(cal_min − i) / s` and `(cal_max − i) / s`. One of these is not finite only when the
arithmetic overflows binary64, which only NIfTI-2's binary64 fields can make it do. With
more than 64 channels `M` has no `"omero"` member: the window is the header's `cal_min` and
`cal_max`, and a window per channel would make the root grow with `C`.

## 5. Source metadata

The root's source metadata `S` ([conventions §2](../conventions.md#2-attributes))
is an object with the members of [§5.1](#51-the-root), in that order,
translated by [conventions §6](../conventions.md#6-source-metadata-as-json).
Everything the file holds is on `refkv_source`, the file's **IR mirror**
([§5.2](#52-the-ir-mirror)), from which the file is rebuilt byte for byte.

### 5.1 The root

<a id="type-niftiroot"></a>**`NiftiRoot`**, the root's `S`:

| member | type | presence | value |
|---|---|---|---|
| `nifti_version` | integer | required | 1 or 2 |
| `byte_order` | string | required | `"little"` or `"big"` |
| `header` | [`NiftiHeader`](#type-niftiheader) | required | every field of the header |
| `scaling` | [`NiftiScaling`](#type-niftiscaling) | optional | present when the scaling is nontrivial ([§4.2](#42-value-scaling)) |
| `affine` | [`NiftiAffine`](#type-niftiaffine) | optional | present when the image has an affine ([§4.3](#43-scales-units-and-position)) |

<a id="type-niftiheader"></a>**`NiftiHeader`**: an object with one member per field of
[§2](#2-the-header) for the file's version, in the order of [§2](#2-the-header) and named as
there, whose type is the field's:

| field's type | member's type | value |
|---|---|---|
| `char[k]` | [`Text`](../conventions.md#type-text) | its bytes up to the first NUL, as a text value (for example, `magic` is `"n+1"` or `"n+2"`, the bytes before the first NUL) |
| `i16`, `i32`, `i64`, `u8` | [`Integer`](../conventions.md#type-integer) | the field's value |
| `f32`, `f64` | [`Float`](../conventions.md#type-float) or [`NiftiBits`](#type-niftibits) | the field's value, or its bits when a JSON number or `"NaN"` does not keep them (below) |
| `T[k]` | array of `T`'s type | `k` values |

<a id="type-niftibits"></a>**`NiftiBits`**, a float (`f32` or `f64`) value whose bits a JSON
number or `"NaN"` does not keep:

| member | type | presence | value |
|---|---|---|---|
| `bits` | string | required | `H`, its IEEE 754 bits as 8 (`f32`) or 16 (`f64`) lowercase hexadecimal digits, most significant first, whatever the file's byte order |

These values are a negative zero (bits `80000000` or `8000000000000000`), which some JSON
writers write as `0`, and every NaN but the canonical one (`7fc00000` or
`7ff8000000000000`), which `"NaN"` stands for: one with a payload, a negative one or a
signaling one. (The header's bytes, those after a character field's first NUL included, are
also the mirror's `header` element.)

<a id="type-niftiscaling"></a>**`NiftiScaling`**:

| member | type | presence | value |
|---|---|---|---|
| `slope` | number | required | `s` ([§4.2](#42-value-scaling)) |
| `inter` | number | required | `i` |

<a id="type-niftiaffine"></a>**`NiftiAffine`**:

| member | type | presence | value |
|---|---|---|---|
| `form` | string | required | `"sform"` or `"qform"`: which affine the image has |
| `applied` | boolean | required | whether the affine is representable, and so gives the scales and translation. When it is not, it is still in `header`, and the image is placed by `pixdim` alone: flips and rotations are not applied |

`header` holds every field as stored, including those the layout does not
use, so that a reader who needs, say, the slice timing (`slice_code`,
`slice_duration`), the intent, or the full affine of a rotated image finds
it here. `scaling` is derived from `header`; it is there so that a reader
need not repeat the rules of [§4.2](#42-value-scaling).

### 5.2 The IR mirror

`refkv_source` is the file's **IR mirror**
([conventions §11.3](../conventions.md#113-the-ir-mirror)).
The IR's elements (the NIfTI **source model**) are below; nothing is left out: the
header, the extender, the extensions, the voxel data, and every byte between
and after them.

### 5.3 Elements

Paths are relative to the IR's root, whose path is `""`; `<i>` is a decimal
name index ([conventions §8.1](../conventions.md#81-elements)). Types are those of
[conventions §8.6](../conventions.md#86-types), with `E` the file's byte order
(`<` little, `>` big).

| path | kind | type | extent: what |
|---|---|---|---|
| `""` | struct | | the root: the whole file, `(0, size)` |
| `header` | value | record | `(0, L)`: the header as one record, a field per field of [§2](#2-the-header) in its order and named as there: `Ei2`, `Ei4`, `Ei8`, `u1`, `Ef4` or `Ef8` for its type (`[k]` for `T[k]`), and `cstr[k]` for `char[k]` |
| `extender` | value | `u1[4]` | `(L, 4)`: the extender bytes |
| `extensions/<i>` | value | record | extension `i` of the chain, when the extender's first byte is not 0: `{esize:Ei4,ecode:Ei4,data:cstr[d]}` for the ecodes of text extensions, 4 (AFNI, XML), 6 (comment), 8 (XCEDE, XML), 32 (CIFTI, XML) and 44 (MRS, JSON), `{esize:Ei4,ecode:Ei4,data:bytes[d]}` for the others, and `{esize:Ei4,ecode:Ei4}` when `d` is 0, `d = esize − 8` |
| `voxels` | data | | `(v, B)`: the voxel data; its form's geometry is `{"data_type": D, "samples": s}` ([§3](#3-data-types)), its codec `{"endian": "little" \| "big"}`, and its recipe `[["src", 0, null]]` |
| `gaps/<offset>` | gap | | bytes no other element claims: padding, bytes the chain does not hold before the voxel data, and bytes after it |

**The chain.** The first extension starts at `q = L + 4`; while `q + 8 ≤ v`,
the extension at `q` has `esize` (`i32`) at `q` and `ecode` (`i32`) at
`q + 4`, both in the file's byte order, and the next starts at `q + esize`.
The chain stops, without the extension at `q`, when `esize < 8` or
`q + esize > v`. (The standard's rule that `esize` is a multiple of 16 is not
checked.)

The view ([conventions §8.7](../conventions.md#87-the-view)) shows the header,
the extender and each extension of at most 1024 bytes; a text extension's
data is shown as its text up to the first NUL, with the bytes from the NUL
on when they are not all NUL.

### 5.4 Equivalence

Hierarchies of one file are compared entry for entry, `refkv_source` included
([conventions §11.4](../conventions.md#114-equivalence)).

## 6. Example

A big-endian NIfTI-1 file at `https://example.org/brain.nii` (the fixture
`fixtures/nifti/nifti_n1_be_int16_4d_scaled.nii`) holds an int16
`4 × 3 × 2 × 5` volume, a radiological sform (not representable, so the
image has no translation), and a slope of 0.5 with an intercept of −20. Its
root `zarr.json` has these attributes (`M` elided):

```json
{
  "ome": M,
  "zarr_conventions": [{
    "uuid": "06e5809d-4d54-4b72-afd0-6bf61a7b4c85",
    "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/zarrify/nifti/schema.json",
    "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nifti.md",
    "name": "refkv_zarrified",
    "description": "The Zarr layout of a NIfTI source zarrified by refkv, and the source's metadata"
  }],
  "refkv_zarrified": {
    "profile": "nifti",
    "version": 0,
    "source": {"url": "https://example.org/brain.nii"},
    "nifti": {
      "nifti_version": 1,
      "byte_order": "big",
      "header": {
        "sizeof_hdr": 348, "data_type": "", "db_name": "", "extents": 0, "session_error": 0, "regular": "", "dim_info": 0,
        "dim": [4, 4, 3, 2, 5, 1, 1, 1],
        "intent_p1": 0.0, "intent_p2": 0.0, "intent_p3": 0.0, "intent_code": 0,
        "datatype": 4, "bitpix": 16, "slice_start": 0,
        "pixdim": [-1.0, 1.5, 1.5, 3.0, 2.5, 1.0, 1.0, 1.0],
        "vox_offset": 352.0, "scl_slope": 0.5, "scl_inter": -20.0,
        "slice_end": 0, "slice_code": 0, "xyzt_units": 10, "cal_max": 10.0, "cal_min": -30.0,
        "slice_duration": 0.0, "toffset": 0.0, "glmax": 0, "glmin": 0,
        "descrip": "", "aux_file": "",
        "qform_code": 0, "sform_code": 1,
        "quatern_b": 0.0, "quatern_c": 0.0, "quatern_d": 0.0,
        "qoffset_x": 0.0, "qoffset_y": 0.0, "qoffset_z": 0.0,
        "srow_x": [-1.5, 0.0, 0.0, 90.0],
        "srow_y": [0.0, 1.5, 0.0, -126.0],
        "srow_z": [0.0, 0.0, 3.0, -72.0],
        "intent_name": "", "magic": "n+1"
      },
      "scaling": {"slope": 0.5, "inter": -20.0},
      "affine": {"form": "sform", "applied": false}
    }
  }
}
```

Its array has the chunk shape `[5, 2, 3, 4]`: its 240 bytes of voxels are
a single chunk.

A NIfTI-1 file whose one extension is followed by 4 bytes before the voxel
data, too few for another (the fixture `nifti_region_chain_tail.nii`), has
the elements `header`, `extender`, `extensions/0`, `gaps/368` (the 4 bytes)
and `voxels`, and its view `refkv_source/tree` shows, after `header`:

```json
"extender": [1, 0, 0, 0],
"extensions/0": {"esize": 16, "ecode": 6, "data": "abcdefg"}
```

A `200 × 700` uint8 slice (the fixture `nifti_rowblock_split.nii`) is
140000 bytes, over `L`, so the cut axis is `y`, of rows of 200 bytes: `k =
ceil(700 / floor(131072 / 200)) = 2` chunks of 350 rows, the chunk shape
`[1, 350, 200]`, and its chunk `0/c/0/1/0` holds rows 350 to 699, 70000
bytes.

# Part 2. The profile

The NIfTI profile of [spec/zarrify.md](../zarrify.md) (revision 16) is [§7](#7-nifti-profile) of this document. What every profile's Part 2 shares is in
[spec/zarrify.md, Profiles](../zarrify.md#profiles).

## 7. NIfTI profile

### 7.1 Header

The input is a NIfTI-1 or NIfTI-2 single file (`.nii`), which its **signature**
([spec/zarrify.md §1.2](../zarrify.md#12-input), tested at the precedence of
[profiles/nifti.json](profiles/nifti.json)) chose: bytes 0–3 of `H` are the 32-bit integer 348 in either byte order
and bytes 344–347 are `6E 2B 31 00` (`n+1`), NIfTI-1; or bytes 0–3 are the 32-bit integer 540
in either byte order and bytes 4–11 are `6E 2B 32 00 0D 0A 1A 0A` (`n+2`), NIfTI-2. Its header is the first `L` bytes of the file
([the convention §2](#2-the-header)); a file
shorter than `L` bytes is rejected. The file is **little-endian** if bytes
0–3, read as a little-endian 32-bit integer, are `L`, and **big-endian**
otherwise ([§1.2](../zarrify.md#12-input) has checked that one of the two holds). Every field is read
in that byte order, at the offset and with the type that [the convention §2](#2-the-header)
gives it.

**Checks.** The input is rejected unless all of these hold:

- **Dimensions:** `n = dim[0]` is from 1 to 7, and `dim[i]` is from 1 to
  2^53 − 1 for every `1 ≤ i ≤ n`. The values `dim[i]` for `i > n` are not
  checked. If `n ≥ 6`, `dim[6]` MUST be 1, and if `n = 7`, `dim[7]` MUST be
  1 (the layout has no axis for dimensions 6 and 7, so CIFTI-2 files, whose
  data runs along them, are rejected).
- **Data type:** `datatype` is in the table of
  [the convention §3](#3-data-types), and
  `bitpix` is 8 times its bytes per voxel `b`.
- **Color:** a color data type has `n ≤ 4`.
- **Offset:** `vox_offset` is an integer `v` with `L + 4 ≤ v ≤ 2^53 − 1`.
  For NIfTI-1, `vox_offset` is a binary32 number, which MUST be finite and
  have an integral value. (The voxel data never starts inside the header or
  its 4-byte extender. The NIfTI-1 standard's advice that `vox_offset` be a
  multiple of 16 is not checked, and a `vox_offset` of 0, which some readers
  take to mean 352, is rejected.)
- **Data size:** with `X`, `Y`, `Z`, `T`, `C` the sizes of the convention
  [§2](#2-the-header), the size of the voxel data, `B = X × Y × Z × T × C × b` computed
  exactly, is at most 2^53 − 1, and `v + B` is at most the file's size.

Every check applies whatever the other fields hold. No other field is
checked: the values the layout uses ([the convention §4](#4-the-image)) only change the
output as the convention says, and never reject the input. (A display
window whose scaling overflows binary64 is left out, by the convention
[§4.4](#44-channels), rather than rejected by [§1.3](../zarrify.md#13-arithmetic); no other number the layout computes can
overflow.)

**Extensions.** The 4 extender bytes at offset `L` and the extension chain
when the first of them is not 0 ([the convention §5.3](#53-elements)) are read
to make the source metadata. They never reject the input, and never change
where the voxel data starts: it starts at `v` whatever they hold. A
header-and-image pair (magic `ni1` or `ni2`) and a gzipped file are rejected
by [§1.2](../zarrify.md#12-input).

### 7.2 Chunks

The voxel data is `B` contiguous bytes at offset `v`, `x` varying fastest:
the voxel with indices `(x, y, z, t, k)` along dimensions 1 to 5 starts at
byte `v + ((((k × T + t) × Z + z) × Y + y) × X + x) × b`. It is the array
`V` of [the convention §4.1](#41-the-array),
row-major at offset `v`.

Every chunk of the convention's array is present. The chunk with coords
`(t, k, z, j, i)` (each only for the axes present), at `0/c/<coords>`, is
the chunk of `V` with those coords along the same axes, and is referenced
as [conventions §7](../conventions.md#7-source-values-as-arrays)
references a chunk of contiguous values (with the chunk limit 2^17 bytes):
one range of source 0, the chunk's bytes in the file, followed for an edge
chunk by its zero padding. Where [conventions §7](../conventions.md#7-source-values-as-arrays) makes that padding a literal
range, it is instead the range `(1, 0, p)` of the output's **zero data
source** ([§1.2](../zarrify.md#12-input)), `p` the padding's bytes: the
source holds as many zero bytes as the most padding any reference of the
output holds. Where [conventions §7](../conventions.md#7-source-values-as-arrays) copies the chunk (when its reference, with
the padding as a literal range, would not fit in a payload), the chunk is
copied, with its padding. The ranges lie within the file (by the data size
check). The output's source table is source 0, followed by the zero data
source when a reference is padded; it has no other data source.

<a id="type-niftichunk"></a>**`NiftiChunk`**, the chunk of `V` that starts `o` bytes into the
voxel data and holds `l` of its bytes, padded with `p` zero bytes
(by [conventions §7](../conventions.md#7-source-values-as-arrays), for the chunk at
`(i_0, …, i_{a−1}, q, 0, …, 0)`, `o` is the offset it gives the chunk's bytes, `l = m × s_a`
and `p = (c − m) × s_a`, which is 0 but for an edge chunk along the cut axis):

| part | range | presence | value |
|---|---|---|---|
| the voxels | source range `(0, v + o, l)` | required | the chunk's bytes in the file |
| the padding | source range `(1, 0, p)` | optional | when `p > 0`: the first `p` bytes of the zero data source |

Where [conventions §7](../conventions.md#7-source-values-as-arrays) copies the chunk, it is
instead `bytes`: the `l` bytes and the `p` zero bytes.

`refkv_source` is the IR mirror ([the convention §5.2](#52-the-ir-mirror)). The
archive has no other entries than the root's and the array's `zarr.json`, the
array's chunks, and the mirror's.

### 7.3 Summary

This section is informative. Both implementations print a one-line JSON
summary, with these members:

| member | value |
|---|---|
| `version` | 1 or 2 |
| `byteOrder` | `"little"` or `"big"` |
| `sizes` | each axis's size |
| `dataType` | the array's data type |
| `color` | whether the data type is a color type |
| `chunkShape` | the array's chunk shape, in its axis order |
| `chunks` | the number of the array's chunks |
| `scaling` | `{"slope": s, "inter": i}` when the scaling is nontrivial, else `null` |
| `affine` | `"sform"`, `"qform"` or `null` |
| `translation` | whether the image has a translation |
| `extensions` | whether the extender's first byte is nonzero |
