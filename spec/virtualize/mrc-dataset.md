# MRC as a dataset

The dataset layout ([conventions §9](../conventions.md#9-dataset-layouts))
of an MRC2014 file: the group `dataset` that the MRC convention's
hierarchy ([mrc.md](mrc.md)) also holds, which xarray opens as a dataset
with named dimensions and coordinates in angstroms. It is written by the
MRC profile ([mrc.md §14](mrc.md#14-mrc-profile)) beside the image layout,
from the same read of the file, and references the same bytes. What all of
refkv's conventions share is in [spec/conventions.md](../conventions.md),
cited here as "conventions §n"; "the MRC convention §n" is a section of
[mrc.md](mrc.md).

Convention version: 0 (until release, [conventions §1](../conventions.md#1-conventions)) · UUID: `f139bf4e-7bab-4c3c-9b2d-66ba747f9b93` ·
Schema: [schema.json](mrc-dataset/schema.json)

## 1. Declaration

The group `dataset`, a child of the root, declares the convention by
[conventions §2](../conventions.md#2-attributes) and [§9](../conventions.md#9-dataset-layouts),
with `"profile": "mrc-dataset"`, `"version": 0`, `"revision": 28`, the
file's URL as `source.url`, and the source metadata of [§4](#4-source-metadata)
as the member `"mrc-dataset"`. Its CMO is:

```json
{
  "uuid": "f139bf4e-7bab-4c3c-9b2d-66ba747f9b93",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/virtualize/mrc-dataset/schema.json",
  "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mrc-dataset.md",
  "name": "refkv_virtualized",
  "description": "The Zarr layout of a MRC dataset source virtualized by refkv, and the source's metadata"
}
```

The root of the hierarchy, which declares the MRC convention, names this
group as `"layouts": {"dataset": "dataset"}` ([conventions §2](../conventions.md#2-attributes)).
The group's arrays have no attributes but those of [§2](#2-the-data-variable)
and [§3](#3-coordinates).

## 2. The data variable

`dataset/data` holds the file's values: the array of
[the MRC convention §4.1](mrc.md#41-the-array), with the same shape, data
type, chunk grid, codecs and fill value, and the same chunks, each the
same reference as the image layout's chunk at the same coords, or the same
copied bytes ([the MRC convention §14.2](mrc.md#142-chunks)). Its
dimensions are, by the kind of file, with `S`, `R` and `C` the names the
axis map gives the sections, the rows and the columns
([the MRC convention §4.1](mrc.md#41-the-array): `z`, `y`, `x` for the
standard map):

| kind | `dimension_names` |
|---|---|
| image | `[R, C]` |
| image stack | `["section", R, C]` |
| volume | `[S, R, C]` |
| volume stack | `["volume", S, R, C]` |

The sections of an image stack are independent images and the volumes of a
volume stack independent volumes, so their index dimensions say so rather
than borrowing a spatial or temporal name.

Its attributes are `{"actual_range": [dmin, dmax]}` when the data type is
not `complex64`, and `dmin` and `dmax` are finite with `dmax > dmin` (the
case in which the image layout has a display window,
[the MRC convention §4.3](mrc.md#43-channels)), and `{}` otherwise.

## 3. Coordinates

Each of the dimensions `x`, `y` and `z` that the data variable has, and
along which the file has a **voxel size** `s`
([the MRC convention §4.2](mrc.md#42-scales-units-and-position)), has the
coordinate variable `dataset/<a>`: a 1-D `float64` array of the
dimension's length `n`, little-endian, chunk shape `[min(n, 2^17)]`, with
the attribute `{"units": "angstrom"}` ([conventions §9](../conventions.md#9-dataset-layouts)).
Its value at index `i` is `o + i × s` in binary64 (the product first,
then the sum), where `o` is the origin along that axis when the image
layout has a translation ([the MRC convention §4.2](mrc.md#42-scales-units-and-position):
a volume or a volume stack whose `origin` is finite and not all 0), and 0
otherwise. A dimension without a voxel size, and the index dimensions
`section` and `volume`, have no coordinate variable.

## 4. Source metadata

The group's source metadata `"mrc-dataset"` is the object of
[the MRC convention §5.1](mrc.md#51-the-root), member for member
(`byte_order`, `kind`, `data_type`, `header`, `axis_map`, `voxel_size`),
so that the group describes its source by itself. The file's IR mirror is
the root's `refkv_source` ([the MRC convention §5.2](mrc.md#52-the-ir-mirror));
the group has none.

## 5. Example

The volume of [the MRC convention §6](mrc.md#6-example) (the fixture
`fixtures/mrc/mrc_volume_float32_le.mrc`, `4 × 3 × 2` float32 voxels of
1.5 angstroms, origin `(-1.5, 2, 7.25)`) has, beside its image layout, the
group `dataset` with the arrays `data` (`[2, 3, 4]`, dimensions `z`, `y`,
`x`, the chunk `dataset/data/c/0/0/0` the range `(1024, 96)` of source 0,
as `0/c/0/0/0` is), `z` (`[7.25, 8.75]`), `y` (`[2, 3.5, 5]`) and `x` (`[-1.5, 0, 1.5, 3]`), each with `{"units": "angstrom"}`, and in xarray:

```
<xarray.Dataset>
Dimensions:  (z: 2, y: 3, x: 4)
Coordinates:
  * z        (z) float64 7.25 8.75
  * y        (y) float64 2.0 3.5 5.0
  * x        (x) float64 -1.5 0.0 1.5 3.0
Data variables:
    data     (z, y, x) float32 ...
```

An image stack of 20 sections with no `cella` along z (the fixture
`mrc_chunk_sections.mrc`) has `data` with the dimensions `section`, `y`,
`x`, coordinates `y` and `x`, and no coordinate for `section`.
