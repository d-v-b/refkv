# HDF5

The Zarr layout of an HDF5 file, NWB files (Neurodata Without Borders 2.x) first among them:
a group per HDF5 group and an array per dataset, at their paths, with the file's attributes,
links and object references in the members of hdmf-zarr's Zarr v3 convention, so that pynwb's
`NWBZarrIO` opens a zarrified NWB file. What all of refkv's conventions share is in
[spec/conventions.md](../conventions.md), cited here as "conventions §n". How refkv produces
this layout as a reference store, and the subset of HDF5 that it reads, is the HDF5 profile,
[Part 2](#part-2-the-profile) below. The design note is
[design/plans/nwb.md](../../design/plans/nwb.md).

Convention version: 0 (until release, [conventions §1](../conventions.md#1-conventions)) · UUID: `6c006834-424b-451b-b786-f8c502ec32af` ·
Schema: [schema.json](hdf5/schema.json)

This document has two parts ([conventions §11.1](../conventions.md#111-parts)).

# Part 1. The convention

This convention gives a layout to every HDF5 file whose structure the profile reads
([§8.3](#83-checks)). An object or attribute it cannot present is left out of the hierarchy and
recorded ([§7.4](#74-what-is-not-presented)); its bytes stay in the IR mirror, from which the file
is rebuilt.

## Layout

| prefix | type | description |
|---|---|---|
| `zarr.json` | group | the HDF5 root group ([§2](#2-the-hierarchy)): its attributes, `_LINKS`, and the declaration with the root's source metadata ([§7.1](#71-groups)) |
| `<path>/zarr.json` | group | an HDF5 group presented at its canonical path ([§2.2](#22-paths)) |
| `<path>/zarr.json` | array | an HDF5 dataset presented at its canonical path ([§3](#3-datasets)), with its source metadata ([§7.2](#72-datasets)) |
| `<path>/c/<coords>`, `<path>/c` | chunk | a chunk of a dataset's array ([§3.2](#32-chunks)): `<path>/c` for a dataset of rank 0 |
| `refkv_source/**` | subtree | the IR mirror ([§7.3](#73-the-ir-mirror)) |

| placeholder | value |
|---|---|
| `<path>` | the object's canonical path ([§2.2](#22-paths)), its link names joined by `/` |
| `<coords>` | the chunk's grid coordinates, in decimal, one key segment each |

## 1. Declaration

The root, every dataset's array and every group with source metadata declare the convention
([conventions §11.2](../conventions.md#112-declaration)), and so does `refkv_source`. Its CMO is:

```json
{
  "uuid": "6c006834-424b-451b-b786-f8c502ec32af",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/zarrify/hdf5/schema.json",
  "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md",
  "name": "refkv_zarrified",
  "description": "The Zarr layout of a HDF5 source zarrified by refkv, and the source's metadata"
}
```

## 2. The hierarchy

The hierarchy is the HDF5 file's: the root group is the hierarchy's root, each HDF5 group a
Zarr group, and each dataset a Zarr array, at the same path. Its **target format** is the Zarr v3
convention of hdmf-zarr 0.14 (the hdmf-zarr documentation's "Zarr Storage Specification"),
which hdmf-zarr, zindi and LINDI share: HDF5 attributes are Zarr attributes, links are
`_LINKS`, object references are paths. Nothing in this convention is particular to NWB: an
NWB file's `.specloc` attribute and `/specifications` group are HDF5 objects, and come out as
hdmf-zarr reads them.

### 2.1 Declaration and attributes

A node's attributes are, in this order:

| member | presence | value |
|---|---|---|
| each HDF5 attribute of the object | when presented ([§6](#6-attributes)) | its value as JSON, by its name |
| `_LINKS` | a group with links other than its children's canonical ones ([§2.3](#23-links)) | its links |
| `_DTYPE` | an array of object references ([§5](#5-object-references)) | `"object_reference"` |
| `_REFERENCE_FIELDS` | an array of records with reference members ([§3.3](#33-compounds)) | their names, in member order |
| `zarr_conventions`, `refkv_zarrified` | as [§1](#1-declaration) says | the declaration ([conventions §2](../conventions.md#2-attributes)), with the node's source metadata ([§7](#7-source-metadata)) |

This departs from [conventions §2](../conventions.md#2-attributes) ("Nothing else"): the
source's attributes are not under `refkv_zarrified` but beside it, because for an HDF5 file they
are the target format's metadata, as xarray (for netCDF-4) and pynwb read them. An HDF5
attribute named `zarr_conventions`, `refkv_zarrified`, `_LINKS`, `_DTYPE` or
`_REFERENCE_FIELDS` is not presented (reason `reserved`).

### 2.2 Paths

The **canonical order** of the file's objects is depth first from the root group, each group's
hard links taken in ascending byte order of their names, each object once: an object's
**canonical path** is the first path that reaches it in that order, its link names joined by
`/` (the root's is empty; written `/` followed by it where this document writes an absolute
path). Each object is presented at most once, at its canonical path; every other hard link to it
is a link ([§2.3](#23-links)), and object references name it by its canonical path
([§5](#5-object-references)).

An object is a node only when its link name is a **node name**: valid UTF-8, not empty, not
`.` or `..`, and not starting with `__` (which Zarr v3 reserves); and, at the root, not
`refkv_source` (reason `reserved`). An object whose name is not one is not presented (reason
`name`), nor is anything below it. A link name that is not valid UTF-8 is read as ISO 8859-1
where it is written as text ([conventions §6](../conventions.md#6-source-metadata-as-json),
`Name`).

An object with a symbol table or link info message is a group; one with a data layout message
is a dataset ([§3](#3-datasets)); a named datatype (a committed datatype's object) is not
presented (reason `named_datatype`), nor is any other object (reason `object`).

### 2.3 Links

A group's `_LINKS` is the list, in ascending byte order of the links' names, of:

- each soft link: `{"name": n, "source": ".", "path": p}`, `p` its path as written (absolute or
  relative; a link that resolves to nothing is listed all the same);
- each hard link that is not its target's canonical path: `{"name": n, "source": ".", "path":
  p}`, `p` the target's canonical path, absolute.

An external link (type 64) is not followed, and is not in `_LINKS`, whose `source` names a Zarr
store: it is in the group's source metadata ([§7.1](#71-groups)). A link of any other type is
listed there too, its file and path `null`.

## 3. Datasets

A dataset is an array when its datatype, layout and filters are among those below; otherwise it
is not presented, with the reason [§7.4](#74-what-is-not-presented) gives.

- **Shape:** the dataspace's sizes (`[]` for a scalar), with a trailing axis of `n` for a
  fixed-length string of `n` bytes. A null dataspace is not presented (`null_dataspace`).
- **Fill value:** the fill value message's value as the data type's JSON fill value, else the
  data type's zero (`0`, `0.0`, `false`, `""`, a struct's object of its fields' zeros). This
  departs from conventions §3's fill value 0: an unallocated chunk of an HDF5 dataset reads as
  its fill value. A fill value the data type has no JSON form of is not presented
  (`fill_value`).
- **Dimension names:** none.
- **Attributes:** [§2.1](#21-declaration-and-attributes).

### 3.1 Data types

| HDF5 datatype | Zarr `data_type` | codecs before the filters |
|---|---|---|
| fixed-point of 1, 2, 4 or 8 bytes, bit offset 0, full precision | `int8` … `int64`, `uint8` … `uint64` | `bytes`, its byte order as `endian` (none for one byte) |
| bitfield of 1, 2, 4 or 8 bytes, bit offset 0, full precision | `uint8` … `uint64` | as above |
| IEEE binary16, binary32 or binary64 floating-point | `float16`, `float32`, `float64` | as above |
| enumeration of a 1-byte integer whose members are exactly `FALSE = 0` and `TRUE = 1` (h5py's `bool`) | `bool` | `bytes` |
| any other enumeration of an integer | its base's | as its base; the members in source metadata |
| compound of the kinds above (nested compounds included), packed: its members in offset order tile its size, each multi-byte member of one byte order | the zarr-extensions `struct`, its fields in offset order, each named as the member (valid UTF-8, unique) | `bytes` with that byte order (`little` when every member is one byte) |
| compound with variable-length string, fixed-length string or object reference members, packed, its other members of the kinds above but compounds | a `struct` of records ([§3.3](#33-compounds)) | `bytes`, `little` |
| fixed-length string of `n` bytes | `uint8`, with a trailing axis of `n` | `bytes` |
| variable-length string (class 9, type 1), ASCII or UTF-8 | `string` | `vlen-utf8` ([§4](#4-variable-length-strings)) |
| object reference (class 7, type 0, 8 bytes) | `string` | `vlen-utf8` ([§5](#5-object-references)) |

Any other datatype is not presented (`datatype`): time, opaque, array, and variable-length
sequences; region references and the revised references of datatype version 4; integers with
padding bits and floats that are not IEEE; compounds with a gap, two byte orders, or members of
other kinds.

**Filters.** A chunked dataset's filters, in pipeline order, are codecs after `bytes`: deflate
(1) is `{"name": "numcodecs.zlib", "configuration": {"level": 1}}` and shuffle (2)
`{"name": "numcodecs.shuffle", "configuration": {"elementsize": s}}`, `s` the datatype's size.
A dataset with any other filter is not presented (`filter`). Strings, references and records
have no filters: their chunks are written as [§4](#4-variable-length-strings),
[§5](#5-object-references) and [§3.3](#33-compounds) say.

### 3.2 Chunks

By the dataset's layout and data type:

| storage | chunk shape | chunks |
|---|---|---|
| **contiguous**, allocated | contiguous values ([conventions §7](../conventions.md#7-source-values-as-arrays)), cut with the limit 2^20 bytes, the element the datatype's (a byte for a fixed-length string) | one range of source 0 each; an edge chunk's padding a range of the zero data source, or the chunk copied with its padding when its reference would not fit in a payload ([spec/zarrify.md §1.2](../zarrify.md#12-input)), as the FITS profile's |
| **contiguous**, unallocated (its address undefined), or of size 0 | the shape (each size at least 1) | none: the array reads as its fill value |
| **compact** | the shape (each size at least 1) | one chunk, the range of source 0 of the data in the layout message; none for a size of 0 |
| **chunked** | HDF5's chunk shape (and the trailing axis) | one per allocated HDF5 chunk, its range of source 0 as the file stores it (HDF5 stores edge chunks whole, as Zarr does); an unallocated chunk has none |

The chunk key of grid coordinates `x` is `<path>/c/<x0>/<x1>/…`, `<path>/c` at rank 0; with a
trailing axis, its coordinate is 0. A chunked dataset of fixed size with a chunk whose filter
mask is not 0 (an optional filter skipped it, as deflate does bytes it cannot shrink) is not
presented (`filter_mask`): its codecs would differ by chunk; nor is one whose partial edge
chunks are stored unfiltered (layout flag bit 0, `edge_chunks`). A contiguous or compact
dataset whose storage is not its elements' size is not presented (`size`).

A virtual dataset (layout class 3, `layout`) and one with an external data files message
(`external_storage`) are not presented: their data is not in the file.

### 3.3 Compounds

A compound with a string or reference member is presented as hdmf-zarr writes one, a `struct`
whose fields are its members in offset order, each:

- a fixed-size member: its data type ([§3.1](#31-data-types)), little-endian;
- a string or object reference member: `{"name": "fixed_length_utf32", "configuration":
  {"length_bytes": 4L}}`, `L` the most code points of its texts, at least 1: a string's text
  (a variable-length string's heap object, a fixed-length string's bytes to its first NUL, read
  as UTF-8 or else ISO 8859-1), a reference's canonical path (empty for a null reference or one
  to no object of the walk).

The array has `_REFERENCE_FIELDS`, the reference members' names, when it has any. Its chunks are
**bytes entries**, the records written whole: the array is cut as contiguous values with the
record's size as the element and the limit 2^20 bytes, and each chunk is its records' bytes
(fixed-size members little-endian, texts as UTF-32LE padded with zero code units to `L`),
padded with zero bytes. This copies the records' values into the archive, the one departure
from "Structure only" ([spec/zarrify.md §1.2](../zarrify.md#12-input)): no range of the file
holds UTF-32 paths, and hdmf-zarr reads no other form. NWB's
`TimeSeriesReferenceVectorData` (a trials table's `timeseries` column) is such a compound.

## 4. Variable-length strings

A variable-length string dataset holds 16-byte elements: a length, a global heap collection's
address and an object index. Each string is that object's data, a run of the file's bytes. The
array's chunks are cut as contiguous values with the element 32 bytes and the limit 2^16, so at
most 2048 strings a chunk, and each chunk is the **reference** of the `vlen-utf8` encoding of its
strings:

- a literal of the chunk's string count, 4 bytes little-endian;
- for each string in order, a literal of its length, 4 bytes little-endian, then, when the
  length is at least 1, the range of source 0 of its heap object's data;
- for each element past the array's edge, a literal of length 0;
- each literal joined to the literal before it.

A string of length 0, or whose collection address is 0 or undefined, is empty. No string is
copied: the elements (the dataset's storage, its chunks decoded when it is chunked) are read
as structure, as are the collections they name. A string's bytes are UTF-8 by its datatype's
character set (ASCII being a subset); a string that is not valid UTF-8 is an error of the
reader that decodes it, not of the profile, which never reads it.

## 5. Object references

An object reference is an object header's address. It is presented as the object's canonical
path, absolute (`/general/extracellular_ephys/electrodes`), the empty string for a null
(undefined or 0) address or one that is no object of the walk:

- a dataset of references is a `string` array with the attribute `_DTYPE: "object_reference"`,
  cut as contiguous values with the element 16 bytes and the limit 2^20; its chunks are **bytes
  entries**, the `vlen-utf8` encoding of their paths (a count, then each path's length and
  UTF-8 bytes, the elements past the edge empty). The paths are derived from the file's
  structure (addresses and the walk), as a dataset layout's coordinates are derived from a
  header ([conventions §9](../conventions.md#9-dataset-layouts)), not copied;
- a reference attribute is `{"_REFERENCE": {"source": ".", "path": p}}`, `null` for a null
  reference.

## 6. Attributes

Each attribute of a presented object is a member of its node's attributes, in ascending byte
order of the attributes' names, its value as JSON
([conventions §6](../conventions.md#6-source-metadata-as-json)):

| datatype | JSON |
|---|---|
| an integer, a bitfield | a number (exact; past 2^53 − 1 in magnitude, the string of its digits) |
| an IEEE float | its binary64 value; NaN and the infinities as `"NaN"`, `"Infinity"`, `"-Infinity"` |
| h5py's `bool` | `true` or `false` |
| another enumeration | its integer |
| a fixed-length string | its text to its first NUL (and, space-padded, without trailing spaces) |
| a variable-length string | its heap object's text; `""` for an empty one |
| an object reference | [§5](#5-object-references) |
| a compound | an object of its members' values, by name |

A scalar dataspace gives the value, a simple one nested arrays by its sizes, and a null
dataspace `null`. Any other datatype, an attribute with a shared datatype or dataspace
(`shared`), and a string whose heap object cannot be read, are not presented.

**Budget.** A node's attributes may take 2^16 bytes, each counted as its value's JSON size plus
its name's length plus 4. Past that, the largest are left out (`budget`), the first in name
order of equal ones, until the rest fit.

## 7. Source metadata

### 7.1 Groups

A group's `hdf5` member:

| member | presence | value |
|---|---|---|
| `address` | required | its object header's address |
| `superblock` | the root's | the superblock's version |
| `attributes` | when it has attributes | each attribute's HDF5 datatype, described (below) |
| `unpresented_attributes` | when some are not presented | each one's reason, by name |
| `unpresented` | when some children are not presented | each child's reason ([§7.4](#74-what-is-not-presented)), by link name |
| `external_links` | when it has any | `[{"name", "type", "file", "path"}]`, in link name order: the file name and path of an external link (type 64; `null` for another type), as text |

### 7.2 Datasets

A dataset's array's `hdf5` member: `address`; `datatype`, its HDF5 datatype described;
`maxshape` when the dataspace has maximum sizes (`null` for unlimited); `layout` (`compact`,
`contiguous`, `chunked`); for a chunked one `chunks`, its HDF5 chunk shape, and `index`
(`btree1`, `single`, `implicit`, `fixed_array`, `extensible_array`, `btree2`); `filters`, the
filter identifiers, when it has any; `fill_value`, the fill value message's bytes in base64,
when defined; and `attributes` and `unpresented_attributes` as a group's.

A **described datatype** is an object with `class` (`integer`, `float`, `time`, `string`,
`bitfield`, `opaque`, `compound`, `reference`, `enum`, `vlen_string`, `vlen_sequence`,
`array`) and `size`, and by class: `order`, `signed`, `offset`, `precision` (integers and
bitfields); `order`, `ieee` (floats); `padding`, `charset` (strings); `tag` (opaque);
`members`, each `{"name", "offset", "datatype"}` (compounds); `kind` (`object`, `region`,
`other`); `base` and `members`, by name (enumerations); `base` (sequences); `dims` and `base`
(arrays).

### 7.3 The IR mirror

`refkv_source` is the file's IR mirror ([conventions §11.3](../conventions.md#113-the-ir-mirror)),
whose elements are the IMS convention's ([ims.md §5.2](ims.md#52-elements)): the superblock,
the objects in canonical order and then those no path reaches in ascending address order, and the
global heap collections that attributes and datasets of strings and records name. The walk
([§8.2](#82-reading)) reaches every object; the presented datasets' storage (their chunks and
contiguous data) is described whatever the IR's room, `2^21 + floor(size / 8)` elements, and
the other structures while it lasts. Every byte no element claims is a gap: the mirror rebuilds
the file.

### 7.4 What is not presented

| reason | what |
|---|---|
| `name` | a link name that is not a node name ([§2.2](#22-paths)) |
| `reserved` | a root link `refkv_source`; an attribute named as a member the hierarchy writes |
| `named_datatype`, `object` | a named datatype; an object that is neither a group nor a dataset |
| `datatype` | a dataset's or attribute's datatype outside [§3.1](#31-data-types) or [§6](#6-attributes) |
| `filter`, `filter_mask`, `edge_chunks` | a filter other than deflate and shuffle; a chunk a filter skipped; unfiltered partial edge chunks |
| `layout`, `external_storage`, `null_dataspace`, `size`, `fill_value` | [§3](#3-datasets) |
| `shared` | an attribute with a shared datatype or dataspace |
| `budget` | an attribute past its node's budget |

Every byte of an object not presented is in the mirror, so the hierarchy loses none of the
file: what it does not present it records, and the mirror rebuilds.

### 7.5 Equivalence

Hierarchies of one file are compared entry for entry, `refkv_source` included
([conventions §11.4](../conventions.md#114-equivalence)).

# Part 2. The profile

The HDF5 profile of [spec/zarrify.md](../zarrify.md) is [§8](#8-hdf5-profile) of this document. What every profile's Part 2 shares is in
[spec/zarrify.md, Profiles](../zarrify.md#profiles).

## 8. HDF5 profile

**Signature** ([spec/zarrify.md §1.2](../zarrify.md#12-input), tested at the precedence of
[profiles/hdf5.json](profiles/hdf5.json)): bytes 0–7 of `H` are the HDF5 signature
`89 48 44 46 0D 0A 1A 0A`. A file whose superblock is not at offset 0 (after a user block) does
not match.

### 8.1 Choosing the profile

The profile reads the root group's links first. When the root group has a link `DataSet` (hard,
or soft through hard links) to a group with a link `ResolutionLevel 0`, the file is an Imaris
file: the IMS profile reads it ([ims.md §7](ims.md#7-ims-profile); its registry entry's
`chosen_by` is `hdf5`), from its first bytes, and the output is that profile's. Otherwise this
profile reads it. A file named as the IMS profile's input is read by that profile alone
([spec/zarrify.md §1.7](../zarrify.md#17-named-inputs-and-layouts)).

### 8.2 Reading

The profile reads HDF5 by the subset of
[ims.md §7.1](ims.md#71-hdf5-structures)–[§7.6](ims.md#76-attributes) and the structures of
[ims.md §7.9](ims.md#79-source-metadata-structures), extended:

- **Data layouts** of classes 0 (compact) and 1 (contiguous) and 2 (chunked, with any fill
  value) are read for every dataset; class 3 (virtual) and the external data files message
  (type 0x07) are recognized and not read further.
- **Datatypes** of every class (versions 1 to 5) are decoded whole: a compound's members (in
  version 1, a member with dimensions rejects the input), an enumeration's members and values,
  an array's dimensions, a sequence's base; the revised references of version 4 are recognized
  and not decoded.
- **Soft links in symbol tables:** a symbol table entry of cache type 2 is a soft link, its value
  the local heap's NUL-terminated text at the scratch pad's first 4 bytes (its object header
  address is not read).
- **External links:** a link message of type 64's value is a byte of version and flags, then
  the file name and the path, each NUL-terminated.
- **The storage of strings, references and records** (contiguous: the bytes at its address;
  compact: the layout message's data; chunked: each allocated chunk, inflated (deflate) and
  unshuffled (shuffle) by the filters its mask does not skip, its elements placed by the chunk's
  grid coordinates, unallocated ones zero) and the global heap collections their strings name
  ([ims.md §7.9](ims.md#79-source-metadata-structures)); a collection that cannot be read has
  no objects.

**The walk** reads every object that hard links reach from the root group, by level: each
level's objects (each object header, a group's links, the attributes, a dataset's messages and
chunk index, as the IMS walk's `visit`, [ims.md §5.3](ims.md#53-the-walk)) in one round, until
a level is empty. Then each object is read again in canonical order by the rules above, which
reject; then the storage of strings, references and records; then the collections.

### 8.3 Checks

A rule of [§8.2](#82-reading)'s subset that fails for a structure the walk reaches rejects the
input, the message prefixed with the object's canonical path (`/units/spike_times: chunk [3]
lies outside the file`): its header, a group's links, its attributes, a dataset's messages and
chunk index, and the storage of a presented dataset of strings, references or records. Besides:

- **Strings:** each element of a presented string dataset of length at least 1, with a
  collection address other than 0 and undefined, names a collection that was read
  (`<path>: element k names no global heap collection`), an object it holds (`… names no object
  of its collection`), whose data is the element's length (`… is n bytes long, its heap object
  m`).
- **Data:** a contiguous dataset's data lies within the file.
- **Budgets:** at most 131072 objects (`budget: more than 131072 objects`); no object more than
  64 links deep (`budget: objects more than 64 links deep`); a dataset of strings, references
  or records of at most 2^26 bytes; at most 2^20 strings presented in all; the mirror's rows
  ([spec/zarrify.md §1.1](../zarrify.md#11-output-and-equivalence)).

No datatype, filter or layout rejects the input: [§7.4](#74-what-is-not-presented) records them.

### 8.4 Chunk references

<a id="type-hdf5chunk"></a>**`Hdf5Chunk`**, a chunk of a dataset's array:

| storage | parts |
|---|---|
| contiguous | the range of source 0, then its zero padding (a range of the zero data source) for an edge chunk; or bytes, the range's and the padding, when that would not fit in a payload |
| compact | the range of source 0 of the layout message's data |
| chunked | the range of source 0 of the chunk, as stored |
| strings | literals and ranges of source 0 ([§4](#4-variable-length-strings)) |
| references, records | bytes ([§5](#5-object-references), [§3.3](#33-compounds)) |

### 8.5 Summary

Both implementations print `objects` (the walk's), `groups` and `arrays` (presented),
`unpresented` (a count by reason), `strings` and `references` (elements presented), `links`
(`soft`, `hard` and `external` counts), `chunks` (the allocated chunks of the presented chunked
datasets), `rounds`, and for a file whose root has `neurodata_type = "NWBFile"`, `nwb`: its
`nwb_version` and the namespaces cached under `.specloc`'s group, each with its versions.
