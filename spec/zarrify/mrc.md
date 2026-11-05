# MRC

The Zarr layout of an MRC2014 file (`.mrc`, `.mrcs`, `.map`, `.rec`, `.st`), gzipped or not
(`.map.gz`, `.mrc.gz`), and the translation of its header into JSON. What all of refkv's conventions share is
in [spec/conventions.md](../conventions.md), cited here as "conventions §n".
How refkv produces this layout as a reference store, and which files it
accepts, is the MRC profile, [Part 2](#part-2-the-profile) below.

Convention version: 0 (until release, [conventions §1](../conventions.md#1-conventions)) · UUID: `2aacd010-2ed1-4748-bc3c-5dd5e840bbce` ·
Schema: [schema.json](mrc/schema.json)

Changed 2026-11-04: the translation of a volume with IMOD's stamp follows IMOD's flag for the
origin's sign ([§4.2](#42-scales-units-and-position)): a file with the stamp, bit 2 of IMOD's flags
clear and no standard `nversion` is placed at `-origin`. The layout took such a file's `origin` as
it stands, which put a subvolume on the wrong side of its parent's first pixel. The dataset
layout's coordinates change with it. `header` still holds `origin` as stored.

Changed 2026-11-04: a mode 0 file with IMOD's stamp is `int8` when bit 0 of IMOD's flags is set
or `nversion` declares the standard, and `uint8` otherwise ([§3](#3-data-types)); the earlier
rule read the bit inverted, against IMOD's documentation, so a stamped mode 0 file's data type
changes.

Changed 2026-11-04: modes 3 and 101 are admitted ([§3](#3-data-types)): mode 3 as a `struct` of two
`int16`, mode 101 as `uint8` through zarr-extensions' bit-packing `packbits`, its chunks cut where
values start on a byte ([§4.1](#41-the-array)).

Changed 2026-11-04: a file gzipped whole (`.map.gz`, `.mrc.gz`) is read, as one chunk of the whole
stream ([§8.4](#84-gzipped-files)), where the profile rejected it.

Changed 2026-10-26, with no change to any output: the layout is tabulated, its entries in
[Layout](#layout) and its types in type tables
([conventions §11.5](../conventions.md#115-layout-tables)), and the text says, as refkv already
wrote it, that any one-byte data type, IMOD's `uint8` too, has the codec `bytes` without a byte
order ([§4.1](#41-the-array)).

Changed 2026-10-25: an edge chunk's zero padding is a range of the output's one zero data
source, not a literal range of each reference, so that the archive holds the zeros once
([§8.2](#82-chunks)); no chunk's values change, nor which chunks are copied.

Changed 2026-10-25: a zarrifier may leave the dataset layout out ([§1](#1-declaration),
[spec/zarrify.md §1.7](../zarrify.md#17-named-inputs-and-layouts)).

Changes before 2026-10-22 are recorded in
[the revision log of VIRTUALIZE.md](../history/virtualize/REVISIONS.md), this document's earlier
name, up to
[revision 29](../history/virtualize/REVISIONS.md#revision-29-no-recorded-revision-the-profile-registry).

This document has two parts ([conventions §11.1](../conventions.md#111-parts)).

# Part 1. The convention

## Layout

The hierarchy's entries ([conventions §11.5](../conventions.md#115-layout-tables)):

| prefix | type | structure | description |
|---|---|---|---|
| `zarr.json` | group | [`ImageGroup`](../conventions.md#type-imagegroup) | the root: the image ([§4](#4-the-image)) |
| `zarr.json#/attributes/ome` | attribute | [`OmeImage`](../conventions.md#type-omeimage) | `M`: one level, the array `0`, with the scales and translation of [§4.2](#42-scales-units-and-position), and `omero` as [§4.3](#43-channels) says |
| `zarr.json#/attributes/zarr_conventions` | attribute | array of [`ConventionMetadata`](../conventions.md#type-conventionmetadata) | the CMO of [§1](#1-declaration) |
| `zarr.json#/attributes/refkv_zarrified` | attribute | [`RootProperty`](../conventions.md#type-rootproperty) | `profile` `"mrc"`, `version` `0`, `source.url` the file's URL, and `layouts` `{"dataset": "dataset"}` unless the dataset layout is left out ([§1](#1-declaration)) |
| `zarr.json#/attributes/refkv_zarrified/mrc` | attribute | [`MrcRoot`](#type-mrcroot) | the root's source metadata ([§5.1](#51-the-root)) |
| `0/zarr.json` | array | [`MrcArray`](#type-mrcarray) | the values ([§4.1](#41-the-array)) |
| `0/c/<coords>` | chunk | [`MrcChunk`](#type-mrcchunk) or [`MrcGzipChunk`](#type-mrcgzipchunk) | every chunk of the array ([§8.2](#82-chunks)), or the one chunk of a gzipped file ([§8.4](#84-gzipped-files)) |
| `dataset/**` | subtree | [the dataset's entries](mrc-dataset.md#layout) | the dataset layout ([§1](#1-declaration), [mrc-dataset.md](mrc-dataset.md)), unless a zarrifier leaves it out |
| `refkv_source/**` | subtree | [the mirror's entries](../conventions.md#table-mirror) | the IR mirror ([§5.2](#52-the-ir-mirror)): its elements are [§5.3](#53-elements)'s |

| placeholder | value |
|---|---|
| `<coords>` | the chunk's coordinates along the array's axes, in decimal, one key segment each, in the axes' order ([§4.1](#41-the-array)): two segments for an image, three for an image stack or a volume, four for a volume stack |

## 1. Declaration

The root and `refkv_source` declare the convention
([conventions §11.2](../conventions.md#112-declaration)), the root with its source
metadata of [§5.1](#51-the-root) as the member `"mrc"`. Its CMO is:

```json
{
  "uuid": "2aacd010-2ed1-4748-bc3c-5dd5e840bbce",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/zarrify/mrc/schema.json",
  "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mrc.md",
  "name": "refkv_zarrified",
  "description": "The Zarr layout of a MRC source zarrified by refkv, and the source's metadata"
}
```

The array has no attributes.

The hierarchy also holds the file as a **dataset**: the group `dataset`,
which declares the MRC dataset convention
([mrc-dataset.md](mrc-dataset.md), [conventions §9](../conventions.md#9-dataset-layouts))
and which xarray opens with named dimensions and coordinates in angstroms.
The root's property names it: `"layouts": {"dataset": "dataset"}`, after
`source`. A zarrifier told to write the image layout alone
([spec/zarrify.md §1.7](../zarrify.md#17-named-inputs-and-layouts)) writes no group `dataset`, and
the root's property has no `layouts`.

## 2. The header

The file's **header** is its first 1024 bytes, in the file's byte order
([§8.1](#81-header)). Its fields, in the order of the file, are those of
the MRC2014 standard (Cheng et al., 2015, and the CCP4 map format), at these
byte offsets. Their types:

| type | value |
|---|---|
| `i32` | a two's complement integer |
| `f32` | an IEEE 754 binary32 number (converted to binary64 exactly) |
| `u8` | an unsigned byte |
| `char[k]` | `k` bytes of text |
| `bytes[k]` | `k` opaque bytes |
| `T[k]` | `k` consecutive values of type `T`, indexed from 0 |

| field | offset | type | meaning |
|---|---|---|---|
| `nx` | 0 | `i32` | columns (the fastest dimension) |
| `ny` | 4 | `i32` | rows |
| `nz` | 8 | `i32` | sections (the slowest dimension) |
| `mode` | 12 | `i32` | the data type ([§3](#3-data-types)) |
| `nxstart` | 16 | `i32` | the first column's index in the unit cell |
| `nystart` | 20 | `i32` | |
| `nzstart` | 24 | `i32` | |
| `mx` | 28 | `i32` | the grid's sampling along x |
| `my` | 32 | `i32` | along y |
| `mz` | 36 | `i32` | along z (a volume stack: the sections per volume) |
| `cella` | 40 | `f32[3]` | the cell's dimensions along x, y and z, in angstroms |
| `cellb` | 52 | `f32[3]` | the cell's angles, in degrees |
| `mapc` | 64 | `i32` | the axis the columns run along: 1 x, 2 y, 3 z |
| `mapr` | 68 | `i32` | the rows' |
| `maps` | 72 | `i32` | the sections' |
| `dmin` | 76 | `f32` | the least value |
| `dmax` | 80 | `f32` | the greatest value |
| `dmean` | 84 | `f32` | the mean value |
| `ispg` | 88 | `i32` | the space group: 0 an image or a stack of images, 1 to 230 a volume, 401 and up a stack of volumes |
| `nsymbt` | 92 | `i32` | the extended header's length in bytes |
| `extra1` | 96 | `bytes[8]` | extra space (IMOD and others write their own fields here) |
| `exttyp` | 104 | `char[4]` | the extended header's type: `CCP4`, `MRCO`, `SERI`, `AGAR`, `FEI1`, `FEI2`, `HDF5`, ... |
| `nversion` | 108 | `i32` | the format's version: 20140 or 20141 |
| `extra2` | 112 | `bytes[84]` | extra space |
| `origin` | 196 | `f32[3]` | the origin along x, y and z, in angstroms |
| `map` | 208 | `char[4]` | `MAP ` |
| `machst` | 212 | `u8[4]` | the machine stamp: byte 0 is `44` for little-endian data, `11` for big-endian |
| `rms` | 216 | `f32` | the values' RMS deviation from the mean |
| `nlabl` | 220 | `i32` | the labels in use |
| `label` | 224 | `char[80][10]` | ten labels of 80 characters |

The header is followed by the **extended header**, `nsymbt` bytes (none when
`nsymbt` is 0), whose layout `exttyp` names and which this convention does
not decode, and then by the **data**: `nx × ny × nz × b` bytes
([§3](#3-data-types)) at offset `1024 + nsymbt`, the column index varying
fastest, then the row, then the section.

## 3. Data types

| `mode` | name | Zarr data type | bytes per value `b` |
|---|---|---|---|
| 0 | INT8 | `int8` | 1 |
| 1 | INT16 | `int16` | 2 |
| 2 | FLOAT32 | `float32` | 4 |
| 3 | COMPLEX_INT16 | `struct` of `real` and `imag`, each `int16` | 4 |
| 4 | COMPLEX64 | `complex64` | 8 |
| 6 | UINT16 | `uint16` | 2 |
| 12 | FLOAT16 | `float16` | 2 |
| 101 | INT4 | `uint8`, packed | ½, each row starting on a byte |

Mode 0 is `int8`, as the MRC2014 standard says, except in an IMOD file
whose bytes are unsigned. When `mode` is 0 and the `i32` at byte 152 (in the
file's byte order; the 41st byte of `extra2`) is 1146047817 (IMOD's stamp,
`IMOD` as a little-endian integer), the data type is `int8` when either
says the bytes are signed: bit 0 of the `i32` at byte 156 (IMOD's flags) is
set ("1 = bytes are stored as signed (the default as of IMOD 4.9)"), or
`nversion` is from 20140 up to, not including, 21000 ("the best way to
indicate that a byte file is signed is simply to conform with the standard
and set the NVERSION field"; both from
[IMOD's MRC format](https://bio3d.colorado.edu/imod/doc/mrc_format.txt)).
Otherwise the bytes are unsigned (IMOD's default before 4.9), and the data
type is `uint8`, `b = 1`. Reading such a file as `int8` would make every
value over 127 negative. IMOD's own test ends `nversion` at the current year
plus 2; a fixed end keeps the output the same whenever a file is zarrified.
(The other flag bits do not change the data type, and neither does the flag
without the stamp; bit 2 is the origin's sign, [§4.2](#42-scales-units-and-position).)
A `complex64` value is its real part, then its imaginary part, each a
binary32 in the file's byte order.

A mode 3 value is its real part, then its imaginary part, each an `int16` in the file's byte
order. Zarr has no complex integer type, so the array's data type is zarr-extensions'
[`struct`](https://github.com/zarr-developers/zarr-extensions/tree/main/data-types/struct) with the
fields `real` and `imag`.

A mode 101 value is 4 bits, unsigned: a row's first value is the low nibble of its first byte, its
second the high nibble, and so on. A row of odd `nx` ends on a padding nibble, so that every row
starts on a byte and has `⌈nx / 2⌉` bytes, and `B = ⌈nx / 2⌉ × ny × nz` ([§8.1](#81-header)).
The order is zarr-extensions'
[`packbits`](https://github.com/zarr-developers/zarr-extensions/tree/main/codecs/packbits), which is
bit packing, not TIFF's run-length PackBits; the array decodes to one `uint8` a value, from 0 to
15. SerialEM writes this mode for K2 and K3 frames.

## 4. The image

The hierarchy is one image at the root ([conventions §4](../conventions.md#4-images)),
without a name, with one array at path `"0"`: the root `zarr.json` is
`{"zarr_format": 3, "node_type": "group", "attributes": A}`, where `A` has
`ome` (`M`, below), `zarr_conventions` and `refkv_zarrified` ([§1](#1-declaration)).

### 4.1 The array

**Kind.** The file is, by `ispg` and `nz`:

| kind | when |
|---|---|
| **volume stack** | `ispg ≥ 401` (the profile has checked that `mz` divides `nz`, [§8.1](#81-header)): `nz / mz` volumes of `mz` sections |
| **image** | `ispg = 0` and `nz = 1` |
| **image stack** | `ispg = 0` and `nz > 1` |
| **volume** | otherwise (any other `ispg`, negative or over 230 included) |

**The axis map.** The sections, rows and columns run along the axes `maps`,
`mapr` and `mapc` name: 1 is `x`, 2 is `y` and 3 is `z`. The map is
**applied** when the three values are a permutation of 1, 2 and 3; then the
sections' axis is named by `maps`, the rows' by `mapr` and the columns' by
`mapc`. Otherwise (a 0, a value out of range, or a value repeated) the
sections' axis is `z`, the rows' `y` and the columns' `x`. The data is
never reordered: the array's axes are the file's dimensions in stored order,
however they are named. (With the standard map `mapc = 1`, `mapr = 2`,
`maps = 3`, the array is `z`, `y`, `x`.)

**Axes.** For an image, the rows' axis then the columns'; for an image stack or a volume, the
sections' axis, the rows' and the columns'; for a volume stack, `t`, then those three. (`t` is
the volume's index in the stack, of type `time`; it is not the acquisition time.)

<a id="type-mrcarray"></a>**`MrcArray`**, the array's `zarr.json`: an
[`ArrayDocument`](../conventions.md#type-arraydocument) with these members:

| member | type | presence | value |
|---|---|---|---|
| `shape` | array of integer | required | `[ny, nx]`, `[nz, ny, nx]` or `[nz / mz, mz, ny, nx]`, in the axes' order |
| `data_type` | string | required | from [§3](#3-data-types) |
| `chunk_grid` | object | required | regular, the chunk shape that [conventions §7](../conventions.md#7-source-values-as-arrays) gives (**Chunks**, below) |
| `fill_value` | number, array of number or object | required | 0, `[0, 0]` for `complex64`, or `{"real": 0, "imag": 0}` for mode 3 |
| `codecs` | array of object | required | `[{"name": "bytes", "configuration": {"endian": E}}]`, `E` the file's byte order, or `[{"name": "bytes"}]` for a one-byte data type (`int8`, and IMOD's `uint8`); for mode 101, `[{"name": "packbits", "configuration": {"padding_encoding": "none", "last_bit": 3}}]`. There is no transpose: the stored order is the array's |
| `dimension_names` | array of string | required | the axes |
| `attributes` | object | required | `{}` ([§1](#1-declaration)) |

**Chunks** ([conventions §7](../conventions.md#7-source-values-as-arrays), contiguous values):
the data is the row-major array `V` of the array's shape, with elements of `b` bytes, at offset
`1024 + nsymbt`. It is cut as contiguous values with the **chunk limit `L = 2^20` bytes**
(1 MiB): blocks of whole volumes or sections when they are small, else blocks of rows of a
section, else runs of a row. The chunk grid, the chunk shape and each chunk's bytes are those
[conventions §7](../conventions.md#7-source-values-as-arrays) gives; an edge chunk along the
cut axis is followed by zero bytes up to the whole chunk.

For example, a `256 × 128` int16 stack of 20 sections (the fixture
`mrc_chunk_sections.mrc`) has sections of 65536 bytes, so `k = ceil(20 /
floor(2^20 / 65536)) = 2` chunks of 10 sections each, the chunk shape
`[10, 128, 256]`. A `1000 × 1100` int8 image of 1100000 bytes, over `L`, is
cut along its rows into 2 chunks of 550 rows, `[550, 1000]`.

**Chunks of mode 101.** The values are cut with elements of one byte, the same limit, as the
safetensors profile cuts a packed tensor ([safetensors.md §8.3](safetensors.md#83-chunks)): when
`nx` is even and each chunk's first value is the first of a byte, each chunk of values `[s, s + m)`
is the bytes `[v + s / 2, v + (s + m) / 2)`, padded with zero bytes to the whole chunk's. Otherwise,
as for an odd `nx`, whose rows are not one run of bits, there is one chunk per row, its chunk shape
1 along every axis but the columns', each the row's `⌈nx / 2⌉` bytes.

### 4.2 Scales, units and position

**Voxel size.** A number is **valid** if it is finite and positive. The
voxel size along each axis is:

| axis | voxel size | when |
|---|---|---|
| `x` | `cella[0] / mx` | `mx ≥ 1`, `cella[0]` is valid and the quotient is valid |
| `y` | `cella[1] / my` | likewise, with `cella[1]` and `my` |
| `z` | `cella[2] / mz` | likewise, with `cella[2]` and `mz` |

An axis without a valid voxel size has no voxel size.

**Scales.** Each axis's scale and unit:

| axis | scale | unit |
|---|---|---|
| `x`, `y`, `z` (each a spatial axis, wherever the axis map put it) | its voxel size, or 1 when it has none | `angstrom` with a voxel size; none without |
| `t` | 1 | none |

**Translation.** For a volume or a volume stack (`ispg` not 0), when the
three `origin` values are finite and at least one is not 0, the image has a
translation ([conventions §5](../conventions.md#5-units)): the position of
the first value, `p[0]` for `x`, `p[1]` for `y`, `p[2]` for `z`, and 0 for `t`
(each only for the axes present). Otherwise it has none: an image's or an image stack's
`origin` is a phase origin in pixels, not a position, and `nxstart`,
`nystart` and `nzstart`, which crystallographic writers use to place a map
in its unit cell, are not applied ([§7](#7-what-the-layout-does-not-express)); all are in `header`.

**The origin's sign.** The position `p` is `origin`, each 0 written `0` and
not `-0`, except in a file whose origin has IMOD's sign, where it is
`-origin`. A file's origin has IMOD's sign when all of these hold:

- the `i32` at byte 152 is IMOD's stamp ([§3](#3-data-types));
- bit 2 (the value 4) of the `i32` at byte 156, IMOD's flags, is clear;
- `nversion` is not a standard version, from 20140 up to, not including, 21000
  ([§3](#3-data-types)).

| stamp | bit 2 | `nversion` | `p` | who writes it |
|---|---|---|---|---|
| yes | set | any | `origin` | IMOD since 4.11 |
| yes | clear | not standard | `-origin` | IMOD before 4.11 |
| yes | clear | standard | `origin` | other writers that keep IMOD's header words |
| no | any | any | `origin` | every other writer |

MRC2014 ([note 10](https://www.ccpem.ac.uk/mrc_format/mrc2014.php)) gives the
position of a subvolume's first pixel in its parent: `origin` is 100, 120 for a
subvolume cut 100 and 120 in. IMOD defines it the other way, as the position
of the parent's origin relative to the first pixel, so that a subvolume has
negative values, and bit 2 says a file is stored with that sign inverted
([IMOD's MRC format](https://bio3d.colorado.edu/imod/doc/mrc_format.txt)).
The first three rows are what IMOD's own reader does, in `mrc_head_read`. It
reads a standard `nversion` with bit 2 clear as the standard's, since IMOD
writes that version only with bit 2 set.
The last row departs from IMOD's reader, which gives a file without the stamp
and without a standard version IMOD's sign. That file may be from any writer,
and UCSF ChimeraX, like MRC2014, reads it as stored.

### 4.3 Channels

`M` has an `"omero"` member, an [`Omero`](../conventions.md#type-omero), when the data type is
not `complex64` or a mode 3 `struct`, and `dmin` and `dmax` are finite with `dmax > dmin`, and in no other case:
`{"channels": [{"label": "C0", "color": "FFFFFF", "active": true, "window":
{"min": dmin, "max": dmax, "start": dmin, "end": dmax}}]}`, one channel
(an [`OmeroChannel`](../conventions.md#type-omerochannel)) whatever the kind of file.

## 5. Source metadata

The root's source metadata `S` ([conventions §2](../conventions.md#2-attributes))
is an object with the members of [§5.1](#51-the-root), in that order,
translated by [conventions §6](../conventions.md#6-source-metadata-as-json).
Everything the file holds is on `refkv_source`, the file's **IR mirror**
([§5.2](#52-the-ir-mirror)), from which the file is rebuilt byte for byte.

### 5.1 The root

<a id="type-mrcroot"></a>**`MrcRoot`**, the root's `S`:

| member | type | presence | value |
|---|---|---|---|
| `byte_order` | string | required | `"little"` or `"big"` |
| `kind` | string | required | `"image"`, `"image_stack"`, `"volume"` or `"volume_stack"` ([§4.1](#41-the-array)) |
| `data_type` | string | required | the Zarr data type of [§3](#3-data-types) (`uint8` for a mode 0 file that [§3](#3-data-types) reads as unsigned, and for mode 101) |
| `header` | [`MrcHeader`](#type-mrcheader) | required | every field of the header |
| `axis_map` | [`MrcAxisMap`](#type-mrcaxismap) | required | whether the axis map named the axes ([§4.1](#41-the-array)) |
| `voxel_size` | [`MrcVoxelSize`](#type-mrcvoxelsize) | optional | the voxel size in angstroms ([§4.2](#42-scales-units-and-position)), present when every axis has one |
| `gzip` | [`MrcGzip`](#type-mrcgzip) | optional | present when the file is gzipped whole ([§8.4](#84-gzipped-files)) |

<a id="type-mrcheader"></a>**`MrcHeader`**: an object with one member per field of
[§2](#2-the-header), in its order and named as there, whose type is the field's (the first row
that matches it applies):

| field's type | member's type | value |
|---|---|---|
| `char[k]` | [`Text`](../conventions.md#type-text) | its bytes up to the first NUL, as a text value; `label` (`char[80][10]`) is an array of ten such values |
| `bytes[k]` | [`Base64`](../conventions.md#type-base64) | the string of its base64 encoding |
| `u8[4]` (`machst`) | array of integer | 4 integers |
| `i32` | [`Integer`](../conventions.md#type-integer) | the field's value |
| `f32` | [`Float`](../conventions.md#type-float) or [`MrcBits`](#type-mrcbits) | the field's value, or its bits when a JSON number or `"NaN"` does not keep them (below) |
| `T[k]` (`cella`, `cellb`, `origin`: the `f32[3]` fields) | array of `T`'s type | `k` values |

<a id="type-mrcbits"></a>**`MrcBits`**, a float (`f32`) value whose bits a JSON number or
`"NaN"` does not keep:

| member | type | presence | value |
|---|---|---|---|
| `bits` | string | required | `H`, its IEEE 754 bits as 8 lowercase hexadecimal digits, most significant first, whatever the file's byte order |

These values are a negative zero (`80000000`), and every NaN but the canonical one
(`7fc00000`), which `"NaN"` stands for. (The header's bytes, those after a text field's first
NUL included, are also the mirror's `header` element.)

<a id="type-mrcaxismap"></a>**`MrcAxisMap`**:

| member | type | presence | value |
|---|---|---|---|
| `applied` | boolean | required | `A`, whether the axis map named the axes ([§4.1](#41-the-array)) |

<a id="type-mrcvoxelsize"></a>**`MrcVoxelSize`**, the voxel size in angstroms
([§4.2](#42-scales-units-and-position)):

| member | type | presence | value |
|---|---|---|---|
| `x` | number | required | `sx`, along `x` |
| `y` | number | required | `sy`, along `y` |
| `z` | number | required | `sz`, along `z` |

`header` holds every field as stored, including those the layout does not
use (`nxstart`, `cellb`, `ispg`, `nversion`, the statistics, the labels), so
that a reader who needs, say, the cell angles or IMOD's flags finds it here.
`kind`, `data_type`, `axis_map` and `voxel_size` are derived from `header`;
they are there so that a reader need not repeat the rules of [§4](#4-the-image).

### 5.2 The IR mirror

`refkv_source` is the file's **IR mirror**
([conventions §11.3](../conventions.md#113-the-ir-mirror)).
The IR's elements (the MRC **source model**) are below; nothing is left out: the
header, the extended header, the data, and every byte after them.

### 5.3 Elements

Paths are relative to the IR's root, whose path is `""`
([conventions §8.1](../conventions.md#81-elements)). Types are those of
[conventions §8.6](../conventions.md#86-types), with `E` the file's byte order
(`<` little, `>` big) and `n` the extended header's length (`nsymbt`).

| path | kind | type | extent: what |
|---|---|---|---|
| `""` | struct | | the root: the whole file, `(0, size)` |
| `header` | value | record | `(0, 1024)`: the header as one record, a field per field of [§2](#2-the-header) in its order and named as there: `Ei4` or `Ef4` for its type (`[k]` for `T[k]`), `u1[4]` for `machst`, `bytes[k]` for `bytes[k]`, `cstr[k]` for `char[k]`, and `{text:cstr[80]}[10]` for `label` |
| `extended` | value | `bytes[n]` | `(1024, n)`, when `n > 0`: the extended header, opaque |
| `data` | data | | `(1024 + n, B)`, `B` the data's size ([§8.1](#81-header)): the data; its form's geometry is `{"data_type": D, "samples": 1}`, with `"bits": 4` for mode 101 ([§3](#3-data-types)), its codec `{"endian": "little" \| "big"}`, and its recipe `[["src", 0, null]]` |
| `gaps/<offset>` | gap | | bytes no other element claims: those after the data |

A file gzipped whole ([§8.4](#84-gzipped-files)) has one element beside the root: `data`, of extent
`(0, size)`, the gzip stream, its codec `{"encoding": "gzip", "endian": "little" \| "big", "skip":
1024 + n}`. The file's bytes do not show the header, so `MrcRoot` is all that holds it.

The view ([conventions §8.7](../conventions.md#87-the-view)) shows the header;
the extended header, opaque bytes, is not shown, and a zarrifier never
reads it.

### 5.4 Equivalence

Hierarchies of one file are compared entry for entry, `refkv_source` included
([conventions §11.4](../conventions.md#114-equivalence)).

## 6. Example

A little-endian volume at `https://example.org/map.mrc` (the fixture
`fixtures/mrc/mrc_volume_float32_le.mrc`) holds a float32 `4 × 3 × 2`
volume in a `6 × 4.5 × 3` angstrom cell (a voxel size of 1.5 angstroms),
with the origin `(-1.5, 2, 7.25)` and the values from −100 to 150. Its root
`zarr.json` has these attributes (`extra2`, 84 zero bytes, elided):

```json
{
  "ome": {
    "version": "0.5",
    "multiscales": [{
      "axes": [
        {"name": "z", "type": "space", "unit": "angstrom"},
        {"name": "y", "type": "space", "unit": "angstrom"},
        {"name": "x", "type": "space", "unit": "angstrom"}
      ],
      "datasets": [{"path": "0", "coordinateTransformations": [
        {"type": "scale", "scale": [1.5, 1.5, 1.5]},
        {"type": "translation", "translation": [7.25, 2.0, -1.5]}
      ]}]
    }],
    "omero": {"channels": [{"label": "C0", "color": "FFFFFF", "active": true,
                            "window": {"min": -100.0, "max": 150.0, "start": -100.0, "end": 150.0}}]}
  },
  "zarr_conventions": [{
    "uuid": "2aacd010-2ed1-4748-bc3c-5dd5e840bbce",
    "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/zarrify/mrc/schema.json",
    "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mrc.md",
    "name": "refkv_zarrified",
    "description": "The Zarr layout of a MRC source zarrified by refkv, and the source's metadata"
  }],
  "refkv_zarrified": {
    "profile": "mrc",
    "version": 0,
    "source": {"url": "https://example.org/map.mrc"},
    "mrc": {
      "byte_order": "little",
      "kind": "volume",
      "data_type": "float32",
      "header": {
        "nx": 4, "ny": 3, "nz": 2, "mode": 2,
        "nxstart": 0, "nystart": 0, "nzstart": 0,
        "mx": 4, "my": 3, "mz": 2,
        "cella": [6.0, 4.5, 3.0], "cellb": [90.0, 90.0, 90.0],
        "mapc": 1, "mapr": 2, "maps": 3,
        "dmin": -100.0, "dmax": 150.0, "dmean": 3.5,
        "ispg": 1, "nsymbt": 0,
        "extra1": "AAAAAAAAAAA=", "exttyp": "", "nversion": 20140, "extra2": "AAAA...",
        "origin": [-1.5, 2.0, 7.25],
        "map": "MAP ", "machst": [68, 68, 0, 0],
        "rms": 12.0, "nlabl": 2,
        "label": ["a float32 volume", "two labels", "", "", "", "", "", "", "", ""]
      },
      "axis_map": {"applied": true},
      "voxel_size": {"x": 1.5, "y": 1.5, "z": 1.5}
    }
  }
}
```

(The labels are shown trimmed; a label's text keeps the spaces that pad it
to 80 characters.) Its array has the chunk shape `[2, 3, 4]`: its 96 bytes
are a single chunk, `0/c/0/0/0`, the range `(1024, 96)` of source 0.

A volume whose columns run along `z` and whose sections run along `x`
(`mapc = 3`, `mapr = 2`, `maps = 1`; the fixture `mrc_axis_map_permuted.mrc`)
has the axes `x`, `y`, `z`, the shape `[nz, ny, nx]`, and the voxel sizes
and origin of `x`, `y` and `z` on those axes.

## 7. What the layout does not express

This section is informative. OME-NGFF 0.5 places an image by a scale and a
translation per axis, names its axes as space, time or channel, and has no
per-index coordinates, so several things an MRC header or extended header
says have no expression in the layout. They are all kept in `header`
([§5.1](#51-the-root)) or in the mirror ([§5.2](#52-the-ir-mirror)), and a
reader that needs them must read them there.

- **The unit cell's angles** (`cellb`). Angles other than 90° make the grid
  non-orthogonal: a shear, which a scale and a translation cannot express.
  The layout uses `cella` and the grid for the scales as if the cell were
  orthogonal; a map of a monoclinic or triclinic crystal is shown skewed.
- **The space group** (`ispg` from 1 to 230) and the symmetry operators of a
  `CCP4` extended header: crystallographic symmetry and the periodicity of
  the map. The layout has no notion of either; `ispg` serves it only to tell
  a volume from a stack.
- **Where a sub-volume sits**. `nxstart`, `nystart` and `nzstart` place the
  map's first voxel in the unit-cell grid, in grid units, and `origin`
  places it in angstroms. Writers differ on which they set, and how, and a
  hierarchy has one translation; the layout applies `origin` for volumes
  only ([§4.2](#42-scales-units-and-position)), as MRC2014 directs for EM
  data, and applies the starts never. A map that an older writer placed by
  its starts alone is shown at the origin. A file IMOD wrote before it had
  a stamp, whose origin has IMOD's sign and nothing to say so, is placed on
  the other side of its first pixel ([§4.2](#42-scales-units-and-position)).
- **What a stack's index means**. The sections of an image stack (`ispg`
  0) are independent images, such as micrographs, a tilt series or
  particles, and the volumes of a volume stack are independent volumes; the
  layout names the one `z` (a space axis) and the other `t` (a time axis)
  because the OME-NGFF axis types offer nothing closer. `kind` says which
  it is.
- **Per-section metadata**. `FEI1`, `FEI2`, `SERI` and `AGAR` extended
  headers hold a record per section (tilt angle, stage position, dose,
  defocus, the pixel size of each frame); the layout does not decode them,
  and OME-NGFF has no place for a value per index anyway.
- **An image's phase origin** (`origin` with `ispg` 0), in pixels: not a
  position.
- **The statistics** `dmean` and `rms`, the labels, the format version
  `nversion` (except that it can decide a mode 0 file's data type,
  [§3](#3-data-types), and the origin's sign,
  [§4.2](#42-scales-units-and-position)), and the extra space (IMOD's own
  fields among them, except the signed-byte flag, [§3](#3-data-types), and
  the origin's sign, [§4.2](#42-scales-units-and-position)).
- **Half-complex transforms**. A mode 4 file is usually a Fourier transform
  of a real volume holding half the spectrum (`nx = N / 2 + 1`); the layout
  gives the data type, `complex64`, not the meaning.

# Part 2. The profile

The MRC profile of [spec/zarrify.md](../zarrify.md) is [§8](#8-mrc-profile) of this document. What every profile's Part 2 shares is in
[spec/zarrify.md, Profiles](../zarrify.md#profiles).

## 8. MRC profile

### 8.1 Header

The input is an MRC2014 file, which its **signature** ([spec/zarrify.md §1.2](../zarrify.md#12-input), tested at the precedence of
[profiles/mrc.json](profiles/mrc.json))
chose: bytes 208–210 of `H` are `4D 41 50` (`MAP`). Its header is the first 1024 bytes of the
file ([the convention §2](#2-the-header)); a file shorter than 1024 bytes
is rejected. The file is **little-endian** if byte 212 (the first byte of
`machst`) is `44`, and **big-endian** if it is `11`; any other value is
rejected. Every field is read in that byte order, at the offset and with
the type that [the convention §2](#2-the-header) gives it. (The second
stamp byte, `44` or `41` for little-endian data in different writers, and
the fourth byte of `map`, a space or a NUL, are not checked.)

**Checks.** The input is rejected unless all of these hold, checked in this
order:

- **Dimensions:** `nx`, `ny` and `nz` are each at least 1.
- **Mode:** `mode` is in the table of [the convention §3](#3-data-types); any
  other value is unknown.
- **Extended header:** `nsymbt` is not negative, and `1024 + nsymbt` is at
  most the file's size.
- **Volume stack:** if `ispg ≥ 401`, `mz` is at least 1 and divides `nz`.
- **Data size:** `B = nx × ny × nz × b` (for mode 101, `⌈nx / 2⌉ × ny × nz`) computed exactly is at most
  2^53 − 1, and `1024 + nsymbt + B` is at most the file's size.

Every check applies whatever the other fields hold. No other field is
checked: the values the layout uses ([the convention §4](#4-the-image)) only change the
output as the convention says, and never reject the input. A file whose `map` field is not `MAP`,
and a file shorter than 212 bytes, are rejected by [§1.2](../zarrify.md#12-input). A gzipped map
is read as [§8.4](#84-gzipped-files) says.

**The extended header** is described ([the convention §5.3](#53-elements))
and never read: the data starts at `1024 + nsymbt` whatever it holds.

### 8.2 Chunks

The data is `B` contiguous bytes at offset `v = 1024 + nsymbt`, the column
index varying fastest: the value at column `i`, row `j` and section `k`
starts at byte `v + ((k × ny + j) × nx + i) × b`. It is the array `V` of
[the convention §4.1](#41-the-array), row-major at offset `v`.

Every chunk of the convention's array is present. The chunk with coords
`c` (in the array's axis order), at `0/c/<coords>`, is the chunk of `V`
with those coords, and is referenced as [conventions §7](../conventions.md#7-source-values-as-arrays)
references a chunk of contiguous values (with the chunk limit 2^20 bytes):
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

<a id="type-mrcchunk"></a>**`MrcChunk`**, the chunk of `V` that starts `o` bytes into the
data and holds `l` of its bytes, padded with `p` zero bytes
(by [conventions §7](../conventions.md#7-source-values-as-arrays), for the chunk at
`(i_0, …, i_{a−1}, q, 0, …, 0)`, `o` is the offset it gives the chunk's bytes, `l = m × s_a`
and `p = (c − m) × s_a`, which is 0 but for an edge chunk along the cut axis):

| part | range | presence | value |
|---|---|---|---|
| the values | source range `(0, v + o, l)` | required | the chunk's bytes in the file |
| the padding | source range `(1, 0, p)` | optional | when `p > 0`: the first `p` bytes of the zero data source |

Where [conventions §7](../conventions.md#7-source-values-as-arrays) copies the chunk, it is
instead `bytes`: the `l` bytes and the `p` zero bytes.

`refkv_source` is the IR mirror ([the convention §5.2](#52-the-ir-mirror)). The
dataset layout ([mrc-dataset.md](mrc-dataset.md)) is written from the same
read: its data variable's chunks are these chunks again, entry for entry,
and its coordinates are deflated index chunks, bytes entries. The archive has no other entries than the
root's and the array's `zarr.json`, the array's chunks, the dataset's
documents and chunks, and the mirror's.

### 8.3 Summary

This section is informative. Both implementations print a one-line JSON
summary, with these members:

| member | value |
|---|---|
| `kind` | `"image"`, `"image_stack"`, `"volume"` or `"volume_stack"` |
| `byteOrder` | `"little"` or `"big"` |
| `mode` | the header's `mode` |
| `dataType` | the array's data type |
| `sizes` | each axis's size |
| `chunkShape` | the array's chunk shape, in its axis order |
| `chunks` | the number of the array's chunks |
| `voxelSize` | `[sx, sy, sz]` when every axis has one, else `null` |
| `translation` | whether the image has one |
| `axisMap` | whether the map was applied |
| `extendedHeader` | `nsymbt` |
| `extendedType` | `exttyp` as text |
| `layouts` | `["dataset"]`, or `[]` without the dataset layout |
| `gzip` | `true`, when the file is gzipped whole; otherwise absent |

### 8.4 Gzipped files

A file gzipped whole is one gzip member (RFC 1952) that holds an MRC file, as the EMDB distributes
its maps (`.map.gz`). This profile reads it ([spec/zarrify.md §1.2](../zarrify.md#12-input),
**Choosing the profile**: the signature of [§8.1](#81-header) holds of what the file's first bytes
inflate to). **`S`** is the stream, those bytes the member holds. Every check of [§8.1](#81-header)
is made of `S` as of a file, but that nothing bounds `1024 + nsymbt + B` but the trailer's check
below, and a file is too short when `S` is.

**Reads.** The file's first bytes, doubling from 64 KiB up to 16 MiB until they inflate to the
first 1024 bytes of `S`, and the last 4 bytes of the file, its trailer's `ISIZE`. A stream whose
start holds less than that is taken to end where it stops. No other byte is inflated: the
extended header is not read, as [§5.3](#53-elements) says.

**The trailer.** The input is rejected when `ISIZE`, the stream's size modulo 2^32, is not `(1024 +
nsymbt + B) mod 2^32`: `the gzip trailer records a stream of <ISIZE> bytes modulo 2^32, not the
<1024 + nsymbt + B> of the header and the data: bytes after the data, or a file of several gzip
members, which a chunk cannot decode`. A `bytes` codec refuses a chunk of another length than the
array's, and several members are what `bgzip` writes: its last trailer records its last block's
size.

**A packed mode.** The input is rejected when the mode is packed ([§3](#3-data-types)) and `nx × bits`
is not a multiple of 8: `a gzipped file of <bits>-bit values and odd nx = <nx>, whose rows each start
on a byte, which one chunk cannot decode`. The rows of such a file are not one run of bits, and the
stream cannot be cut into the rows [§4.1](#41-the-array) would give it.

**The array** is `V` as [§4.1](#41-the-array) says, in **one chunk of the whole array**: its chunk
shape is the array's shape, and its codecs are `bytes` (`packbits` for a packed mode,
[§4.1](#41-the-array)), then:

| codec | when |
|---|---|
| `{"name": "refkv.skip", "configuration": {"length": k}}` | always: `k = 1024 + nsymbt`. It passes over the header and the extended header |
| `{"name": "gzip", "configuration": {"level": 6}}` | always: its level, which decoding ignores, is written as refkv writes its own gzip chunks' |

The dataset layout's data variable has the same codecs and the same chunk.

<a id="type-mrcgzipchunk"></a>**`MrcGzipChunk`**, the array's one chunk, `0/c/0/.../0`:

| part | range | presence | value |
|---|---|---|---|
| the stream | source range `(0, 0, size)` | required | the whole file, which the `gzip` codec decodes |

<a id="type-mrcgzip"></a>**`MrcGzip`**:

| member | type | presence | value |
|---|---|---|---|
| `skip` | integer | required | `1024 + nsymbt` |

**The cost is reads.** Nothing is copied and every value is exact, but the chunk is coarse: a
gzip stream has no index, so a reader inflates the whole stream to read any value, even one slice,
holds the whole array in memory to do so, and reads it with no concurrency. Zarrifying stays cheap,
since only the start of the stream is inflated. A reader that needs random access to a large
volume should decompress the file once and zarrify that, which gives chunks of at most 1 MiB.
