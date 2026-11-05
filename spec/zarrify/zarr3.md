# Zarr v3

The Zarr v3 layout of a Zarr v3 hierarchy, and its attributes. What all of refkv's conventions
share is in [spec/conventions.md](../conventions.md), cited here as "conventions §n". How refkv
produces this layout as a reference store from a listed store or a zip file is the Zarr v3
profile, [Part 2](#part-2-the-profile) below. It covers the native Zarr inputs of
[d-v-b/refkv#271](https://github.com/d-v-b/refkv/issues/271), measured against VirtualiZarr's
`ZarrParser` and `ZippedZarrParser`.

Convention version: 0 (until release, [conventions §1](../conventions.md#1-conventions)) · UUID: `95203d0a-6586-426a-b920-3db2f4f1e97e` ·
Schema: [schema.json](zarr3/schema.json)

Changes: [changes/zarr3/](changes/zarr3/), one file per change.

Added 2026-11-05.

This document has two parts ([conventions §11.1](../conventions.md#111-parts)).

# Part 1. The convention

This convention gives a layout only to the hierarchies that meet its requirements. Where it says
that "the input is rejected", or that something MUST hold, a hierarchy that fails has no layout
under this convention, and the profile rejects it. A node below the root that fails
[§2](#2-nodes) or [§3](#3-arrays) is the one exception: it is left out, and the rest of the
hierarchy has its layout ([§3.3](#33-nodes-that-are-not-presented)).

A Zarr v3 hierarchy (the Zarr storage specification, version 3) is a tree of groups and arrays,
each with a `zarr.json` document; an array's chunks are objects at the keys its chunk key
encoding gives. The source is already the target format, so the output is the hierarchy itself:
a node at the path of every node, whose `zarr.json` is the store's own, and each array's chunks
its chunk objects, referenced whole, under the same keys. The convention adds its declaration to
the attributes, moves into the source metadata the two attributes that would collide with it
([§4](#4-source-metadata)), and drops consolidated metadata, which would describe the store and
not the hierarchy ([§2](#2-nodes)). It decodes nothing: codecs, data types, fill values and chunk
grids pass through as written ([§3](#3-arrays)), so an array reads through the output exactly as
it reads from the store. The store's listing and every document read, byte for byte, are its IR
mirror on `refkv_source` ([§4.1](#41-elements)); every other object of the store is kept whole
([§5](#5-other-objects)).

## Layout

The hierarchy's entries ([conventions §11.5](../conventions.md#115-layout-tables)):

| prefix | type | structure | description |
|---|---|---|---|
| `zarr.json` | group | [`Zarr3Node`](#type-zarr3node) | the root, when the store's root is a group ([§2](#2-nodes)) |
| `zarr.json` | array | [`Zarr3Node`](#type-zarr3node) | the root, when the store's root is an array ([§3](#3-arrays)) |
| `zarr.json#/attributes/zarr_conventions` | attribute | array of [`ConventionMetadata`](../conventions.md#type-conventionmetadata) | `[C, …]`, `C` the CMO of [§1](#1-declaration), then the root's own CMOs, if it keeps them in place ([§4](#4-source-metadata)) |
| `zarr.json#/attributes/refkv_zarrified` | attribute | [`RootProperty`](../conventions.md#type-rootproperty) | `profile` `"zarr3"`, `version` `0`, `source.url` the store's URL, ending in `/`, or the zip file's URL |
| `zarr.json#/attributes/refkv_zarrified/zarr3` | attribute | [`Zarr3Source`](#type-zarr3source) | the root's source metadata ([§4](#4-source-metadata)), when it has any |
| `<path>/zarr.json` | group | [`Zarr3Node`](#type-zarr3node) | every other group, implicit or not ([§2](#2-nodes)) |
| `<path>/zarr.json` | array | [`Zarr3Node`](#type-zarr3node) | every other array that is presented ([§3](#3-arrays)) |
| `<path>/zarr.json#/attributes/zarr_conventions` | attribute | array of [`ConventionMetadata`](../conventions.md#type-conventionmetadata) | `[C, …]`, on a node that has source metadata |
| `<path>/zarr.json#/attributes/refkv_zarrified` | attribute | [`NodeProperty`](../conventions.md#type-nodeproperty) | `{"zarr3": S}`, on a node that has source metadata ([§1](#1-declaration)) |
| `<path>/zarr.json#/attributes/refkv_zarrified/zarr3` | attribute | [`Zarr3Source`](#type-zarr3source) | `S`, the node's source metadata ([§4](#4-source-metadata)) |
| `<chunk>` | chunk | [`Zarr3Object`](#type-zarr3object) | each present chunk of the root, when it is an array ([§3.2](#32-chunks)) |
| `<path>/<chunk>` | chunk | [`Zarr3Object`](#type-zarr3object) | each present chunk of the presented array at `<path>` ([§3.2](#32-chunks)) |
| `refkv_source/**` | subtree | [the mirror's entries](../conventions.md#table-mirror) | the IR mirror ([§4.1](#41-elements)): a store IR's, with `ir/held`, for a listed store; a file IR's for a zip file. Under a root array, plain keys of the archive ([§5](#5-other-objects)) |
| `refkv_source/zarr.json#/attributes/refkv_zarrified/zarr3` | attribute | [`Zarr3Mirror`](#type-zarr3mirror) | `refkv_source`'s source metadata ([§5](#5-other-objects)) |
| `refkv_source/objects/<k>` | object | [`Zarr3Object`](#type-zarr3object) | each other object of a listed store, kept whole ([§5](#5-other-objects)) |

| placeholder | value |
|---|---|
| `<path>` | a node's path, the path of a directory of the store ([§2](#2-nodes)); it stands for one or more segments |
| `<chunk>` | a chunk key of the array ([§3.1](#31-chunk-grids-and-chunk-keys)); it stands for one or more segments |
| `<k>` | the other object's key, its last segment escaped ([§5](#5-other-objects)); it stands for one or more segments |

## 1. Declaration

The root and `refkv_source` declare the convention
([conventions §11.2](../conventions.md#112-declaration)), the root with its source metadata
([§4](#4-source-metadata)), if it has any, as the member `"zarr3"`. Every other node that has
source metadata declares it with `{"zarr3": S}`. Its CMO is:

```json
{
  "uuid": "95203d0a-6586-426a-b920-3db2f4f1e97e",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/zarrify/zarr3/schema.json",
  "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr3.md",
  "name": "refkv_zarrified",
  "description": "The Zarr layout of a Zarr v3 source zarrified by refkv, and the source's metadata"
}
```

## 2. Nodes

The **candidates** are the directories `D` (the root included) for which the store has an object
`D/zarr.json` (`zarr.json` for the root). A store whose root is not a candidate is rejected (`not
a Zarr v3 store: the root has no zarr.json`). Candidates are classified from the root down (in
order of their number of segments, then of their paths):

- a candidate inside an array or inside a node that is not presented
  ([§3.3](#33-nodes-that-are-not-presented)) is **not a node**, and its `zarr.json` is one of
  [§5](#5-other-objects)'s other objects;
- otherwise it is a **node**: an array or a group, as its `zarr.json` says, or a node that is not
  presented.

Every directory that is a proper ancestor of a node, and is not one itself, is an **implicit
group**: its `zarr.json` is `{"zarr_format": 3, "node_type": "group", "attributes": {}}`. (Zarr
v3 has no implicit groups; the output gives the node a document, as the Zarr v2 convention does,
so that every reader can walk to the nodes below it. The mirror's listing says the store had
none.) No other directory is a node.

A node's `zarr.json` is read as JSON ([spec/zarrify.md §1.6](../zarrify.md#16-json-documents)).
It MUST be an object with these members:

| member | MUST be |
|---|---|
| `zarr_format` | the integer 3 |
| `node_type` | `"array"` or `"group"` |
| `attributes` | absent, or an object |

and an array's MUST meet [§3](#3-arrays). Its other members are not read.

<a id="type-zarr3node"></a>**`Zarr3Node`**, a node's `zarr.json` in the output: the store's
`zarr.json`, every member as written and in its order, but

| member | value |
|---|---|
| `attributes` | the attributes kept in place ([§4](#4-source-metadata)), then, on a node that declares the convention, its [`Declaration`](../conventions.md#type-declaration): `zarr_conventions` is `C` followed by the CMOs the node keeps in place, if any, and `refkv_zarrified` is last. A node without `attributes` gets the member, last |
| `consolidated_metadata` | absent: it is left out |

**Consolidated metadata.** zarr-python writes a group's consolidated metadata as the member
`consolidated_metadata` of its `zarr.json`. It is never read: nodes come from the listing and
documents from their own objects, so stale consolidated metadata cannot change the output, and
the output leaves the member out, since it would describe the store's documents and not the
hierarchy's. The mirror holds the document it was in, byte for byte ([§4.1](#41-elements)).

## 3. Arrays

An array's `zarr.json` MUST have these members, or the array is not presented
([§3.3](#33-nodes-that-are-not-presented)):

| member | value |
|---|---|
| `shape` | an array of `n` integers ([spec/zarrify.md §1.6](../zarrify.md#16-json-documents)), `0 ≤ n ≤ 32`, each from 0 to 2^53 − 1 |
| `chunk_grid` | a chunk grid of [§3.1](#31-chunk-grids-and-chunk-keys), for `n` dimensions |
| `chunk_key_encoding` | a chunk key encoding of [§3.1](#31-chunk-grids-and-chunk-keys) |
| `storage_transformers` | absent, or an empty array |
| `data_type`, `fill_value` | present |
| `codecs` | a nonempty array |

An extension point (a chunk grid, a chunk key encoding, a codec) is an object whose member `name`
is a string and whose member `configuration`, if present, is an object, or a string, its name
alone.

**What passes through.** The convention checks only what it needs to find an array's chunks: its
shape, chunk grid and chunk key encoding. Everything else is the store's and passes through
unchanged, whatever its value:

- **Codecs.** Every codec, by any name, with any configuration, in its order: zarr-python's
  (`bytes`, `transpose`, `gzip`, `zstd`, `blosc`, `crc32c`, `sharding_indexed`, `vlen-utf8`,
  `vlen-bytes`), the `numcodecs.*` codecs, imagecodecs-zarr's, refkv's own, and codecs no reader
  has. A chunk is referenced whole and never decoded, so a reader decodes it from the output with
  the same codecs it would decode it from the store with, and fails where it would fail there.
  The convention names no codec of its own, so [conventions §3](../conventions.md#3-arrays)'s
  table of codec names does not constrain the names that pass through.
- **Data types and fill values**: any, as written, an integer of any number of digits included
  ([§4](#4-source-metadata)).
- **Chunk grids**: `regular` and `rectilinear` ([§3.1](#31-chunk-grids-and-chunk-keys)), with
  their configurations as written. Another grid's chunk keys are not known, so its array is not
  presented.
- `dimension_names`, and any member the Zarr v3 specification does not define, extension members
  with `must_understand` included: a reader meets them as it would in the store.

`storage_transformers` is the exception: a storage transformer changes where a chunk's bytes are,
so an array with one is not presented.

### 3.1 Chunk grids and chunk keys

The array's **chunks along axis `k`**, `m[k]`, by its chunk grid:

| `chunk_grid` | MUST be | `m[k]` |
|---|---|---|
| `regular` | a configuration whose member `chunk_shape` is an array of `n` integers, each from 1 to 2^53 − 1 | `ceil(shape[k] / chunk_shape[k])` |
| `rectilinear` (zarr-extensions) | a configuration whose member `kind` is `"inline"` and whose member `chunk_shapes` is an array of `n` entries; each an integer from 1 to 2^53 − 1, or a nonempty array whose items are each an integer from 1 to 2^53 − 1 or a pair `[s, r]` of such integers | an integer entry `c`: `ceil(shape[k] / c)`; an array, whose sizes (an integer item one size, a pair `r` sizes `s`) MUST total at least `shape[k]`: the number of its sizes, in order, that reach `shape[k]` (sizes past them are not chunks, as zarr-python counts them) |

Any other grid, or a configuration that does not meet its row, means the array is not presented
(the message `chunk grid <name> is not one of <n> dimensions` names both).
(`m[k]` is 0 when `shape[k]` is 0: such an array has no chunk keys.)

The **chunk key encoding** is `default` or `v2`, with the separator `s` that its configuration's
member `separator` gives, which MUST be `"."` or `"/"` (`null` is neither); without the member, `s` is `"/"` for `default`
and `"."` for `v2`. Any other encoding means the array is not presented. The **chunk keys** of the
array at path `D` are `D/` (nothing at the root) followed by:

| encoding | `n = 0` | `n ≥ 1` |
|---|---|---|
| `default` | `c` | `c`, then for each axis `s` and `i[k]` |
| `v2` | `0` | the `i[k]` joined by `s` |

where each `i[k]` is the decimal form, without leading zeros, of an integer with
`0 ≤ i[k] < m[k]`. These are the keys the Zarr v3 specification gives; a key under the array that
is not one of them (a chunk index outside the grid, `00`, a different separator) is one of
[§5](#5-other-objects)'s other objects.

### 3.2 Chunks

The **chunk objects** of a presented array are the store's objects whose keys are its chunk keys,
whatever their size. A chunk is **present** when its chunk object is not empty: its bytes are the
whole object, as the store holds it. A chunk without an object reads as the fill value, as in the
store. An empty chunk object is one of [§5](#5-other-objects)'s empty objects: its chunk is absent
too, and reads as the fill value, and the mirror lists it ([§4.1](#41-elements)). (zarr-python
fails on an empty chunk object, which an interrupted write can leave, rather than read the fill
value; the mirror records that the object was there.)

**Sharded arrays.** An array whose codecs hold `sharding_indexed` has shards as its chunks: the
chunk grid's chunks are the shards, and each shard object is a chunk object, referenced whole,
with the codec and its index passing through. The inner chunks are not referenced one by one,
which would need every shard's index read: the profile reads no chunk
([spec/zarrify.md §1.4](../zarrify.md#14-store-inputs)). A reader that reads part of a shard
(its index, then an inner chunk) asks the reference store for ranges of the shard's entry, which
it reads as ranges of the shard's object, so partial reads stay exact. A shard of a sharded
array is the same reference as any chunk; VirtualiZarr's `ZarrParser` references shards whole
too.

<a id="type-zarr3object"></a>**`Zarr3Object`**, a present chunk, or an other object
([§5](#5-other-objects)): the whole of an object of the store, of size `z > 0`:

| part | range | presence | value |
|---|---|---|---|
| the object | a listed store's source range `(i, 0, z)`, `i` the object's own `url` source ([spec/zarrify.md §1.4](../zarrify.md#14-store-inputs)); a zip file's `(0, ds, z)`, the entry's data in the zip file ([§7.4](#74-the-zip-form)) | required | every byte of the object |

### 3.3 Nodes that are not presented

A node below the root whose `zarr.json` fails [§2](#2-nodes) or [§3](#3-arrays), is not JSON
([spec/zarrify.md §1.6](../zarrify.md#16-json-documents)), or is too large or empty to read, is
**not presented**: it has no `zarr.json`, and its reason, the rule it failed first, is in the
root's source metadata ([`Zarr3Source`](#type-zarr3source)'s `not_presented`), in order of the
nodes' paths. In the zip form, an array a chunk of which is compressed is not presented either
([§7.4](#74-the-zip-form)). A node that is not presented is still a node of [§2](#2-nodes): a
candidate inside it is not a node (its node type may be unknown, so it is taken for an array),
and its ancestors are groups. Its document is held in the mirror ([§4.1](#41-elements)) (a
document that is not JSON as one gap), but for one too large to read, which is an other object.
Its objects are other objects ([§5](#5-other-objects)), so the hierarchy keeps every byte of it.

A root that fails is the whole store, and the input is rejected, with its reason.

<a id="type-zarr3notpresented"></a>**`Zarr3NotPresented`**, a node left out:

| member | type | presence | value |
|---|---|---|---|
| `path` | string | required | the node's path |
| `reason` | string | required | the rule it failed first |

## 4. Source metadata

The attributes of a Zarr v3 node are the target format's metadata already: OME-NGFF 0.5's `ome`,
xarray's and CF's attributes, GeoZarr's conventions. So they stay in place, beside the
declaration, as the HDF5 convention keeps an HDF5 object's
([hdf5 §2.1](hdf5.md#21-declaration-and-attributes)), departing from
[conventions §2](../conventions.md#2-attributes) ("Nothing else"). Two attributes would collide
with the declaration, and **move** into the source metadata:

- `refkv_zarrified`, always: a store that is itself a refkv hierarchy nests its own record, as
  [conventions §2](../conventions.md#2-attributes) says;
- `zarr_conventions`, unless it is a nonempty array of which no element is an object whose member
  `uuid` is `C`'s or whose member `name` is `"refkv_zarrified"`. Such an array lists other
  conventions' CMOs, which stay in place: on a node that declares the convention they follow `C`
  in its `zarr_conventions` ([conventions §2](../conventions.md#2-attributes), "Other
  attributes"), and on any other node `zarr_conventions` is as written. Any other value (an
  object, an empty array, an array that names this convention or another refkv convention, whose
  member `refkv_zarrified` moves with it) moves, so that the output declares no convention whose
  member it does not hold.

A node's source metadata `S` ([conventions §2](../conventions.md#2-attributes)) is the object of
these members, each present only when its value is not empty; the node has source metadata only
when `S` has a member:

<a id="type-zarr3source"></a>**`Zarr3Source`**, a node's `S`:

| member | type | presence | value |
|---|---|---|---|
| `attributes` | object | optional | the attributes moved, as written, in the document's order |
| `not_presented` | array of [`Zarr3NotPresented`](#type-zarr3notpresented) | optional | the root's, when a node is left out ([§3.3](#33-nodes-that-are-not-presented)), in order of their paths |

So a node's `zarr.json` is recovered from the output: its `attributes` are the output's, without
the declaration (and without `C` in `zarr_conventions`), with `S.attributes` set over them; its
other members are the output's; `consolidated_metadata` and the document's formatting (whitespace,
member order within nested objects, how a number that is not an integer is spelled) are the
mirror's ([§4.1](#41-elements)). Members are copied as
[spec/zarrify.md §1.6](../zarrify.md#16-json-documents) reads them, except that a number written
as an integer (a JSON number with no fraction and no exponent) is kept exactly, every digit,
beyond 2^53 − 1 too, as a `uint64` fill value of 18446744073709551615 needs; any other number is
its binary64 value.

### 4.1 Elements

Everything else the store holds that the hierarchy does not reference is on `refkv_source`, which
is the store's **IR mirror** ([conventions §8](../conventions.md#8-the-ir-mirror)).

**A listed store** has a store's IR ([conventions §8.9](../conventions.md#89-stores)): its table,
from which the listing and every document read are rebuilt byte for byte, and its view,
`refkv_source/tree`. Its elements are [the Zarr v2 convention's](zarr2.md#41-elements), a
node's `zarr.json` read being a **held** object as a `.zarray` is (a value per top-level member,
the rest gaps; one of more than 4096 top-level members one value, `json`); every other object is
referenced, or empty.

**A zip file** has a file's IR of the zip file ([conventions §8.1](../conventions.md#81-elements)):
its leaves partition the zip file, which the mirror rebuilds byte for byte from source 0. Types
are those of [conventions §8.6](../conventions.md#86-types):

| path | kind | type | extent: what |
|---|---|---|---|
| `""` | struct | | the zip file, `(0, size)` |
| `end` | value | `{signature:<u4,disk:<u2,cd_disk:<u2,entries_here:<u2,entries:<u2,cd_size:<u4,cd_offset:<u4,comment_length:<u2}` | the EOCD record |
| `end_comment` | value | `ascii[n]` | its comment (when not empty) |
| `zip64_locator` | value | `{signature:<u4,cd_disk:<u4,record_offset:<u8,disks:<u4}` | the ZIP64 EOCD locator (ZIP64 only) |
| `zip64_end` | value | `{signature:<u4,size:<u8,version_made:<u2,version_needed:<u2,disk:<u4,cd_disk:<u4,entries_here:<u8,entries:<u8,cd_size:<u8,cd_offset:<u8}` | the ZIP64 EOCD record's first 56 bytes (ZIP64 only) |
| `central_directory` | struct | | the central directory (when not empty) |
| `central_directory/entries/<i>` | struct | | central directory entry `i`: its `header` (`{signature:<u4,version_made:<u2,version_needed:<u2,flags:<u2,method:<u2,time:<u2,date:<u2,crc32:<u4,compressed_size:<u4,size:<u4,name_length:<u2,extra_length:<u2,comment_length:<u2,disk:<u2,internal:<u2,external:<u4,local_header_offset:<u4}`), `name` (`ascii[n]`), `extra` (`bytes[n]`) and `comment` (`ascii[n]`), each when not empty |
| `entries/<i>` | struct | | the local header and data of central directory entry `i`, for each object of the store that is not empty, `[lho, ds + cs)` |
| `entries/<i>/local_header` | value | `{signature:<u4,version:<u2,flags:<u2,method:<u2,time:<u2,date:<u2,crc32:<u4,compressed_size:<u4,size:<u4,name_length:<u2,extra_length:<u2}` | its local header |
| `entries/<i>/name`, `entries/<i>/extra` | value | `ascii[n]`, `bytes[n]` | its local header's name and extra field (when not empty) |
| `entries/<i>/data` | value or derived | | a node's `zarr.json` read: `ascii[us]` when stored, a derived element of the form `{"transform":"deflate"}` over `[ds, ds + cs)` when deflated; any other object: `bytes[us]` when stored, `bytes[cs]` (its compressed bytes) otherwise |
| `gaps/<offset>` | gap | | bytes no other element claims: the local headers and data of empty objects, ignored entries and superseded entries ([§7.4](#74-the-zip-form)), data descriptors, padding |

**Equivalence.** Hierarchies of one store are compared entry for entry, `refkv_source` included
([conventions §11.4](../conventions.md#114-equivalence)).

## 5. Other objects

An **other object** is an object of a listed store
([spec/zarrify.md §1.4](../zarrify.md#14-store-inputs)) whose size is not 0 and which is neither
a node's `zarr.json` read ([§4.1](#41-elements)) nor a chunk object of a presented array
([§3.2](#32-chunks)). Among them are READMEs, a Zarr v2 document (`.zgroup`, `.zarray`) beside
the hierarchy, the `zarr.json` of a candidate that is not a node, the objects under an array that
are not chunk keys, and all the objects of a node that is not presented
([§3.3](#33-nodes-that-are-not-presented)) but for the document the mirror holds. Each is kept
whole: the hierarchy has the key `refkv_source/objects/<k>`, `k` the object's key escaped as
[the Zarr v2 convention §5](zarr2.md#5-other-objects) escapes it (a last segment `zarr.json`,
`.zarray` or `.zgroup`, followed by any number of `~`, gets one `~` more), and its bytes are the
object's. A zip file has no other objects in the hierarchy: its mirror rebuilds the zip file,
every object's bytes included.

**Empty objects and ignored keys** are as in [the Zarr v2 convention §5](zarr2.md#5-other-objects):
the empty objects have no entry, and the mirror lists them, each an object of the IR of size 0 (in
a zip file, a gap of its local header); the ignored keys, a listing's
([spec/zarrify.md §1.4](../zarrify.md#14-store-inputs)) or a zip file's
([§7.4](#74-the-zip-form)), are `refkv_source`'s member `ignored`.

**The group `refkv_source`** is as in [the Zarr v2 convention §5](zarr2.md#5-other-objects): a
child of the root that declares the convention with the source metadata
[`Zarr3Mirror`](#type-zarr3mirror), and holds the mirror and the other objects. A hierarchy that
has a node, an implicit group included, at the path `refkv_source` or below it is rejected (`the
node <p> is where the store's source metadata goes (refkv_source/)`, `<p>` the first such path).
When the root is an array, the mirror's keys and the objects' keys are plain keys of the archive.

<a id="type-zarr3mirror"></a>**`Zarr3Mirror`**, `refkv_source`'s source metadata: a
[`Mirror`](../conventions.md#type-mirror) with these members, and no `empty_dirs`:

| member | type | presence | value |
|---|---|---|---|
| `ir` | [`IrDescription`](../conventions.md#type-irdescription) | required | the mirror's description: a store IR's, with `objects`, for a listed store ([conventions §8.9](../conventions.md#89-stores)); a file IR's for a zip file |
| `ignored` | array of string | optional | the ignored keys in ascending order of their UTF-8 bytes; present only when there is at least one |

## 6. Example

The array `a` of the store `https://example.org/h.zarr/`, whose `zarr.json` is

```json
{
  "shape": [4, 6],
  "data_type": "int16",
  "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [2, 3]}},
  "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
  "fill_value": -1,
  "codecs": [{"name": "bytes", "configuration": {"endian": "little"}}, {"name": "zstd", "configuration": {"level": 0, "checksum": false}}],
  "attributes": {"units": "m", "zarr_conventions": [{"uuid": "f17cb550-5864-4468-aeb7-f3180cfb622f", "name": "proj:"}], "refkv_zarrified": {"profile": "tiff"}},
  "dimension_names": ["y", "x"],
  "zarr_format": 3,
  "node_type": "array",
  "storage_transformers": []
}
```

has, in the output, the `zarr.json`

```json
{
  "shape": [4, 6],
  "data_type": "int16",
  "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [2, 3]}},
  "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
  "fill_value": -1,
  "codecs": [{"name": "bytes", "configuration": {"endian": "little"}}, {"name": "zstd", "configuration": {"level": 0, "checksum": false}}],
  "attributes": {
    "units": "m",
    "zarr_conventions": [
      {
        "uuid": "95203d0a-6586-426a-b920-3db2f4f1e97e",
        "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/zarrify/zarr3/schema.json",
        "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr3.md",
        "name": "refkv_zarrified",
        "description": "The Zarr layout of a Zarr v3 source zarrified by refkv, and the source's metadata"
      },
      {"uuid": "f17cb550-5864-4468-aeb7-f3180cfb622f", "name": "proj:"}
    ],
    "refkv_zarrified": {"zarr3": {"attributes": {"refkv_zarrified": {"profile": "tiff"}}}}
  },
  "dimension_names": ["y", "x"],
  "zarr_format": 3,
  "node_type": "array",
  "storage_transformers": []
}
```

and its present chunks `a/c/0/0`, `a/c/0/1` and `a/c/1/0` are entries referencing the objects of
those keys whole. An array of the same store without moved attributes has its `zarr.json` as
written.

## What the layout does not express

This section is informative.

- **Inner chunks of a shard** are not entries: a shard is one reference ([§3.2](#32-chunks)). A
  reader that does not know `sharding_indexed` reads neither the store nor the output.
- **Consolidated metadata** is only in the mirror ([§2](#2-nodes)); a reader that opens the
  output with consolidated metadata required finds none.
- **Implicit groups**: the output gives them a `zarr.json` the store does not have
  ([§2](#2-nodes)); only the mirror's listing tells them apart.
- **Codecs are not checked** ([§3](#3-arrays)): an archive may name a codec no reader has, as its
  store does.
- **Compressed zip entries**: an array whose chunks are compressed in the zip file is left out
  ([§7.4](#74-the-zip-form)); its bytes are the mirror's.

# Part 2. The profile

The Zarr v3 profile of [spec/zarrify.md](../zarrify.md) is [§7](#7-zarr-v3-profile) of this
document. What every profile's Part 2 shares is in
[spec/zarrify.md, Profiles](../zarrify.md#profiles).

## 7. Zarr v3 profile

A Zarr v3 store is read in one of two forms:

| form | input | `U` | its objects |
|---|---|---|---|
| **listed** | a store input ([spec/zarrify.md §1.4](../zarrify.md#14-store-inputs)): a store URL, or a local directory with the URL it is served from | the store URL, ending in `/` | the listing's ([spec/zarrify.md §1.5](../zarrify.md#15-listing-a-store)) |
| **zipped** | a file input ([spec/zarrify.md §1.2](../zarrify.md#12-input)) | the zip file's URL | its entries ([§7.4](#74-the-zip-form)) |

The two forms give the same hierarchy for the same store, but for `source.url`, the chunks'
sources, the other objects, `refkv_source` (the IR mirror of a store or of a file,
[§4.1](#41-elements)), and the nodes only a zip file leaves out: an array with a compressed chunk,
and a node whose `zarr.json` is compressed with a method other than deflate
([§7.4](#74-the-zip-form)). Every other document is the same, and every chunk holds the same
bytes.

**Numbers.** The documents' integer literals are kept exactly where the convention copies them
([its §4](#4-source-metadata)), beyond 2^53 − 1 too, in place of
[§1.6](../zarrify.md#16-json-documents)'s last paragraph; every number the convention uses (a
size, a chunk shape) is its binary64 value, as [§1.6](../zarrify.md#16-json-documents) says.

### 7.1 Choosing the profile

**Listed form.** [spec/zarrify.md §1.4](../zarrify.md#14-store-inputs)'s table of root keys
chooses it by a root key `zarr.json`, after the rows of the other profiles' root keys. A store
whose root has `.zarray` or `.zgroup` as well is read as Zarr v2, whose convention keeps the
`zarr.json` as an other object ([zarr2.md §5](zarr2.md#5-other-objects)), as it did before this
profile; zarr-python 3 would open such a store as Zarr v3, and a store that holds both versions
is left to the version its existing profile reads. In each host the choosers test the keys as
`choose_profile` in `python/src/refkv/zarrify/store/stores.py` does.

**Zipped form.** The **signature** ([spec/zarrify.md §1.2](../zarrify.md#12-input), tested at the
precedence of [profiles/zarr3.json](profiles/zarr3.json), after NumPy's `.npz` and before SAFE's
zip form): bytes 0–3 of `H` are `50 4B 03 04`, a ZIP local file header, and its name (bytes from
30, of the length in bytes 26–27, as far as `H` holds them) does not start with a segment ending
in `.SAFE` followed by `/`. So a zip file is a SAFE product's when its first entry is under a
`.SAFE` directory ([safe.md §8.1](safe.md#81-choosing-the-profile)), and this profile's otherwise.
zarr-python's `ZipStore` writes the root's `zarr.json` first; a zip file of a directory store
made with `zip -r` names its entries under the store's directory, which this profile reads as a
store whose root has no `zarr.json`, and rejects.

### 7.2 Reading

**Listed form.** The store is listed ([§1.5](../zarrify.md#15-listing-a-store)), then every
candidate's `zarr.json` ([§2](#2-nodes)) that is neither empty nor larger than
[§1.6](../zarrify.md#16-json-documents) allows is read whole, in one round, concurrently
([conventions §8.9](../conventions.md#89-stores)), each once; then the documents are checked from
the root down. So a document that cannot be read is a failure even when another document read in
the same round would reject the input. The `zarr.json` of a candidate below an array is read
with the others, since which candidates are arrays is known only once they are read; it is then an
other object. No other object is read.

### 7.3 Chunks and other objects

Every chunk that the convention says is present ([§3.2](#32-chunks)) is an entry under its own
key, referencing the whole chunk object: in the listed form through its own `url` source
([§1.4](../zarrify.md#14-store-inputs)), in the zipped form as the range of the zip file its
entry's data is ([§7.4](#74-the-zip-form)). A chunk object of size 0 has no entry.

In the listed form every other object of [§5](#5-other-objects) is referenced whole in the same
way, by an entry under the key `refkv_source/objects/<k>`, as
[the Zarr v2 profile](zarr2.md#72-chunks-and-other-objects) does; the sources are in ascending
order of their entries' keys. The ignored keys are the member `ignored` of `refkv_source`'s
source metadata ([§5](#5-other-objects)). The documents under `refkv_source/` (the mirror) are
deflated and, like the chunks, not among the documents a reader fetches when it opens the archive
([conventions §2](../conventions.md#2-attributes)).

### 7.4 The zip form

The file is a ZIP archive (PKWARE APPNOTE 6.3), read from its end as
[the SAFE profile's zip form](safe.md#84-the-zip-form) reads one: its **end of central
directory**, ZIP64 records and **central directory** by the same rules, with the same messages.
Then:

- **Names.** An entry's name is a key. A key that
  [spec/zarrify.md §1.4](../zarrify.md#14-store-inputs) ignores (empty, ending in `/`, with an
  empty, `.` or `..` segment) is an **ignored entry**, recorded as [§1.4](../zarrify.md#14-store-inputs) records a listed key, with
  the entry's size `us` as its size: a directory entry of size 0 is not recorded. (Unlike SAFE,
  the store's root is the zip file's root, not a directory in it.)
- **Superseded entries.** When several entries have one name, the last in the central directory
  is the object, and the others are **superseded**: they are not read, and their bytes are the
  mirror's gaps. zarr-python's `ZipStore` rewrites a document by adding an entry of the same
  name, and reads the last, as Python's `zipfile` does.
- **The store's objects** are the other entries, the last of each name, with size `us`. The
  candidates and the nodes are theirs, as in [§2](#2-nodes); a zip file whose root has no
  `zarr.json` is rejected (`not a Zarr v3 store: the root has no zarr.json`).
- **Local headers.** For each object of size `us > 0`, the 30 bytes at its local header offset
  `lho` are read as [the SAFE profile's](safe.md#84-the-zip-form) are: they MUST be a local file
  header, its data starts at `ds = lho + 30 + nn + ne` from the local header's own lengths, `ds + cs`
  MUST be at most the central directory's offset, and the objects' ranges `[lho, ds + cs)` MUST NOT
  overlap.
- **Methods.** An object with method 0 (stored) MUST have `cs = us` (`the stored zip entry <k>
  has a compressed size other than its size`); its bytes are `[ds, ds + us)` of the file. An object
  of another method is not read and is not checked, but:
  - a node's `zarr.json` with method 8 (deflate), whose `cs` and `us` are each at most
    [§1.6](../zarrify.md#16-json-documents)'s limit, is read whole and inflated: the raw deflate
    stream MUST end exactly at `cs` bytes and give exactly `us` bytes, whose CRC-32 MUST be the
    entry's, or the document cannot be read. The `us` of all of them together MUST be at most 2^27
    (`the deflated documents are larger than 2^27 bytes together`), checked before any is read;
  - a node's `zarr.json` of any other method cannot be read (`zarr.json: the zip entry has the
    compression method <m>`, the path before `zarr.json`): the node is not presented
    ([§3.3](#33-nodes-that-are-not-presented)), and the root rejects the input;
  - a chunk object of a presented array that is not stored and not empty means its array is not
    presented (`<p>: chunk <k> is compressed in the zip file (method <m>)`, the first such key),
    checked after the convention's rules; the root's rejects the input. (A chunk can be
    referenced in place only when it is stored. zarr-python's `ZipStore` stores every entry, and
    VirtualiZarr's `ZippedZarrParser` rejects a zip file with any compressed entry.)

The profile reads, in rounds: the file's last `min(size, 65557)` bytes; the ZIP64 locator and
record, when there are; the central directory; every object's local header; then the candidates'
documents; then the values the view shows that it did not read
([conventions §8.7](../conventions.md#87-the-view)). It reads no chunk. Each local header is a
read of 30 bytes, which the read planner merges where they are close; a zip file of many large
chunks costs a request per chunk here. (VirtualiZarr presumes that a local header's lengths are
the central directory's, and reads none when the entries are then contiguous; this profile reads
them, so that a reference never rests on a presumption.)

### 7.5 Rejection

The input is rejected when a rule of [§1.4](../zarrify.md#14-store-inputs)–[§1.6](../zarrify.md#16-json-documents)
(listed form) or [§1.2](../zarrify.md#12-input) and [§7.4](#74-the-zip-form) (zipped form) fails,
and when the convention gives it no layout: wherever it says that the input is rejected, or that
something MUST hold and it does not. A node below the root that fails its rules is left out
instead ([the convention §3.3](#33-nodes-that-are-not-presented)). The rejection messages of the
roles are the source model's, [schema/zarr3.json](../../rust/refkv-ir/schema/zarr3.json).

### 7.6 Summary

This section is informative. Both implementations print a one-line JSON summary, with these
members:

| member | value |
|---|---|
| `groups` | the groups, implicit or not |
| `arrays` | the arrays presented |
| `shardedArrays` | the arrays presented with the codec `sharding_indexed` |
| `notPresented` | the nodes left out, when there are any |
| `chunks` | the number of chunk entries |
| `emptyChunks` | the chunk objects of size 0 |
| `objects` | the store's objects |
| `otherObjects` | the listed form's entries under `refkv_source/objects/` |
| `form`, `entries`, `superseded`, `emptyObjects` | the zipped form's: `"zip"`, the zip file's entries, the superseded ones, and the objects of size 0 |
| `listingRequests` | the listed form's listing requests |
| `reads` or `planner` | the Rust core's counts: the object reads, or the file run's requests |
| `elements` | the Rust core's count: the IR's elements |
| `folded` | the Rust core's count: what the mirror's table folded |
