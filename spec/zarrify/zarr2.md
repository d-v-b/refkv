# Zarr v2

The Zarr v3 layout of a Zarr v2 hierarchy, and its attributes. What all of
refkv's conventions share is in [spec/conventions.md](../conventions.md), cited
here as "conventions §n". How refkv produces this layout as a reference store
from a listed store is the Zarr v2 profile,
[Part 2](#part-2-the-profile) below. The
[OME-Zarr convention](ome-zarr.md) builds on this one.

Convention version: 0 (until release, [conventions §1](../conventions.md#1-conventions)) · UUID: `8e792619-d671-4687-ab51-752885dd3ee6` ·
Schema: [schema.json](zarr2/schema.json)

Changed 2026-10-27: a zlib stream's codec is `numcodecs.zlib`, zarr-python's name for it, in
place of `zlib` ([conventions §3](../conventions.md#3-arrays), d-v-b/refkv#90).

Changed 2026-10-26, with no change to any output: the layout is tabulated, its entries in
[Layout](#layout) and its types in type tables
([conventions §11.5](../conventions.md#115-layout-tables)).

Changes before 2026-10-22 are recorded in
[the revision log of VIRTUALIZE.md](../history/virtualize/REVISIONS.md), this document's earlier
name, up to
[revision 29](../history/virtualize/REVISIONS.md#revision-29-no-recorded-revision-the-profile-registry).

This document has two parts ([conventions §11.1](../conventions.md#111-parts)).

# Part 1. The convention

This convention gives a layout only to the hierarchies that meet its
requirements. Where it says that "the input is rejected", or that
something MUST hold, a hierarchy that fails has no layout under this
convention, and the profile rejects it.

A Zarr v2 hierarchy (the Zarr storage specification, version 2) is a tree of
groups and arrays: a group has a `.zgroup` document, an array a `.zarray`
document, and either may have a `.zattrs` document of user attributes. An
array's chunks are objects at keys `<array>/<chunk key>`. The output is a
Zarr v3 hierarchy with a node at the path of every Zarr v2 node; each
array's chunks are its chunk objects, referenced whole, under the same keys
(the Zarr v3 `v2` chunk key encoding produces exactly the Zarr v2 keys).
Each node's attributes, and the members of its documents that the Zarr v3
metadata does not reproduce, are its source metadata ([§4](#4-source-metadata)); the store's
listing and every document read, byte for byte, are its IR mirror on
`refkv_source` ([§4.1](#41-elements)). Every other object of the store, but
consolidated metadata, is kept whole ([§5](#5-other-objects)).

## Layout

The hierarchy's entries ([conventions §11.5](../conventions.md#115-layout-tables)):

| prefix | type | structure | description |
|---|---|---|---|
| `zarr.json` | group | [`Zarr2Group`](#type-zarr2group) | the root, when the store's root is a group ([§2](#2-nodes)) |
| `zarr.json` | array | [`Zarr2Array`](#type-zarr2array) | the root, when the store's root is an array ([§3](#3-arrays)) |
| `zarr.json#/attributes/zarr_conventions` | attribute | array of [`ConventionMetadata`](../conventions.md#type-conventionmetadata) | `[C]`, `C` the CMO of [§1](#1-declaration) |
| `zarr.json#/attributes/refkv_zarrified` | attribute | [`RootProperty`](../conventions.md#type-rootproperty) | `profile` `"zarr2"`, `version` `0`, `source.url` the store's URL, ending in `/` |
| `zarr.json#/attributes/refkv_zarrified/zarr2` | attribute | [`Zarr2Node`](#type-zarr2node) | the root's source metadata ([§4](#4-source-metadata)), when it has any |
| `<path>/zarr.json` | group | [`Zarr2Group`](#type-zarr2group) | every other group, implicit or explicit ([§2](#2-nodes)) |
| `<path>/zarr.json` | array | [`Zarr2Array`](#type-zarr2array) | every other array ([§3](#3-arrays)) |
| `<path>/zarr.json#/attributes/zarr_conventions` | attribute | array of [`ConventionMetadata`](../conventions.md#type-conventionmetadata) | `[C]`, `C` the CMO of [§1](#1-declaration), on a node that has source metadata |
| `<path>/zarr.json#/attributes/refkv_zarrified` | attribute | [`NodeProperty`](../conventions.md#type-nodeproperty) | `{"zarr2": S}`, on a node that has source metadata ([§1](#1-declaration)) |
| `<path>/zarr.json#/attributes/refkv_zarrified/zarr2` | attribute | [`Zarr2Node`](#type-zarr2node) | `S`, the node's source metadata ([§4](#4-source-metadata)) |
| `<chunk>` | chunk | [`Zarr2Object`](#type-zarr2object) | each present chunk of the root, when it is an array ([§3.2](#32-chunks)) |
| `<path>/<chunk>` | chunk | [`Zarr2Object`](#type-zarr2object) | each present chunk of the array at `<path>` ([§3.2](#32-chunks)) |
| `refkv_source/**` | subtree | [the mirror's entries](../conventions.md#table-mirror) | the store's IR mirror ([§4.1](#41-elements)), a store IR's, with `ir/held`: every entry under `refkv_source/` but those under `refkv_source/objects/`. Under a root array, plain keys of the archive ([§5](#5-other-objects)) |
| `refkv_source/zarr.json#/attributes/refkv_zarrified/zarr2` | attribute | [`Zarr2Mirror`](#type-zarr2mirror) | `refkv_source`'s source metadata ([§5](#5-other-objects)) |
| `refkv_source/objects/<k>` | object | [`Zarr2Object`](#type-zarr2object) | each other object, kept whole ([§5](#5-other-objects)) |

| placeholder | value |
|---|---|
| `<path>` | a node's path, the path of a directory of the store ([§2](#2-nodes)); it stands for one or more segments |
| `<chunk>` | a chunk key of the array ([§3.2](#32-chunks)): `0` for an array of no dimensions; otherwise its `n` indexes, in decimal, joined by its separator `s`, so one segment when `s` is `"."` and `n` segments when it is `"/"` |
| `<k>` | the other object's key, its last segment escaped ([§5](#5-other-objects)); it stands for one or more segments |

## 1. Declaration

The root and `refkv_source` declare the convention
([conventions §11.2](../conventions.md#112-declaration)), the root with its source
metadata ([§4](#4-source-metadata)), if it has any, as the member `"zarr2"`. Every
other node that has source metadata declares it with `{"zarr2": S}`. Its CMO is:

```json
{
  "uuid": "8e792619-d671-4687-ab51-752885dd3ee6",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/zarrify/zarr2/schema.json",
  "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md",
  "name": "refkv_zarrified",
  "description": "The Zarr layout of a Zarr v2 source zarrified by refkv, and the source's metadata"
}
```

## 2. Nodes

The **candidates** are the directories `D` (the root included) for which
the store has an object `D/.zarray` or `D/.zgroup`. A candidate with both
rejects the input. Candidates are classified from the root down (in order
of their number of segments):

- a candidate inside an array (a proper descendant of a directory that is
  an array) is **not a node**, and none of its documents is read or
  checked;
- otherwise, a candidate with `.zarray` is an **array**, and one with
  `.zgroup` an **explicit group**.

Every directory that is a proper ancestor of an array or an explicit
group, and is not one itself, is an **implicit group**. No other directory
is a node.

A node's documents are read as JSON ([spec/zarrify.md §1.6](../zarrify.md#16-json-documents)):

| document | MUST be |
|---|---|
| `.zgroup` | an object whose member `zarr_format` is the integer 2. Its other members are kept in the group's source metadata ([§4](#4-source-metadata)) |
| `.zarray` | [§3](#3-arrays) |
| `.zattrs`, if the node has one | an object: the node's **attributes**, `{}` if it has none. A `.zattrs` of a directory that is not an array or an explicit group is not read: it is one of [§5](#5-other-objects)'s other objects |

**Consolidated metadata.** A `.zmetadata` object (consolidated metadata) is
never read: nodes come from the listing and documents from their own
objects, so stale consolidated metadata cannot change the output. (Chunks
must come from the listing anyway, since `.zmetadata` does not list them.)
It is kept as an other object ([§5](#5-other-objects)), so that consolidated metadata that
differs from the documents is not lost.

The hierarchy has one `zarr.json` per node, at `<path>/zarr.json`
(`zarr.json` for the root). An array's is [§3](#3-arrays)'s
[`Zarr2Array`](#type-zarr2array); a group's, implicit or explicit, is
`{"zarr_format": 3, "node_type": "group", "attributes": A}`:

<a id="type-zarr2group"></a>**`Zarr2Group`**, a group's `zarr.json`:

| member | type | presence | value |
|---|---|---|---|
| `zarr_format` | integer | required | `3` |
| `node_type` | string | required | `"group"` |
| `attributes` | object | required | `A`: the members of a [`Declaration`](../conventions.md#type-declaration), the declaration of [§1](#1-declaration), or `{}` for a node without source metadata (but the root, which always declares the convention) |

## 3. Arrays

An array's `.zarray` MUST be an object with these members, or the input is
rejected:

| member | value |
|---|---|
| `zarr_format` | the integer 2 |
| `shape` | an array of `n` integers ([spec/zarrify.md §1.6](../zarrify.md#16-json-documents)), `0 ≤ n ≤ 32`, each from 0 to 2^53 − 1 |
| `chunks` | an array of `n` integers, each from 1 to 2^53 − 1 |
| `dtype` | a string in the table below |
| `order` | `"C"` or `"F"` |
| `compressor` | `null` or absent (no compression), or an object whose member `id` is a string ([§3.1](#31-compressors)) |
| `filters` | `null`, absent, or an empty array. A nonempty array rejects the input (no filter is supported) |
| `fill_value` | see below; absent is the same as `null` |
| `dimension_separator` | `"."`, `"/"`, `null` or absent (`"."`) |

Other members are not read here; they are kept in the array's source
metadata ([§4](#4-source-metadata)).

**Data types.** `dtype` is a NumPy type string: a byte order character
(`<` little-endian, `>` big-endian, `|` not applicable), a kind and a size
in bytes.

| `dtype` | Zarr `data_type` | `b` |
|---|---|---|
| `\|b1`, `<b1`, `>b1` | `bool` | 1 |
| `\|i1`, `<i1`, `>i1`; `\|u1`, `<u1`, `>u1` | `int8`; `uint8` | 1 |
| `<i2`, `>i2`; `<u2`, `>u2`; `<f2`, `>f2` | `int16`; `uint16`; `float16` | 2 |
| `<i4`, `>i4`; `<u4`, `>u4`; `<f4`, `>f4` | `int32`; `uint32`; `float32` | 4 |
| `<i8`, `>i8`; `<u8`, `>u8`; `<f8`, `>f8` | `int64`; `uint64`; `float64` | 8 |

Any other `dtype` rejects the input: among them strings (`S`, `U`),
objects (`O`), void and structured types (`V`, or a list), date-times
(`M8`, `m8`), complex numbers (`c8`, `c16`), and a multi-byte type with `|`.

**Fill value.** The Zarr v3 `fill_value` `F`, by the array's type and its `fill_value`:

| type | `fill_value` | `F` |
|---|---|---|
| any | `null` | `false` for `bool` and 0 for every other type. (Zarr v2's null means the value of a missing chunk is undefined; Zarr v3 needs a value, and 0 is what Zarr v2 readers return in practice.) The null itself is kept in the source metadata ([§4](#4-source-metadata)) |
| `bool` | `true` or `false` | that value |
| integer types | an integer ([spec/zarrify.md §1.6](../zarrify.md#16-json-documents)) within the type's range | that integer |
| float types | a number whose magnitude is at most the type's largest finite value (65504 for `float16`, 3.4028234663852886e38 for `float32`), compared as binary64 ([spec/zarrify.md §1.6](../zarrify.md#16-json-documents)), other than a negative zero | that number, as a binary64 value |
| float types | a number that is a negative zero, written with a fraction or an exponent, such as `-0.0` | the Zarr v3 hex fill of its bits: `"0x8000"` for `float16`, `"0x80000000"` for `float32` and `"0x8000000000000000"` for `float64` |
| float types | one of the strings `"NaN"`, `"Infinity"` and `"-Infinity"` | that string (Zarr v3 writes these special values the same way) |

Any other value rejects the input, including, for an integer type, an integer above
2^53 − 1 (such as the largest `uint64`). A negative zero is the one value whose JSON
number some JSON writers do not keep (they write `0`), hence its hex fill. The integer
literal `-0` is the integer 0, and gives 0. (Zarr v2 writes NaN as the string `"NaN"`,
without a payload, so no other float fill value depends on bits that JSON cannot write.)

**Codecs.** In the order of their step, each when its condition holds:

| step | codec | when |
|---|---|---|
| 1 | `{"name": "transpose", "configuration": {"order": [n−1, …, 1, 0]}}` | `order` is `"F"` and `n ≥ 2` (a Fortran-order chunk stores the first index fastest) |
| 2 | `{"name": "bytes"}` | `b = 1` |
| 2 | `{"name": "bytes", "configuration": {"endian": e}}`, with `e` `"little"` for `<` and `"big"` for `>` | `b > 1` |
| 3 | the compressor's codec ([§3.1](#31-compressors)) | there is one |

**The array.** The array's `zarr.json` is the object

```json
{
  "zarr_format": 3,
  "node_type": "array",
  "shape": shape,
  "data_type": "...",
  "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": chunks}},
  "chunk_key_encoding": {"name": "v2", "configuration": {"separator": s}},
  "fill_value": F,
  "codecs": [...],
  "attributes": A
}
```

with no other members:

<a id="type-zarr2array"></a>**`Zarr2Array`**, an array's `zarr.json`: an
[`ArrayDocument`](../conventions.md#type-arraydocument) without `dimension_names`, with these
members:

| member | type | presence | value |
|---|---|---|---|
| `shape` | array of integer | required | `shape` |
| `data_type` | string | required | from `dtype`, by the table of data types above |
| `chunk_grid` | object | required | regular, its chunk shape `chunks` |
| `chunk_key_encoding` | object | required | `{"name": "v2", "configuration": {"separator": s}}`, `s` the dimension separator |
| `fill_value` | boolean, integer, number or string | required | `F` (**Fill value**, above) |
| `codecs` | array of object | required | the codecs above (**Codecs**), in their order |
| `attributes` | object | required | `A`: the members of a [`Declaration`](../conventions.md#type-declaration), the declaration of [§1](#1-declaration), with the array's source metadata ([§4](#4-source-metadata)), or `{}` if it has none (but the root, which always declares the convention) |

(Zarr v2 has no dimension names; the xarray convention `_ARRAY_DIMENSIONS` stays an
attribute.)

### 3.1 Compressors

The compressor object's `id` selects the codec:

| `id` | codec | members the codec carries |
|---|---|---|
| `zlib` | `{"name": "numcodecs.zlib", "configuration": {"level": L}}` (zarr-python's name for numcodecs' `Zlib`, which writes Zarr v2's `zlib`) | `id`, `level` |
| `gzip` | `{"name": "gzip", "configuration": {"level": L}}` | `id`, `level` |
| `zstd` | `{"name": "zstd", "configuration": {"level": L, "checksum": x}}` | `id`, `level`, `checksum` |
| `blosc` | `{"name": "blosc", "configuration": {"cname": c, "clevel": l, "shuffle": h, "typesize": b, "blocksize": k}}` | `id`, `cname`, `clevel`, `shuffle`, `blocksize` |
| anything else, among them `lz4`, `bz2`, `lzma` and `delta` | rejected | |

The values of the codecs' configurations, from the compressor's members:

| value | member | MUST be | when the member is absent |
|---|---|---|---|
| `L`, for `zlib` and `gzip` | `level` | an integer from −1 to 9; `L` is that integer, except that −1 (zlib's default level) is 6, the level it stands for (the Zarr v3 `gzip` and `numcodecs.zlib` codecs take 0 to 9) | 1, numcodecs' default |
| `L`, for `zstd` | `level` | an integer from −131072 to 22 (zstd's levels, which the Zarr v3 `zstd` codec takes); `L` is that integer | 0, numcodecs' default |
| `x`, for `zstd` | `checksum` | a boolean | `false` |
| `c`, for `blosc` | `cname` | one of `blosclz`, `lz4`, `lz4hc`, `snappy`, `zlib` and `zstd` | rejected |
| `l`, for `blosc` | `clevel` | an integer from 0 to 9 | rejected |
| `h`, for `blosc` | `shuffle` | an integer, which gives `h` by the table below | rejected |
| `k`, for `blosc` | `blocksize` | an integer from 0 to 2^31 − 1 | 0 |
| `b`, for `blosc` | | the data type's size | |

| `shuffle` | `h` |
|---|---|
| 0 | `"noshuffle"` |
| 1 | `"shuffle"` |
| 2 | `"bitshuffle"` |
| −1 (numcodecs' automatic shuffle) | `"bitshuffle"` when `b = 1`, and `"shuffle"` otherwise |

Any other value rejects the input. (A level only matters when encoding; the codec carries it
so that the compressor's configuration can be recovered.) `blosc` is numcodecs' Blosc. (A
blosc chunk describes itself, so this configuration does not change how chunks decode; it is
exactly the compressor's.) The compressor's members that its codec does not carry
(numcodecs writes none, but another writer may) are kept in the source metadata
([§4](#4-source-metadata)).

### 3.2 Chunks

**Chunks.** The chunk keys of an array at path `D` are `D/` (nothing at
the root) followed by: `0` when `n = 0`; otherwise `i0`, `i1`, …, `i(n−1)`
joined by `s`, where each `ik` is the decimal form of an integer with `0 ≤ ik
< ceil(shape[k] / chunks[k])`, without leading zeros. An array with a zero in
`shape` has no chunk keys. The **chunk objects** of the array are the
store's objects with these keys, whatever their size. A chunk is present
when its chunk object is not empty: its bytes are the whole object, the
chunk as Zarr v2 stored it. A chunk without an object reads as the fill
value, as in Zarr v2. An empty chunk object is one of [§5](#5-other-objects)'s empty objects:
its chunk is absent too, and reads as the fill value, and the mirror lists
it ([§4.1](#41-elements)). (Zarr v2 readers fail on an empty chunk object, which an interrupted
write can leave behind, rather than read the fill value; the mirror records
that the object was there.) Every other object under `D` is one of [§5](#5-other-objects)'s
other objects.

<a id="type-zarr2object"></a>**`Zarr2Object`**, a present chunk, or an other object
([§5](#5-other-objects)): the whole of an object of the store, of size `s > 0`:

| part | range | presence | value |
|---|---|---|---|
| the object | source range `(i, 0, s)` | required | every byte of the object, `i` its own `url` source ([spec/zarrify.md §1.4](../zarrify.md#14-store-inputs)) |


## 4. Source metadata

A node's source metadata `S` ([conventions §2](../conventions.md#2-attributes)) is
the object

```json
{"attributes": A, "metadata": M}
```

where each member is present only when its value is not empty, and
the node has source metadata only when `S` has a member. An implicit group
has none.

<a id="type-zarr2node"></a>**`Zarr2Node`**, a node's `S`:

| member | type | presence | value |
|---|---|---|---|
| `attributes` | object | optional | `A`, the node's attributes (its `.zattrs` document), whole and unchanged, present when not empty: user attributes, xarray's `_ARRAY_DIMENSIONS`, and the metadata of other conventions (an OME-NGFF version other than 0.4, for example, which the [OME-Zarr convention](ome-zarr.md) does not migrate). Every member is under the key, so none can collide with the conventions of the Zarr v3 hierarchy; a document that has its own `refkv_zarrified` or `zarr_conventions` nests them there |
| `metadata` | [`Zarr2Metadata`](#type-zarr2metadata) | optional | `M`, present when not empty |

<a id="type-zarr2metadata"></a>**`Zarr2Metadata`**, `M`: the members of the node's `.zgroup`
or `.zarray` that its `zarr.json` does not reproduce, in this order:

| member | type | presence | value |
|---|---|---|---|
| one per member of `.zgroup` but `zarr_format` | any JSON value | optional | for an explicit group, every member of `.zgroup` but `zarr_format`, in the document's order |
| one per member of `.zarray` that [§3](#3-arrays) does not read | any JSON value | optional | for an array, every member of `.zarray` that [§3](#3-arrays) does not read (any member but `zarr_format`, `shape`, `chunks`, `dtype`, `order`, `compressor`, `filters`, `fill_value` and `dimension_separator`), as written, in the document's order |
| `fill_value` | null or integer | optional | for an array: `null` when `fill_value` is `null` or absent ([§3](#3-arrays) gives `F` = 0 or `false`, which is not Zarr v2's "undefined"); the member as written when the array's type is a float type and `fill_value` is an integer literal (no fraction, no exponent) beyond ±(2^53 − 1) (`F` is its binary64 value, which may differ from it, and is not written as an integer) |
| `compressor` | object | optional | for an array, when the compressor has members that its codec does not carry ([§3.1](#31-compressors)), or blosc's `shuffle` is −1 (which [§3.1](#31-compressors) resolves to a shuffle): the object of those members, `shuffle` among them when it is −1, in the compressor's order |

What `S` leaves out, the Zarr v3 metadata holds: `zarr_format`, `shape`,
`chunks`, the data type, `order`, the fill value, the compressor's `id`
and the members its codec carries, the filters and the separator. What
neither keeps is layout: the byte order character of a one-byte type,
`order` for an array of fewer than two dimensions (whose chunks C and F
order lay out alike), whether a default level was written (or was −1
rather than 6), whether no filters were `null`, `[]` or absent, whether the
compressor was `null` or absent, whether the separator `"."` was written or
was `null`, whether `fill_value` was `null` or absent, whether zstd's
`checksum` was `false` or absent, whether blosc's `blocksize` was 0 or
absent, and the documents' formatting (whitespace, member order, and how a
number that is not an integer is spelled). So a node's documents are
recovered from its `zarr.json`: `.zattrs` is `A`, and `.zgroup` or
`.zarray` is the document that [§2](#2-nodes) and [§3](#3-arrays) read back from the Zarr v3
metadata, with the members of `M` set over it (those of `M.compressor`
over the compressor object). The documents themselves, formatting included,
are the mirror's ([§4.1](#41-elements)).

Members are copied as [spec/zarrify.md §1.6](../zarrify.md#16-json-documents)
reads them, except that a number written as an integer (a JSON number
with no fraction and no exponent) is kept exactly, every digit, beyond
2^53 − 1 too, and is written back so. Any other number is its binary64
value, and a number whose binary64 value is infinite still rejects the
input. Where [§3](#3-arrays) uses a number (a size, a fill value, a level), it uses its
binary64 value.

### 4.1 Elements

Everything else the store holds that the hierarchy does not reference is on
`refkv_source`, which is the store's **IR mirror**
([conventions §8](../conventions.md#8-the-ir-mirror), as a store's IR,
[conventions §8.9](../conventions.md#89-stores)): its table, from which the
listing and every document read are rebuilt byte for byte, and its view,
`refkv_source/tree`. This section is the Zarr v2 **source model**: the
elements of a Zarr v2 store's IR.

Paths are relative to the IR's root, whose path is `""`; `<d>`, `<k>` are a
directory's and an object's key segments, named by
[conventions §8.9](../conventions.md#89-stores) (so a chunk key's trailing
digits are its name index). Types are those of
[conventions §8.6](../conventions.md#86-types).

| path | kind | type | extent: what |
|---|---|---|---|
| `""` | struct | | the store's root directory (no extent) |
| `…/<d>` | struct | | a directory of the store (no extent) |
| `…/<k>` | object | | an object of the store: `(0, s)`, `s` its listed size. It is **held** when it is a document [§2](#2-nodes) reads (the `.zgroup` of an explicit group, the `.zarray` of an array, the `.zattrs` of either), and otherwise **referenced** (a chunk object, an other object of [§5](#5-other-objects)) or empty |
| `…/<k>/<m>` | value | `ascii[n]` | in a held object: the text of the value of its top-level member `m`, as written (`n` its bytes); the members' keys are named as [conventions §8.1](../conventions.md#81-elements) names source texts (a key that is empty, holds `/` or `~`, is `gaps` or `json`, or repeats an earlier one is escaped and indexed, as ND2's names are, [ND2 §5.3](nd2.md#53-elements), so that no member takes the gaps' name or the whole document's) |
| `…/<k>/json` | value | `ascii[s]` | in a held object of more than 4096 top-level members: the whole document, in place of a value per member (the view would show only the first few thousand of them) |
| `…/<k>/gaps/<offset>` | gap | | in a held object: the bytes between its members' values (the braces, the keys, the colons, commas and whitespace), or the whole object when it has no member |

The view shows each member's text of at most 1024 bytes as a string
([conventions §8.7](../conventions.md#87-the-view)); a member past that is
the spill form, its bytes in `refkv_source/ir/held`. The mirror lists every
object of the store, documents nested under arrays, `.zmetadata` and empty
objects included, with its size; the hierarchy references the chunk objects
and the other objects ([§5](#5-other-objects)), and the mirror rebuilds the
rest.

**Equivalence.** Hierarchies of one store are compared entry for entry,
`refkv_source` included ([conventions §11.4](../conventions.md#114-equivalence)).

## 5. Other objects

An **other object** is an object of the store
([spec/zarrify.md §1.4](../zarrify.md#14-store-inputs)) whose size is not
0 and which is none of these:

- a document that [§2](#2-nodes) reads: the `.zgroup` of an explicit group, the
  `.zarray` of an array, and the `.zattrs` of either;
- a chunk object of an array ([§3.2](#32-chunks)), whatever its size.

Among them are READMEs and other files a writer put next to the data,
OME-XML outside the place that [the OME-Zarr convention
§7](ome-zarr.md#7-chunks-and-ome-xml) gives it, a `.zattrs` of a
directory that is not a node, documents nested under an array (the
`.zgroup` or `.zarray` of a candidate inside an array), and the objects
under an array that are not chunk keys. Each is kept whole: the hierarchy has the key `refkv_source/objects/<k>`, where `k` is the
object's key, escaped, and its bytes are the object's. (An empty object
holds nothing, and has no key; the mirror lists it, below.)

**Escaping.** A last segment that a Zarr reader would take for a node's
document must not end a key under `refkv_source/objects/`: when the last
segment of `k` is `zarr.json`, `.zarray` or `.zgroup` followed by any
number (zero included) of `~`, one `~` is appended to it. So `zarr.json`
is kept at `refkv_source/objects/zarr.json~`, `a/.zgroup` at
`refkv_source/objects/a/.zgroup~`, and `zarr.json~` at
`refkv_source/objects/zarr.json~~`. The escape is one to one: a key whose
last segment is one of these names followed by at least one `~` is the
object's key with one `~` removed, and every other key is the object's.
No Zarr v3 or Zarr v2 reader then opens a node under `refkv_source/objects/`.

**Empty objects and ignored keys.** The **empty objects** are the store's
objects of size 0, the empty chunk objects ([§3.2](#32-chunks)) among them: they have no
entry, and the mirror lists them, each an object of the IR of size 0
([§4.1](#41-elements)). The **ignored keys** are the relative keys of the
listed objects that
[spec/zarrify.md §1.4](../zarrify.md#14-store-inputs) ignores and
records, which are not objects of the IR. When there is at least one,
`refkv_source`'s source metadata has, beside the mirror's description `ir`,
the member `"ignored": I`, `I` the ignored keys in ascending order of their
UTF-8 bytes; otherwise it has no such member.

**The group `refkv_source`.** The hierarchy has the group `refkv_source`, a
child of the root (the source metadata node of
[conventions §2](../conventions.md#2-attributes)), whose `zarr.json` declares
the convention with the source metadata `{"ir": D, "ignored": I}` (`D` the
mirror's description, [conventions §8.2](../conventions.md#82-the-table);
`ignored` as above), and the mirror's arrays
and view groups below it. The directories between it and the other objects are
not nodes and have no `zarr.json`. A hierarchy that has a node ([§2](#2-nodes)),
an implicit group included, at the path `refkv_source`, or below it, is
rejected, since its keys would mix with these. When the root is an array,
which can have no children, the mirror's keys (`refkv_source/zarr.json`
among them) and the objects' keys under `refkv_source/objects/` are plain keys
of the archive, which Zarr readers do not read as nodes.

<a id="type-zarr2mirror"></a>**`Zarr2Mirror`**, `refkv_source`'s source metadata: a
[`Mirror`](../conventions.md#type-mirror) with these members, and no `empty_dirs`:

| member | type | presence | value |
|---|---|---|---|
| `ir` | [`IrDescription`](../conventions.md#type-irdescription) | required | `D`, the mirror's description, a store IR's, with `objects` ([conventions §8.9](../conventions.md#89-stores)) |
| `ignored` | array of string | optional | `I`, the ignored keys in ascending order of their UTF-8 bytes; present only when there is at least one |

## 6. Example

An array of the store `https://example.org/h.zarr/`, whose `.zarray` is

```json
{
  "zarr_format": 2,
  "shape": [4],
  "chunks": [4],
  "dtype": "<u2",
  "compressor": {"id": "blosc", "cname": "lz4", "clevel": 5, "shuffle": 1, "blocksize": 0, "nthreads": 2},
  "fill_value": 0,
  "order": "C",
  "filters": null,
  "custom": {"x": 1}
}
```

and whose `.zattrs` is `{"_ARRAY_DIMENSIONS": ["x"]}`:

```json
{
  "zarr_format": 3,
  "node_type": "array",
  "shape": [
    4
  ],
  "data_type": "uint16",
  "chunk_grid": {
    "name": "regular",
    "configuration": {
      "chunk_shape": [
        4
      ]
    }
  },
  "chunk_key_encoding": {
    "name": "v2",
    "configuration": {
      "separator": "."
    }
  },
  "fill_value": 0,
  "codecs": [
    {
      "name": "bytes",
      "configuration": {
        "endian": "little"
      }
    },
    {
      "name": "blosc",
      "configuration": {
        "cname": "lz4",
        "clevel": 5,
        "shuffle": "shuffle",
        "typesize": 2,
        "blocksize": 0
      }
    }
  ],
  "attributes": {
    "zarr_conventions": [
      {
        "uuid": "8e792619-d671-4687-ab51-752885dd3ee6",
        "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/zarrify/zarr2/schema.json",
        "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md",
        "name": "refkv_zarrified",
        "description": "The Zarr layout of a Zarr v2 source zarrified by refkv, and the source's metadata"
      }
    ],
    "refkv_zarrified": {
      "zarr2": {
        "attributes": {
          "_ARRAY_DIMENSIONS": [
            "x"
          ]
        },
        "metadata": {
          "custom": {
            "x": 1
          },
          "compressor": {
            "nthreads": 2
          }
        }
      }
    }
  }
}
```

# Part 2. The profile

The Zarr v2 profile of [spec/zarrify.md](../zarrify.md) is [§7](#7-zarr-v2-profile) of this document. What every profile's Part 2 shares is in
[spec/zarrify.md, Profiles](../zarrify.md#profiles).

## 7. Zarr v2 profile

The profile was chosen because the store's root has a `.zarray` or a
`.zgroup` and the store does not declare OME-NGFF 0.4 ([§1.4](../zarrify.md#14-store-inputs)); a store that
does is read by the OME-Zarr profile ([ome-zarr.md §11](ome-zarr.md#11-ome-zarr-profile), [ome-zarr.md](ome-zarr.md#part-2-the-profile)),
which builds on this one. The store is listed ([§1.5](../zarrify.md#15-listing-a-store)), and the documents
that [the convention §2](#2-nodes) reads are
read as JSON ([§1.6](../zarrify.md#16-json-documents)).

**Consolidated metadata.** A `.zmetadata` object (consolidated metadata) is
never read: nodes come from the listing and documents from their own
objects, so stale consolidated metadata cannot change the output. (Chunks
must come from the listing anyway, since `.zmetadata` does not list them.)

**Numbers.** The documents' integer literals are kept exactly where the
convention copies them ([its §4](#4-source-metadata)),
beyond 2^53 − 1 too, in place of [§1.6](../zarrify.md#16-json-documents)'s last paragraph; every number the
convention uses is its binary64 value, as [§1.6](../zarrify.md#16-json-documents) says.

### 7.1 Rejection

The input is rejected when a rule of [§1.4](../zarrify.md#14-store-inputs)–[§1.6](../zarrify.md#16-json-documents) fails, and when the
convention gives it no layout: wherever it says that the input is
rejected, or that something MUST hold and it does not.

### 7.2 Chunks and other objects

Every chunk that the convention says is present
([the convention §3.2](#32-chunks)) is an
entry under its own key, referencing the whole chunk object through its own
`url` source ([§1.4](../zarrify.md#14-store-inputs)). A chunk object of size 0 has no entry: it is
one of [the convention §5](#5-other-objects)'s empty objects, which the mirror lists.

Every other object of [the convention §5](#5-other-objects)
is referenced whole in the same way, by an entry under the key
`refkv_source/objects/<k>` (with a `~` appended when [the convention §5](#5-other-objects)
escapes `k`'s last segment) whose `url` source is the URL of the object `k`
([§1.4](../zarrify.md#14-store-inputs)). (This is the one place where an entry's key is not its object's
key; [§1.4](../zarrify.md#14-store-inputs)'s rules otherwise apply: the sources are in ascending order of
their entries' keys, each entry referencing its object's whole range.)
The ignored keys are the member `ignored` of `refkv_source`'s source metadata
([the convention §5](#5-other-objects)). The documents under `refkv_source/`
(the mirror) are deflated and, like the chunks, not among the documents a
reader fetches when it opens the archive
([conventions §2](../conventions.md#2-attributes)).

**Reading.** The store is listed, then every document that
[the convention §2](#2-nodes) reads, which the listing names, is read whole in
one round, concurrently ([conventions §8.9](../conventions.md#89-stores)), each
once; then the documents are checked in path order. So a document that cannot
be read is a failure even when another document read in the same round would
reject the input. No other object is read.

### 7.3 Summary

This section is informative. Both implementations print a one-line JSON
summary, with these members:

| member | value |
|---|---|
| `groups` | the groups, explicit and implicit |
| `arrays` | the arrays |
| `chunks` | the number of chunk entries |
| `emptyChunks` | the chunk objects of size 0 |
| `objects` | the store's objects |
| `otherObjects` | the entries under `refkv_source/objects/` |
| `listingRequests` | the listing's requests |
| `reads` | the Rust core's count: the object reads |
| `elements` | the Rust core's count: the IR's elements |
| `folded` | the Rust core's count: what the mirror's table folded |
