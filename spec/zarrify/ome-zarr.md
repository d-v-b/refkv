# OME-Zarr

The OME-Zarr 0.5 layout of an OME-Zarr 0.4 hierarchy on Zarr v2 storage,
and its attributes. It builds on the [Zarr v2 convention](zarr2.md).
What all of refkv's conventions share is in
[spec/conventions.md](../conventions.md), cited here as "conventions §n". How
refkv produces this layout as a reference store from a listed store is the
OME-Zarr profile, [Part 2](#part-2-the-profile) below.

Convention version: 0 (until release, [conventions §1](../conventions.md#1-conventions)) · UUID: `b74ea302-65bb-49ae-b81f-f9bb52cd4eed` ·
Schema: [schema.json](ome-zarr/schema.json) · Changed 2026-10-26, with no change to any
output: the layout is tabulated, its entries in [Layout](#layout) and its types in type
tables ([conventions §11.5](../conventions.md#115-layout-tables))

This document has two parts ([conventions §11.1](../conventions.md#111-parts)).

# Part 1. The convention

This convention gives a layout only to the hierarchies that meet its
requirements. Where it says that "the input is rejected", or that
something MUST hold, a hierarchy that fails has no layout under this
convention, and the profile rejects it.

The other conventions describe data that is not Zarr. This one is a
different use of refkv: **format migration without copying data**. Its input
is already Zarr: an OME-Zarr 0.4 hierarchy
([OME-NGFF 0.4](https://ngff.openmicroscopy.org/0.4/)) on Zarr v2 storage.
Its output is the same data as OME-Zarr 0.5
([OME-NGFF 0.5](https://ngff.openmicroscopy.org/0.5/)) on Zarr v3. Every
chunk is referenced in place, unchanged; only the metadata is rewritten.
The archive is a few hundred bytes per chunk, and a reader of OME-Zarr 0.5
reads the 0.4 dataset through it without the dataset being copied or
changed.

The convention requires of the input what OME-NGFF 0.4 requires (MUST)
that the output needs to be a correct OME-Zarr 0.5 dataset ([§4](#4-validation)). What 0.4
only recommends (SHOULD) is not required.

Why the chunks can stay where they are: OME-Zarr 0.5 allows every feature
of Zarr v3, "including codecs, chunk grids, chunk key encodings, data types
and storage transformers", unless it disallows one (OME-NGFF 0.5, §1
"Storage format"), and it disallows none of those used here. Zarr v3
defines the `v2` chunk key encoding
([Zarr v3 core specification, chunk key encodings](https://zarr-specs.readthedocs.io/en/latest/v3/chunk-key-encodings/v2/index.html)),
whose keys are exactly Zarr v2's for either separator, and its `transpose`,
`bytes`, `zlib`, `gzip`, `zstd` and `blosc` codecs decode every chunk that
[the Zarr v2 convention §3](zarr2.md#3-arrays) accepts.

## Layout

The hierarchy's entries ([conventions §11.5](../conventions.md#115-layout-tables)), those of
the [Zarr v2 convention's layout](zarr2.md#layout) with this convention's types:

| prefix | type | structure | description |
|---|---|---|---|
| `zarr.json` | group | [`OmeZarrGroup`](#type-omezarrgroup) | the root, when the store's root is a group ([§2](#2-hierarchy)) |
| `zarr.json` | array | [`OmeZarrArray`](#type-omezarrarray) | the root, when the store's root is an array ([§6](#6-arrays)) |
| `zarr.json#/attributes/ome` | attribute | [`OmeZarrOme`](#type-omezarrome) | when the root is an OME group ([§5](#5-group-attributes)) |
| `zarr.json#/attributes/zarr_conventions` | attribute | array of [`ConventionMetadata`](../conventions.md#type-conventionmetadata) | `[C]`, `C` the CMO of [§1](#1-declaration) |
| `zarr.json#/attributes/refkv_zarrified` | attribute | [`RootProperty`](../conventions.md#type-rootproperty) | `profile` `"ome-zarr"`, `version` `0`, `source.url` the store's URL, ending in `/` |
| `zarr.json#/attributes/refkv_zarrified/ome-zarr` | attribute | [`OmeZarrNode`](#type-omezarrnode) | the root's source metadata ([§8.1](#81-nodes)), when it has any |
| `<path>/zarr.json` | group | [`OmeZarrGroup`](#type-omezarrgroup) | every other group, implicit or explicit, OME group or not ([§2](#2-hierarchy)) |
| `<path>/zarr.json` | array | [`OmeZarrArray`](#type-omezarrarray) | every other array ([§6](#6-arrays)) |
| `<path>/zarr.json#/attributes/ome` | attribute | [`OmeZarrOme`](#type-omezarrome) | every other OME group's ([§5](#5-group-attributes)) |
| `<path>/zarr.json#/attributes/zarr_conventions` | attribute | array of [`ConventionMetadata`](../conventions.md#type-conventionmetadata) | `[C]`, `C` the CMO of [§1](#1-declaration), on a node that has source metadata |
| `<path>/zarr.json#/attributes/refkv_zarrified` | attribute | [`NodeProperty`](../conventions.md#type-nodeproperty) | `{"ome-zarr": S}`, on a node that has source metadata ([§1](#1-declaration)) |
| `<path>/zarr.json#/attributes/refkv_zarrified/ome-zarr` | attribute | [`OmeZarrNode`](#type-omezarrnode) | `S`, the node's source metadata ([§8.1](#81-nodes)) |
| `<chunk>` | chunk | [`Zarr2Object`](zarr2.md#type-zarr2object) | each present chunk of the root, when it is an array ([§7](#7-chunks-and-ome-xml)) |
| `<path>/<chunk>` | chunk | [`Zarr2Object`](zarr2.md#type-zarr2object) | each present chunk of the array at `<path>` ([§7](#7-chunks-and-ome-xml)) |
| `OME/METADATA.ome.xml` | object | [`Zarr2Object`](zarr2.md#type-zarr2object) | the OME-XML of a collection at the root, at its own key, when the store has it and its size is not 0 ([§7](#7-chunks-and-ome-xml)) |
| `<path>/OME/METADATA.ome.xml` | object | [`Zarr2Object`](zarr2.md#type-zarr2object) | the OME-XML of the collection at `<path>`, likewise ([§7](#7-chunks-and-ome-xml)) |
| `refkv_source/**` | subtree | [the mirror's entries](../conventions.md#table-mirror) | the store's IR mirror ([§8.2](#82-the-store-ir)), a store IR's, with `ir/held`: every entry under `refkv_source/` but those under `refkv_source/objects/`. Under a root array, plain keys of the archive |
| `refkv_source/zarr.json#/attributes/refkv_zarrified/ome-zarr` | attribute | [`Zarr2Mirror`](zarr2.md#type-zarr2mirror) | `refkv_source`'s source metadata ([§8.2](#82-the-store-ir)) |
| `refkv_source/objects/<k>` | object | [`Zarr2Object`](zarr2.md#type-zarr2object) | each other object, kept whole ([the Zarr v2 convention §5](zarr2.md#5-other-objects)), OME-XML elsewhere than [§7](#7-chunks-and-ome-xml)'s included |

| placeholder | value |
|---|---|
| `<path>` | a node's path, the path of a directory of the store ([the Zarr v2 convention §2](zarr2.md#2-nodes)); it stands for one or more segments |
| `<chunk>` | a chunk key of the array ([the Zarr v2 convention §3.2](zarr2.md#32-chunks)): `0` for an array of no dimensions; otherwise its `n` indexes, in decimal, joined by its separator `s`, so one segment when `s` is `"."` and `n` segments when it is `"/"` |
| `<k>` | the other object's key, its last segment escaped ([the Zarr v2 convention §5](zarr2.md#5-other-objects)); it stands for one or more segments |

## 1. Declaration

The root and `refkv_source` declare the convention
([conventions §11.2](../conventions.md#112-declaration)), the root with its source
metadata ([§8](#8-source-metadata)), if it has any, as the member `"ome-zarr"`, and
`refkv_source` with that of [§8.2](#82-the-store-ir). Every other node that has source
metadata declares it with `{"ome-zarr": S}`. Its CMO is:

```json
{
  "uuid": "b74ea302-65bb-49ae-b81f-f9bb52cd4eed",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/zarrify/ome-zarr/schema.json",
  "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ome-zarr.md",
  "name": "refkv_zarrified",
  "description": "The Zarr layout of a OME-Zarr source zarrified by refkv, and the source's metadata"
}
```

## 2. Hierarchy

The nodes, the arrays and the chunks are [the Zarr v2 convention](zarr2.md)'s. Every rule of [the Zarr v2 convention §2–§3](zarr2.md#2-nodes)
applies, with the same rejections: the candidates and nodes (explicit and
implicit groups, arrays, nothing read inside an array), the `.zgroup`,
`.zarray` and `.zattrs` documents, each array's `zarr.json` (shape, data
type, chunk grid, the `v2` chunk key encoding with the array's separator,
fill value, codecs and attributes), and its chunk keys and chunk objects;
and so do its rules for source metadata ([§4](zarr2.md#4-source-metadata))
and other objects ([§5](zarr2.md#5-other-objects)), except where
this convention says otherwise. The hierarchy has the same keys as the Zarr
v2 convention's hierarchy, except that [§7](#7-chunks-and-ome-xml)'s OME-XML objects keep their own
keys, and the same `zarr.json` documents except for:

- the attributes of OME groups ([§5](#5-group-attributes)), and their source metadata ([§8](#8-source-metadata));
- the member `dimension_names` of image levels ([§6](#6-arrays)).

Every other node, called **not OME** here, is converted by [the Zarr v2 convention](zarr2.md) unchanged:
implicit groups, explicit groups without OME members (such as the row
groups of a plate, or a group of tables next to an image), and arrays that
are not image levels, with their source metadata ([§8](#8-source-metadata)). The objects that are
not part of [the Zarr v2 convention](zarr2.md)'s nodes or chunks
are its other objects, kept whole under `refkv_source/objects/`
([the Zarr v2 convention §5](zarr2.md#5-other-objects)), except
[§7](#7-chunks-and-ome-xml)'s.

**Empty objects, ignored keys and `refkv_source`.** They are
[the Zarr v2 convention §5](zarr2.md#5-other-objects)'s: the empty objects have no entry and
the mirror lists them, and the group `refkv_source` holds the store's IR mirror
([§8.2](#82-the-store-ir)), so a node at `refkv_source`, or below it, rejects the input. The
ignored keys are `refkv_source`'s source metadata ([§8.2](#82-the-store-ir)).

## 3. OME groups

A **relative path** is a string of one or more segments separated by `/`,
none of them empty, `.` or `..`; relative to a group at path `G`, it names
the node at `G` joined with it ([the Zarr v2 convention §2](zarr2.md#2-nodes)). An **image** is an explicit group
whose attributes have the member `multiscales`.

The **OME members** are `multiscales`, `omero`, `labels`, `image-label`,
`plate`, `well` and `bioformats2raw.layout`. An **OME group** is an
explicit group whose attributes have at least one of them, or whose path is
`C/OME` (`OME` when `C` is the root) for a group `C` whose attributes have
`bioformats2raw.layout`, when its attributes have the member `series` (then
`series` is an OME member of that group too: OME-NGFF 0.4 §3.2 puts it
there).

An explicit group whose attributes have a member `ome` rejects the input
(its output would mix an existing `ome` with the converted one, and
OME-NGFF 0.5 requires one version in a hierarchy).

The groups are:

| group | what it is |
|---|---|
| **image** | a group with `multiscales` |
| **labels group** | a group with `labels` |
| **label image** | an image that a labels group lists, or a group with `image-label` |
| **plate** | a group with `plate` |
| **well** | a group with `well`; the **fields** of a well are the images its `images` list |
| **collection** | a group with `bioformats2raw.layout` |

A group may be several of these at once (a bioformats2raw collection is
often a plate too); each applies.

## 4. Validation

Each rule below is a requirement of OME-NGFF 0.4 (section numbers are
OME-NGFF 0.4's) that OME-Zarr 0.5 also has; an input that breaks one is
rejected. "Integer" is an integer of [spec/zarrify.md §1.6](../zarrify.md#16-json-documents). The rules apply to every OME group,
wherever it is in the hierarchy, and a rule's position in this list does
not matter ([spec/zarrify.md §1.2](../zarrify.md#12-input), evaluation order).

**Versions.** The member `version` of a multiscale (an element of
`multiscales`), and of `plate`, `well` and `image-label`, MUST be the
string `"0.4"` when it is present. (It is optional in 0.4: the member is a
SHOULD. Another version is not converted, and 0.5 requires one version in a
hierarchy.)

**Images** (OME-NGFF 0.4 §3.1, §3.3, §3.4). For each image at path `G`:

- I1. `multiscales` is a nonempty array of objects.
- For each multiscale `m`, with `n` its number of axes:
  - I2. `axes` is an array of 2 to 5 objects; each axis's member `name` is
    a string, and the names are distinct.
  - I3. Each axis's member `type` is absent, `null` or a string. The axis
    is a **space** axis if `type` is `"space"`, a **time** axis if it is
    `"time"`, and an **other** axis otherwise (a channel axis, a custom
    type, `null` or no type: 0.4 allows "one additional entry of
    type:channel or a null / custom type"). In order, the axes are at most
    one time axis, then at most one other axis, then 2 or 3 space axes.
  - I4. `datasets` is a nonempty array of objects; each one's member `path`
    is a relative path naming an array (a node of the [Zarr v2 convention §2](zarr2.md#2-nodes), not a group or a
    directory) under `G`, with `n` dimensions.
  - I5. Each dataset's `coordinateTransformations` is an array of one or
    two objects: the first has `type` `"scale"` and `scale`, an array of
    `n` numbers; the second, if there is one, has `type` `"translation"`
    and `translation`, an array of `n` numbers. (So a translation before
    the scale, an `identity`, or a third transformation rejects.)
    Transformations given by `path` instead of a vector (0.4 allows "binary
    data at a location in this container", in a format it does not
    define) are not supported and reject the input.
  - I6. `m`'s own `coordinateTransformations`, if `m` has the member,
    follows I5.
  - I7. The datasets are ordered from highest resolution to lowest (0.4:
    "from largest (i.e. highest resolution) to smallest"): along every
    axis, each dataset's scale is at least the previous dataset's.
- I8. No array is a level of two multiscales whose axis names differ (0.5
  gives each level the `dimension_names` of its image, [§6](#6-arrays), which cannot
  be two lists). Here a multiscale's levels are the arrays its kept datasets
  name (all of them, except for label images, L7).

The member `unit` of an axis is not read: 0.4 says an axis SHOULD have one,
and it SHOULD be a UDUNITS-2 name. The members `name`, `type` and
`metadata` of a multiscale are SHOULDs and are not read either.

**omero** (OME-NGFF 0.4 §3.5). For each OME group with `omero`:

- O1. `omero` is an object whose member `channels` is an array of objects;
  each channel's `color` is a string of 6 hexadecimal digits, and its
  `window` an object whose members `min`, `max`, `start` and `end` are
  numbers.

**Labels** (OME-NGFF 0.4 §3.6, §3.7). For each labels group at path `L` and each group
with `image-label`:

- L1. `labels` is an array of relative paths, each naming an image under
  `L`.
- L2. `image-label` is an object, and its group is an image ("image-label
  groups MUST also contain multiscales metadata").
- L3. `colors`, if present, is an array of objects whose member
  `label-value` is an integer, distinct across `colors`; a color's `rgba`,
  if present, is an array of 4 integers from 0 to 255.
- L4. `properties`, if present, is an array of objects whose member
  `label-value` is an integer.
- L5. `source`, if present, is an object whose member `image`, if present,
  is a string.

For each label image `X`, its **source image** is its `image-label`'s
`source.image` resolved against `X`'s path (segments `..` remove a segment,
`.` and empty ones are skipped), or `../../` resolved the same way if `X`
has no `source.image` (0.4's default):

- L6. A `source.image` that is given names an image (resolved within the
  store).
- L7. If the source image is an image, let `N` be the number of datasets
  of its first multiscale. Each multiscale of `X` has at least `N`
  datasets; fewer rejects the input. A multiscale with more than `N` keeps
  only its first `N` datasets, in order: the others are **dropped**.
  (0.4 and 0.5 both say "the two datasets series MUST have the same number
  of entries". omero-zarr, which wrote the IDR's OME-Zarr 0.4 data, gives
  every label image one level more than its image, so that rule would
  reject every labelled image the IDR publishes; dropping the extra levels
  instead makes the output satisfy 0.5 without losing an array, since the
  dropped ones stay in the hierarchy, [§6](#6-arrays).) When the source is not an
  image, every dataset is kept.
- L8. Every level of `X` that a kept dataset names has an integer data
  type (`int8` to `uint64`; 0.4: "only integer values are supported"; 0.5
  requires it).

The checks of I1–I7 apply to every dataset, dropped or not: a dropped
dataset is still part of a 0.4 document the input must get right.

**Plates and wells** ([conventions §4](../conventions.md#4-images), OME-NGFF 0.4 §3.8, §3.9). For each plate at path `P`:

- P1. `plate` is an object; `columns` and `rows` are arrays of objects
  whose member `name` is a string of one or more ASCII letters and digits,
  the names distinct within each array.
- P2. `wells` is an array of objects; each well's `rowIndex` and
  `columnIndex` are integers indexing `rows` and `columns`, and its `path`
  is the string `<row name>/<column name>` of those two; the path names a
  well (an explicit group with `well`) under `P`.
- P3. `acquisitions`, if present, is an array of objects; each `id` is an
  integer of at least 0, distinct; `maximumfieldcount`, if present, an
  integer of at least 1; `name` and `description`, if present, strings;
  `starttime` and `endtime`, if present, integers.
- P4. `field_count`, if present, is an integer of at least 1, and `name`,
  if present, a string.

For each well at path `W`:

- P5. `well` is an object whose member `images` is an array of objects;
  each one's `path` is a string of one or more ASCII letters and digits,
  the paths distinct, and names an image under `W` (a field); its
  `acquisition`, if present, is an integer.
- P6. When the well is listed by a plate that has `acquisitions`: each
  field's `acquisition`, if present, is the `id` of one of them, and when
  there is more than one acquisition every field has an `acquisition`.

**Collections** (OME-NGFF 0.4 §3.2). For each collection at path `C`:

- B1. `bioformats2raw.layout` is the integer 3.
- B2. If the OME group `C/OME` has `series`: it is an array of relative
  paths, each naming an image under `C`.
- B3. Otherwise, if `C` has no `plate`: the images whose paths are `C`
  joined with a decimal number (no leading zeros) are numbered `0`, `1`,
  …, `k − 1` for some `k ≥ 1` ("consecutively numbered groups starting
  from 0").

## 5. Group attributes

The attributes of an OME group whose `.zattrs` is `A` are the member
`ome`, and the declaration of [§1](#1-declaration) with the group's source metadata ([§8](#8-source-metadata)),
when it has any. `ome` is:

```json
{"version": "0.5", "multiscales": [...], "omero": ..., ...}
```

which has, for each OME member `k` of `A`, the member `k` with `A`'s value
changed only as its type says:

<a id="type-omezarrome"></a>**`OmeZarrOme`**, an OME group's `ome`, its members in this order:

| member | type | presence | value |
|---|---|---|---|
| `version` | string | required | `"0.5"` |
| `multiscales` | array of object | optional | when `A` has it: each multiscale without its member `version` (0.5 has no per-multiscale version; the hierarchy's version is `ome.version`), and, for a multiscale of a label image that has dropped datasets (L7), with `datasets` its first `N` elements, each unchanged (path and `coordinateTransformations` as written). Nothing else of the multiscale changes: its axes and its own `coordinateTransformations` apply to the kept levels as before |
| `omero` | object | optional | when `A` has it: the object without its member `version`, if it has one |
| `labels` | array of string | optional | when `A` has it: unchanged |
| `image-label` | object | optional | when `A` has it: the object without its member `version`, if it has one |
| `plate` | object | optional | when `A` has it: the object without its member `version`, if it has one |
| `well` | object | optional | when `A` has it: the object without its member `version`, if it has one |
| `bioformats2raw.layout` | integer | optional | when `A` has it: unchanged (3) |
| `series` | array of string | optional | when `A` has it, on a collection's `OME` group ([§3](#3-ome-groups)): unchanged |

0.5 dropped the `version` of `omero`, `image-label`, `plate` and `well`: its examples, and its
JSON schemas, have none.

**The inverse.** The 0.4 value that `ome` gives back for each of its
members `k` but `version` is, when `k` is not **unversioned** (listed in
the group's source metadata `U`, [§8](#8-source-metadata)):

| `k` | the value given back |
|---|---|
| `multiscales` | each multiscale with the member `"version": "0.4"` added (0.4 asks each multiscale for one, as a SHOULD) |
| `omero`, `image-label`, `plate` and `well` | the object with the member `"version": "0.4"` added (0.4 asks `image-label`, `plate` and `well` for one, as a SHOULD, and its `omero` example has one) |
| `labels`, `bioformats2raw.layout` and `series` | the value (0.4 gives these no version) |

and, when `k` is unversioned, the value of `ome`'s member unchanged (each
multiscale, or the object, without a `version`).

A member `k` of `A` is unversioned when it is:

| `k` | unversioned when |
|---|---|
| `multiscales` | no multiscale has the member `version`, and none of them has dropped datasets (L7) |
| `omero`, `image-label`, `plate` or `well` | the object has no member `version` |

An OME member of `A` is **given back** when the inverse of its member of
`ome` equals it, as a JSON value. As `ome` differs from `A` only by the
`version` members and the dropped datasets, this is so exactly when:

| `k` | given back when |
|---|---|
| `multiscales` | none of the multiscales has dropped datasets (L7), and either every multiscale has the member `version` and it is the string `"0.4"`, or none has the member `version` (it is unversioned) |
| `omero`, `image-label`, `plate` and `well` | the object has the member `version` and it is the string `"0.4"`, or it has no member `version` (it is unversioned) (for `image-label`, `plate` and `well`, [§4](#4-validation) allows only `"0.4"` or no version; for `omero`, which [§4](#4-validation) does not check, any other value is not given back) |
| `labels`, `bioformats2raw.layout` and `series` | always |

A member that is not given back (one with another `omero` version,
`multiscales` of which only some have a `version`, or `multiscales` with
dropped datasets) is kept whole, as written, in the group's attributes in
its source metadata ([§8](#8-source-metadata));
so is every member of `A` that is not an OME member (for example
`_creator`, or a user's own). So the group's `.zattrs` is recovered as the
inverse of each member of `ome`, with the members of the kept attributes
set over them. A group that is not an OME group keeps its whole `.zattrs`,
as in the [Zarr v2 convention](zarr2.md#4-source-metadata).

<a id="type-omezarrgroup"></a>**`OmeZarrGroup`**, a group's `zarr.json`: a
[`Zarr2Group`](zarr2.md#type-zarr2group) with these members:

| member | type | presence | value |
|---|---|---|---|
| `zarr_format` | integer | required | `3` |
| `node_type` | string | required | `"group"` |
| `attributes` | object | required | for an OME group, the member `ome`, an [`OmeZarrOme`](#type-omezarrome), first; then, on the root and on a group that has source metadata ([§8](#8-source-metadata)), the members of a [`Declaration`](../conventions.md#type-declaration) of this convention ([§1](#1-declaration)); `{}` for a group that is neither |

## 6. Arrays

OME-NGFF 0.5 requires every level of an image to have `dimension_names`
equal to the image's axis names ([conventions §3](../conventions.md#3-arrays) of 0.5: "MUST be included in the
zarr.json of the Zarr array of a multiscale level and MUST match the names
in the axes metadata"). Each array that is a level of a multiscale (the
array a kept dataset's path names, label images included) gets the member
`"dimension_names"`: that multiscale's axis names, in order. By I8 every
multiscale of which it is a level gives the same names. No other array gets
`dimension_names`; in particular the arrays of dropped datasets (L7) stay in
the hierarchy as plain arrays, converted by [the Zarr v2 convention](zarr2.md) alone (nothing in 0.5 makes
an array that is not a level carry dimension names). The array's attributes are
its source metadata ([§8](#8-source-metadata)).

<a id="type-omezarrarray"></a>**`OmeZarrArray`**, an array's `zarr.json`: a
[`Zarr2Array`](zarr2.md#type-zarr2array) with these members:

| member | type | presence | value |
|---|---|---|---|
| `dimension_names` | array of string | optional | present only for a level of a multiscale: that multiscale's axis names, in order; after `codecs` |
| `attributes` | object | required | the members of a [`Declaration`](../conventions.md#type-declaration) of this convention ([§1](#1-declaration)), with the array's source metadata ([§8](#8-source-metadata)), when it has any (and on the root, which always declares the convention); otherwise `{}` |

## 7. Chunks and OME-XML

The chunks are the [Zarr v2 convention](zarr2.md#32-chunks)'s,
under their own keys: the arrays keep the `v2` chunk key encoding with
their own separator.

A bioformats2raw collection SHOULD hold its OME-XML metadata in
`OME/METADATA.ome.xml`, which OME-Zarr 0.5 keeps at the same place. For each
collection `C`, the object `C/OME/METADATA.ome.xml` (`OME/METADATA.ome.xml`
for the root), if the store has it and its size is not 0, is in the
hierarchy under that key, as the object is. It is not read. (An empty one
holds nothing: it is one of [the Zarr v2 convention §5](zarr2.md#5-other-objects)'s empty objects,
which the mirror lists.) (The
group `OME` itself is a node only if it has a `.zgroup`; the object is kept
either way.) Every other object that is not a node's document or a chunk,
OME-XML elsewhere included, is one of the
[Zarr v2 convention](zarr2.md#5-other-objects)'s other objects,
kept whole under `refkv_source/objects/`.

## 8. Source metadata

### 8.1 Nodes

A node's source metadata `S` ([conventions §2](../conventions.md#2-attributes)) is
the [Zarr v2 convention](zarr2.md#4-source-metadata)'s,
`{"attributes": A, "metadata": M}`, with a third member for an OME group,
`{"attributes": A, "metadata": M, "unversioned": U}`, each member present
only when it is not empty, with the same `M` (the members of `.zgroup` or
`.zarray` that the `zarr.json` does not reproduce) and the same rule for
numbers (an integer literal is kept exactly), except that the attributes
`A` of an OME group are only the members of its `.zattrs` that [§5](#5-group-attributes) does not
give back: `ome` ([§5](#5-group-attributes)) is a translation, and `A` keeps what it cannot give
back. `U` is the array of the names of the group's unversioned members
([§5](#5-group-attributes)), in the order `multiscales`, `omero`, `image-label`, `plate`,
`well`: so a member that 0.4 allowed to have no version is given back
from `ome` by name, not copied whole. (`U` is not a member of `M`, whose
members are the `.zgroup`'s own.) Every other node's `A` is its whole
`.zattrs`. A node with none of them has no source metadata; in particular
an OME group whose `.zattrs` has only OME members of version `"0.4"` (a
typical image or well), and whose `.zgroup` has only `zarr_format`, has
none, and does not declare the convention unless it is the root.

<a id="type-omezarrnode"></a>**`OmeZarrNode`**, a node's `S`:

| member | type | presence | value |
|---|---|---|---|
| `attributes` | object | optional | `A`, present when not empty: for an OME group, the members of its `.zattrs` that [§5](#5-group-attributes) does not give back, as written; for every other node, its whole `.zattrs`, as the Zarr v2 convention's [`Zarr2Node`](zarr2.md#type-zarr2node) has it |
| `metadata` | [`Zarr2Metadata`](zarr2.md#type-zarr2metadata) | optional | `M`, present when not empty: the members of `.zgroup` or `.zarray` that the `zarr.json` does not reproduce, as the Zarr v2 convention's |
| `unversioned` | array of string | optional | `U`, an OME group's, present when not empty: the names of its unversioned members ([§5](#5-group-attributes)), in the order `multiscales`, `omero`, `image-label`, `plate`, `well` |

### 8.2 The store IR

`refkv_source` is the store's IR mirror, as for the Zarr v2 convention: the store's IR is
[its §4.1](zarr2.md#41-elements)'s, element for element (the listing, each node document held
as a value per top-level member, every other object referenced or empty), and its view
`refkv_source/tree`. `refkv_source` declares this convention with the source metadata
`{"ir": D, "ignored": I}` (that is, `{"ome-zarr": {"ir": D, "ignored": I}}`), a
[`Zarr2Mirror`](zarr2.md#type-zarr2mirror): `D` the mirror's description
([conventions §8.9](../conventions.md#89-stores)), `I` the ignored keys of
[the Zarr v2 convention §5](zarr2.md#5-other-objects) in ascending order of their UTF-8 bytes,
present only when not empty. The OME layer adds no element: what
it reads is in the node documents the mirror holds. Hierarchies of one store are compared
entry for entry, `refkv_source` included
([conventions §11.4](../conventions.md#114-equivalence)).

## 9. Not checked

This section is informative. This convention does not check:

- what 0.4 says SHOULD (or MAY) be: units, axis types beyond I3, the names
  and `type` of multiscales, `version` members, `field_count` and `name` of
  a plate, every well and label being listed, labels' `colors` existing, and
  the advice on row and column names on case-insensitive file systems;
- what 0.5 requires that 0.4 does not: that groups between a labels group
  and its label images hold no metadata;
- what neither version makes checkable from metadata alone: that each
  dimension of a label equals its image's or is 1, that an image's levels
  shrink in shape as their scales grow, that a field's acquisition count
  matches `maximumfieldcount`, and the OME-XML (`Image` elements matching
  the series).

## 10. Example

The root of the store `https://example.org/i.zarr/`, an image whose multiscale has
`"version": "0.4"`, which `ome` gives back, so that the root has no source
metadata:

```json
{
  "zarr_format": 3,
  "node_type": "group",
  "attributes": {
    "ome": {
      "version": "0.5",
      "multiscales": [
        {
          "axes": [
            {
              "name": "angle",
              "type": "angle",
              "unit": "degree"
            },
            {
              "name": "y",
              "type": "space"
            },
            {
              "name": "x",
              "type": "space"
            }
          ],
          "datasets": [
            {
              "path": "s0",
              "coordinateTransformations": [
                {
                  "type": "scale",
                  "scale": [
                    15.0,
                    1.0,
                    1.0
                  ]
                }
              ]
            }
          ]
        }
      ]
    },
    "zarr_conventions": [
      {
        "uuid": "b74ea302-65bb-49ae-b81f-f9bb52cd4eed",
        "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/zarrify/ome-zarr/schema.json",
        "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ome-zarr.md",
        "name": "refkv_zarrified",
        "description": "The Zarr layout of a OME-Zarr source zarrified by refkv, and the source's metadata"
      }
    ],
    "refkv_zarrified": {
      "profile": "ome-zarr",
      "version": 0,
      "source": {
        "url": "https://example.org/i.zarr/"
      }
    }
  }
}
```

Its child `nested`, an image whose multiscale has no `version` (0.4 allows
that), so that the multiscale is unversioned: `ome` gives it back as it is,
and the source metadata names it rather than copying it:

```json
{
  "zarr_format": 3,
  "node_type": "group",
  "attributes": {
    "ome": {
      "version": "0.5",
      "multiscales": [
        {
          "axes": [
            {
              "name": "k",
              "type": null
            },
            {
              "name": "y",
              "type": "space"
            },
            {
              "name": "x",
              "type": "space"
            }
          ],
          "datasets": [
            {
              "path": "0",
              "coordinateTransformations": [
                {
                  "type": "scale",
                  "scale": [
                    1,
                    1,
                    1
                  ]
                }
              ]
            }
          ]
        }
      ]
    },
    "zarr_conventions": [
      {
        "uuid": "b74ea302-65bb-49ae-b81f-f9bb52cd4eed",
        "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/zarrify/ome-zarr/schema.json",
        "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ome-zarr.md",
        "name": "refkv_zarrified",
        "description": "The Zarr layout of a OME-Zarr source zarrified by refkv, and the source's metadata"
      }
    ],
    "refkv_zarrified": {
      "ome-zarr": {
        "unversioned": [
          "multiscales"
        ]
      }
    }
  }
}
```

# Part 2. The profile

The OME-Zarr profile of [spec/zarrify.md](../zarrify.md) (revision 16) is [§11](#11-ome-zarr-profile) of this document. What every profile's Part 2 shares is in
[spec/zarrify.md, Profiles](../zarrify.md#profiles). The profile builds on
the Zarr v2 profile ([zarr2.md](zarr2.md#part-2-the-profile), [zarr2.md §7](zarr2.md#7-zarr-v2-profile)).

## 11. OME-Zarr profile

The profile was chosen because the store's root has a `.zarray` or a
`.zgroup` and the store declares OME-NGFF 0.4 ([§1.4](../zarrify.md#14-store-inputs)). The store is read
as [zarr2.md §7](zarr2.md#7-zarr-v2-profile) reads it, integer literals included: its node documents in one
round; then each document the test of OME-NGFF 0.4 names that the round did not read (one
inside an array, say), one round each, a document the test cannot parse rejecting the input,
and one that cannot be read failing the zarrifier, as in [zarr2.md §7.2](zarr2.md#72-chunks-and-other-objects).
The choice between this profile and [zarr2.md §7](zarr2.md#7-zarr-v2-profile) reads the same rounds.

### 11.1 Rejection

The input is rejected when a rule of [zarr2.md §7](zarr2.md#7-zarr-v2-profile) rejects it, and when the
convention gives it no layout: wherever it says that the input is
rejected, or that something MUST hold and it does not (in particular, a
rule of [its §4](#4-validation)).

### 11.2 Chunks and OME-XML

Chunks are [zarr2.md §7](zarr2.md#7-zarr-v2-profile)'s: each nonempty chunk object is an entry under its own key,
referencing the whole object through its own `url` source ([§1.4](../zarrify.md#14-store-inputs)). Each
OME-XML object of [the convention §7](#7-chunks-and-ome-xml)
is referenced whole in the same way: an entry under its key, with its own
source, in the order of [§1.4](../zarrify.md#14-store-inputs) among the chunk entries (an object of size 0
has no entry; it is an empty object, which the mirror lists, as an empty chunk object is).
Every other object is as [zarr2.md §7.2](zarr2.md#72-chunks-and-other-objects) says; the ignored keys are
`refkv_source`'s source metadata ([the convention §8.2](#82-the-store-ir)).

### 11.3 Summary

This section is informative. Both implementations print a one-line JSON
summary, with these members:

| member | value |
|---|---|
| `groups` | the groups, explicit and implicit |
| `arrays` | the arrays |
| `chunks` | the chunk entries |
| `emptyChunks` | the chunk objects of size 0 |
| `objects` | the store's objects |
| `images` | the images |
| `labels` | the label images |
| `droppedLabelLevels` | the datasets L7 drops, over all label images |
| `plates` | the plates |
| `wells` | the wells |
| `fields` | the images wells list |
| `omeXml` | the OME-XML entries |
| `otherObjects` | the entries under `refkv_source/objects/` |
| `listingRequests` | the listing's requests |

and the Rust core's counts ([zarr2.md §7.3](zarr2.md#73-summary)).

