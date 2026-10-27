# Implementation plan: the HDF5 profile, with NWB as its first target

This plan implements an HDF5 convention and profile (`spec/zarrify/hdf5.md`,
Part 1 and Part 2 of one document, as [fits.md](../../spec/zarrify/fits.md)
is) in the Rust core, reached from Python, the browser and the CLI, with
fixtures, a verifier, a public corpus and a docs page. The first target is
Neurodata Without Borders (NWB 2.x), whose files are HDF5 files. It is a
design note, not normative; the convention and the profile state the
decisions.

The input is **one HDF5 file**. The profile is generic: it presents every
group, dataset, attribute, link and reference that it can, records the
ones it cannot, and has no NWB-specific code in its hierarchy. NWB files
come out readable by pynwb because the convention adopts the Zarr v3
storage convention that hdmf-zarr 0.14 reads
([§1.3](#13-prior-art)). [§3](#3-the-profile-split) says why NWB is
neither a separate profile nor a layout in version 0.

## 1. Research summary

Sources read: The HDF Group's *HDF5 File Format Specification* version 3;
the subset of it that [ims.md §7.1](../../spec/zarrify/ims.md#71-hdf5-structures)–[§7.6](../../spec/zarrify/ims.md#76-attributes)
specifies and `rust/refkv-ir/src/hdf5.rs` reads; the IMS walk
([ims.md §5.3](../../spec/zarrify/ims.md#53-the-walk)) and its code in
`rust/refkv-ir/src/ims.rs`; the source code of LINDI (f48e993, March 2026),
kerchunk's `hdf.py` (37e8f90), VirtualiZarr's HDF parser (4fc1bc5),
hdmf-zarr (2eef251, release 0.14.0) and bendichter/zindi (79536a5); hdmf's
`H5SpecWriter`; the hdmf-common and NWB schemas; the zarr-extensions
registry (`string`, `struct`, `vlen-utf8`). Five DANDI files were surveyed
with h5py over HTTP range reads in October 2026 (a survey script that
counts layouts, filters, datatypes and links by walking the file), and
three of them were inspected in detail. Findings marked **[inspected]**
were checked against real files.

### 1.1 The format in one page

An HDF5 file is a graph of **objects** (groups, datasets and named
datatypes), each an **object header** of messages, reached from the
superblock's root group. A **group** maps link names to targets: a
**hard** link (an object header address; one object may have several), a
**soft** link (a path), or an **external** link (a file name and a path in
that file). Groups and datasets carry **attributes**: small typed values
held in the object header or, when there are many, in a fractal heap.

A **dataset** has a dataspace (scalar, simple of rank `k` with sizes and
maximum sizes, or null), a datatype, a filter pipeline and one of four
**layouts**:

| class | layout | where the data is |
|---|---|---|
| 0 | **compact** | inside the layout message, at most 64 KB |
| 1 | **contiguous** | one run of bytes in row-major order (unfiltered); unallocated until written |
| 2 | **chunked** | a chunk grid, each chunk filtered separately, found through a chunk index (v1 B-tree, single chunk, implicit, fixed array, extensible array, v2 B-tree) |
| 3 | **virtual** | other datasets, possibly in other files |

The **external files** message (type 0x07) gives a contiguous dataset's
storage as runs of other files.

Datatypes, by class: 0 fixed-point, 1 floating-point, 2 time, 3 fixed-length
string, 4 bitfield, 5 opaque, 6 compound (named members at byte offsets),
7 reference (an object reference is an 8-byte object header address; a
region reference is a 12-byte pointer to a global heap object that names an
object and a selection), 8 enumeration (a base integer and named values),
9 variable-length (a sequence or a string), 10 array. h5py writes a NumPy
`bool` as an enumeration of `int8` with the members `FALSE = 0` and
`TRUE = 1`.

A **variable-length** element is 16 bytes: a length (4), the address of a
**global heap collection** (8) and an object index in it (4). The
element's bytes are that heap object's data, so a vlen string is not in the
dataset's storage at all: the storage holds pointers.

### 1.2 How NWB uses it

An NWB 2.x file is an HDF5 file laid out by the NWB schema, which pynwb
writes through hdmf's HDF5 backend: groups for `acquisition`, `processing`,
`general`, `intervals`, `units`, `stimulus` and `specifications`; scalar
string datasets for `session_description`, `identifier`,
`session_start_time`; time series as a `data` dataset with `timestamps` or
a `starting_time`; tables (`DynamicTable`) as one dataset per column with
`VectorIndex` datasets for ragged columns. Every group and dataset has the
attributes `neurodata_type`, `namespace` and `object_id`.

The survey ([§1.4](#14-findings-that-shaped-the-design)), by file:

| file | NWB | size | datasets: contiguous / chunked | filters | vlen-string datasets | reference datasets / attributes | soft links | other |
|---|---|---|---|---|---|---|---|---|
| 000006 `sub-anm372907_ses-20170613.nwb` (2020 ecephys) | 2.0.2 | 259 KB | 57 / 0 | none | 33 | 1 / 3 | 1 | 3 fixed-string attributes |
| 000054 `sub-F3_ses-testing-day8_behavior+ophys.nwb` (ophys) | 2.9.0 | 81 MB | 85 / 0 | none | 43 | 0 / 2 | 6 | a compound dataset |
| 000021 `..._probe-729445654_ecephys.nwb` (Allen LFP) | 2.2.2 | 72 MB (4.7 GB logical) | 50 / 2 | deflate | 34 | 1 / 1 | 2 | a bool dataset and attribute |
| 000409 `sub-UCLA037_..._desc-raw_ecephys.nwb` (IBL raw) | 2.9.0 | 51 GB | 81 / 46 | deflate | 68 | 1 / 4 | 3 | |
| 000728 Allen Brain Observatory ophys | | | | | | | | h5py timed out after 240 s of metadata reads |

Every surveyed file has superblock version 0 (h5py's default, libver
`earliest`), so chunk indexes are version 1 B-trees and groups are
symbol tables. The bulk data is in a few chunked datasets with deflate
alone (no shuffle, blosc or zstd) and a fixed maximum shape. Most
datasets are contiguous, and the two small files are entirely contiguous.
About 40% of datasets and nearly all attributes are vlen strings. There
were no external links, external files, virtual datasets or compact
datasets.

### 1.3 Prior art

Four tools already reference NWB or HDF5 chunks. All four read the file
through h5py (libhdf5), so none runs in a browser, and each is defined by its
code rather than by a specification.

| | LINDI 0.4.6 | kerchunk `SingleHdf5ToZarr` | VirtualiZarr `HDFParser` | hdmf-zarr 0.14 / zindi |
|---|---|---|---|---|
| status | last commit 2026-03-09; pins `zarr<3` | maintenance mode (its README points to VirtualiZarr) | active | hdmf-zarr: release 2026-09-22; zindi: active |
| output | Zarr v2 reference JSON (`.lindi.json`, `.lindi.tar`) | Zarr v2 reference JSON | Zarr v3 `ManifestStore` | Zarr v3 (zindi: reference JSON) |
| contiguous | one chunk; split along axis 0 past 20 MB when unfiltered and numeric | one chunk, never split | one chunk (oversized unfiltered chunks trimmed along axis 0) | zindi: split at 4 MiB |
| compact | inlined when small; no other path | inlined under 500 bytes, else no chunk: reads as the fill value (inferred from code) | no handling: reads as the fill value (inferred) | zindi: inlined |
| vlen strings | inlined (JSON codec) | `vlen_encode`: embed (copy), null, leave (raw 16-byte pointers) or encode | read and inlined as `vlen-utf8` | inlined, `StringDType` |
| object references | `{"_REFERENCE": {object_id, path, source, source_object_id}}` in attributes and JSON elements | dropped (`None`; attributes become `str(ref)`) | behind a flag: path strings relative to the dataset; attributes `{"ref": {"node": …}}` | datasets: `StringDType` of absolute paths with `_DTYPE: "object_reference"`; attributes: `{"_REFERENCE": {"source": ".", "path": …}}` |
| region references | not handled | not handled | raise | not handled |
| compound | inlined JSON objects + `_COMPOUND_DTYPE` | structured dtype, referenced | referenced; with vlen or reference fields, read and inlined | v3 `struct`; string and reference fields as fixed UTF-32 (≥ 512 characters), listed in `_REFERENCE_FIELDS` |
| scalar | shape `(1,)` + `_SCALAR: true` | shape `()` | shape `()` | shape `()` |
| enum | bool kept; other mappings lost | lost | not handled (unverified) | `bool` |
| soft links | `_SOFT_LINK` group attribute | skipped | behind the flag; else followed and copied | `_LINKS: [{name, source, path}]` on the parent |
| external links | raise | not translated | behind the flag (`uri`); else fail | `_LINKS` with a relative `source` |
| filters | shuffle, deflate, blosc, zstd, fletcher32 | the same | deflate, shuffle, lzf, and hdf5plugin's (blosc, zstd, lz4 as `imagecodecs_lz4h5`, bzip2) | |
| chunk index | h5py `chunk_iter` | h5py `chunk_iter` | h5py `chunk_iter` | h5py |

The finding that decides the design: **hdmf-zarr 0.14.0 (September 2026)
reads and writes Zarr v3 only**, by a convention it shares with zindi and
LINDI ([hdmf-dev/hdmf-zarr#336](https://github.com/hdmf-dev/hdmf-zarr/pull/336)). In that convention:

- an object reference dataset is a `string` array of absolute paths with
  the attribute `_DTYPE: "object_reference"`;
- a reference attribute is `{"_REFERENCE": {"source": ".", "path": "/…"}}`;
- links are `_LINKS` on the parent group;
- a compound dataset is a `struct`, and a scalar is a 0-dimensional array;
- the root's `.specloc` names the group of cached specifications, and a
  `_REFERENCE` wrapper is accepted there.

hdmf-zarr reads v2 files only through `ZarrV2IO`, for conversion. pynwb's
`NWBZarrIO` opens any zarr-python store and needs `.specloc` to load the
cached namespaces. So a refkv output that follows this convention is an NWB
Zarr file that `NWBZarrIO` opens, with no copy and no conversion.

**What refkv adds** over the four:

- a specified profile, so that the Rust core and its browser build produce
  equivalent outputs, with fixtures and a corpus checked against h5py and
  pynwb;
- its own sans-IO HDF5 reader, which batches reads by rounds
  ([§4.2](#42-reading)) rather than following pointers one request at a
  time;
- structure only. No values are inlined: strings are referenced in place
  ([§2.4](#24-variable-length-strings)), and compact data is referenced in
  its object header;
- `size` and `etag` pins and optional CRC-32C checksums on every range;
- one `.vzip` archive rather than a JSON reference file;
- an IR mirror that rebuilds every byte of the file, including what the
  hierarchy cannot present;
- zarrifying in the browser, from a DANDI URL, with no libhdf5.

### 1.4 Findings that shaped the design

- **Small contiguous datasets dominate by count, a few large ones by
  bytes.** 000006 has 57 contiguous datasets, all at most 64 KiB and 45 at
  most 1 KiB. 000054 has 85: 54 at most 1 KiB, and five float32
  `(22769, 161)` traces of 14.66 MB each. 000021 has 50, 49 at most
  64 KiB, the largest a float64 `(875, 384)` of 2.69 MB. None was
  unallocated **[inspected]**.
- **Chunk shapes are the writer's.** 000021's LFP is float32
  `(12081218, 96)` in chunks of `(47193, 1)`, one channel per chunk, with
  deflate level 9. h5py reports 5.06 MB stored for 4.64 GB logical, so most
  chunks are unallocated or nearly constant. Its timestamps are
  `(11799,)` chunks **[inspected]**. A reference cannot rechunk these, so a
  reader of a time window across channels reads 96 chunks.
- **`/specifications` is scalar vlen strings.** Each namespace and schema
  file is a scalar vlen-string dataset of compact JSON, contiguous, from
  597 bytes (`core/2.0.2/namespace`) to 26,472 bytes
  (`core/2.9.0/nwb.icephys`). 000054 caches `core/2.9.0`,
  `hdmf-common/1.8.0` and `hdmf-experimental/0.5.0`. The root attribute
  `.specloc` is an object reference to `/specifications` **[inspected]**.
- **References are a handful per file, with fixed roles.** `electrodes/group`
  is a reference dataset of `(64,)` or `(96,)` pointers to the
  `ElectrodeGroup`s. Reference attributes are `table` on a
  `DynamicTableRegion` (`units/electrodes`, `…/electrodes` of an
  `ElectricalSeries`, `fluorescence/rois`) and `target` on a `VectorIndex`.
  All resolve to one path each **[inspected]**.
- **Soft links are absolute and short.** Examples are
  `…/device → /general/devices/…`, `…/imaging_plane → …` and the `rois`
  of four `RoiResponseSeries` → `…/fluorescence/rois` **[inspected]**.
- **The compound dataset is a packed record.** `PlaneSegmentation/pixel_mask`
  is `{x: <u4, y: <u4, weight: <f4}`, 19,570 elements, contiguous,
  12 bytes per element with no padding **[inspected]**.
- **Metadata reads are the cost.** h5py over fsspec gave up on 000728 after
  240 s, reading pointer by pointer. refkv's reader asks for every sibling
  structure of a round at once, so its round count is the depth of the
  pointer chains ([hdf5.rs](../../rust/refkv-ir/src/hdf5.rs), module
  docs).
- **DANDI's bucket serves what the profile needs.**
  `dandiarchive.s3.amazonaws.com/blobs/…` answers range requests with
  `Accept-Ranges: bytes`, a strong `ETag`, and `Access-Control-Allow-Origin: *`
  with `ETag` exposed, so browser zarrifying and `etag` pins both work
  **[inspected]**.

## 2. The convention (Part 1 of `spec/zarrify/hdf5.md`)

The hierarchy mirrors the HDF5 file's: the root group is the Zarr root, a
group is a Zarr group, and a dataset is a Zarr array at the same path. Its
**target format** is the hdmf-zarr Zarr v3 convention
([§1.3](#13-prior-art)): HDF5 attributes are Zarr attributes, and links and
references take that convention's reserved members. Nothing in it is
NWB-specific: those members encode HDF5 concepts, and an NWB file's
`.specloc` and `/specifications` are ordinary HDF5 objects that come out in
the form hdmf-zarr reads. refkv's own record sits beside them under
`refkv_zarrified`.

### 2.1 Declaration and the attributes rule

The root declares the convention with `"profile": "hdf5"`, `"version": 0`,
the file's URL as `source.url`, and the root's source metadata
([§2.9](#29-source-metadata)) as the member `"hdf5"`. Each dataset node
declares it with its own source metadata, and so does each group that has
any. `refkv_source` holds the IR mirror. The convention has a new UUID,
`schema_url` and `spec_url`.

This convention departs from
[conventions §2](../../spec/conventions.md#2-attributes), "Nothing else",
on purpose. A node's attributes hold, beside the declaration:

| member | from |
|---|---|
| each HDF5 attribute of the object, by name | the attribute, as JSON ([§2.7](#27-attributes)) |
| `_LINKS` | the group's soft links, extra hard links and external links ([§2.8](#28-links)) |
| `_DTYPE` | `"object_reference"` on a reference array ([§2.5](#25-object-references)) |

The conventions' "Nothing else" table gains a row for these members, defined
by the hdmf-zarr convention, on nodes of the HDF5 convention. The rule
exists so that source attributes cannot masquerade as a target format's
metadata. For HDF5 the attributes *are* the target format's metadata, as
xarray (for netCDF-4) and pynwb read them. An HDF5 attribute named
`zarr_conventions`, `refkv_zarrified`, `_LINKS`, `_DTYPE` or
`_REFERENCE_FIELDS` is not written as an attribute. It is listed as
unpresented ([§2.10](#210-what-is-not-presented)), and its value stays in
the mirror.

### 2.2 Nodes and paths

The walk ([§4.2](#42-reading)) gives every reachable object one **canonical
path**. The path is the first one found depth first from the root group,
with each group's hard links taken in ascending byte order of their names,
the order of [ims.md §5.3](../../spec/zarrify/ims.md#53-the-walk). Each
object is presented once, at that path. Every other hard link to it is a
link ([§2.8](#28-links)), and the path is also where references to the
object point ([§2.5](#25-object-references)). Different writers produce
the same order, so the hierarchy is a function of the file.

A link name is a Zarr node name only when it is valid UTF-8, is not `.` or
`..`, contains no `/`, and does not start with `__` (reserved by Zarr v3). A
root link named `refkv_source` collides with the mirror. Any such object is
unpresented under that link. A **named datatype** (a committed datatype
object) is not a node. Its datatype is recorded where datasets use it.

### 2.3 Datasets

A dataset is an array when its datatype, layout and filters are among
those below. Otherwise it is unpresented ([§2.10](#210-what-is-not-presented)).

**Shape:** the dataspace's sizes; `[]` for a scalar. A null dataspace is
unpresented. A maximum shape is source metadata.

**Data types**, by HDF5 datatype:

| HDF5 datatype | Zarr `data_type` | codec of step 3 ([conventions §3](../../spec/conventions.md#3-arrays)) |
|---|---|---|
| fixed-point of 1, 2, 4 or 8 bytes, offset 0, full precision | `int8`…`int64`, `uint8`…`uint64` | `bytes`, with the byte order as `endian` |
| IEEE binary16, binary32, binary64 | `float16`, `float32`, `float64` | `bytes` |
| enumeration of a 1-byte integer whose members are exactly `FALSE = 0` and `TRUE = 1` (h5py's `bool`) | `bool` | `bytes` |
| any other enumeration of an integer base | the base's | `bytes`; the members in source metadata |
| bitfield of 1, 2, 4 or 8 bytes | `uint8`…`uint64` | `bytes` |
| variable-length string (class 9, type 1), ASCII or UTF-8 | `string` | `vlen-utf8` ([§2.4](#24-variable-length-strings)) |
| fixed-length string of `n` bytes | `uint8`, with a trailing axis of `n` | `bytes`; the padding and character set in source metadata |
| object reference (class 7, 8 bytes) | `string` | `vlen-utf8` ([§2.5](#25-object-references)) |
| compound whose members, in offset order, tile its size with no gap, each a fixed-point, float, bool enumeration or nested such compound, all of one byte order | `struct` (zarr-extensions), fields in offset order | `bytes` |

Every other datatype makes the dataset unpresented in version 0:

- time, opaque, array and variable-length sequences;
- region references;
- compounds with a gap, mixed byte orders, or a string or reference
  member;
- integers with padding bits, and non-IEEE floats.

[§2.6](#26-compound-datatypes) gives the options for the compounds
version 0 leaves out.

**Fill value:** the HDF5 fill value as the data type's JSON fill value (0,
`false`, `""`, a struct's object of field values), 0 when none is defined.
This departs from the conventions' "fill value 0", because an unallocated
chunk of an HDF5 dataset reads as its fill value. A fill value for which
the data type has no JSON form makes the dataset unpresented.

**Codecs and chunks, by layout:**

| layout | chunks | codecs |
|---|---|---|
| **contiguous**, allocated | contiguous values ([conventions §7](../../spec/conventions.md#7-source-values-as-arrays)), cut with the **limit 2^20 bytes**, as MRC and FITS. A `(22769, 161)` float32 trace is 14 chunks of 1628 rows. Edge chunks are padded by a range of the zero data source or copied, as [fits.md](../../spec/zarrify/fits.md) does | step 3 |
| **contiguous**, unallocated (undefined address) | none (the fill value) | step 3 |
| **compact** | one chunk, the whole array: one range of source 0 into the layout message's data, which lies in the object header. A scalar's chunk shape is `[]` | step 3 |
| **chunked** | HDF5's chunk shape and grid, one Zarr chunk per allocated HDF5 chunk not wholly outside the dataspace, as [ims.md §7.8](../../spec/zarrify/ims.md#78-chunk-references) references it. HDF5 stores edge chunks whole, as Zarr does | `numcodecs.shuffle` (`elementsize` the datatype's size) for filter 2, step 3, then `numcodecs.zlib` for filter 1, in pipeline order |
| **virtual**, or contiguous with an **external files** message | unpresented | |

In version 0, the filters are deflate (1) and shuffle (2). A chunked
dataset is unpresented when any of these holds:

- it has any other filter;
- a chunk's filter mask is not 0;
- partial edge chunks are left unfiltered (layout flag bit 0);
- it is a string or reference dataset with filters.

[§11](#11-open-questions-and-their-decisions) lists the filters to add
from corpus evidence. A dataset with a size of 0 along some axis has no
chunks.

**Dimension names:** none. HDF5 has no dimension names, and NWB gives
dimensions only in its schema. Dimension scales (`DIMENSION_LIST`,
`CLASS = "DIMENSION_SCALE"`) are kept as attributes, and a later dataset
layout reads them ([§3](#3-the-profile-split)).

### 2.4 Variable-length strings

A vlen string dataset holds 16-byte pointers, and each string is the data
of a global heap object, a run of bytes in the file. Each element is
addressable by range, but a chunk's elements are scattered. Heap objects
carry 16-byte headers and 8-byte padding, so two strings are never
adjacent. A chunk therefore cannot be one range, but it can be a list of
ranges.

**The reference (recommended).** The `vlen-utf8` codec reads a chunk as a
little-endian `u32` element count, then each element as a `u32` byte
length and its bytes. A chunk of `c` elements is the reference:

- a literal of the count `c` and the first element's length (8 bytes);
- then, for each element, a range of source 0 over its heap object's data,
  followed by a literal of the next element's length (the last element is
  followed by nothing);
- an element of length 0, and each padding element of an edge chunk, is
  only its length literal, and adjacent literals are one literal.

No string is copied. The pointers are read from the dataset's storage
(structure, as FITS reads a compressed image's descriptors), and so are
the collections they name, to find each object's offset.

**Chunks.** A chunk's payload is at most 25 bytes per element. That
is an 8-byte wrapped length literal, plus a wrapped range of at most
17 bytes for an offset below 2^53 and a length below 2^32. A string array
is therefore cut as contiguous values
([conventions §7](../../spec/conventions.md#7-source-values-as-arrays))
with an element of 32 bytes and the limit 2^16. That gives at most 2048
elements per chunk, a payload of at most 51 KB, within the 65519-byte
limit of [spec/zarrify.md §1.2](../../spec/zarrify.md#12-input). A scalar
string is one chunk of one element. The largest cached schema, 26 KB, is
one range.

**The cost against "no copies".** Nothing is copied, but a short string
costs more in its reference than in its bytes. A 3-byte `location` string
(`"CA1"`) costs about 20 bytes of payload. A reader that does not join
nearby ranges makes one request per element. Most of an NWB file's strings
are scalars and short table columns, so the payloads stay small. A reader
fetches a collection's objects together when it joins ranges within a gap.
refkv's reader should do that for all references, and it is a reader
change, not a profile change.

**The alternative.** All four tools inline: they read the strings and write
the chunk's bytes into the reference file
([§1.3](#13-prior-art)). That reads string data at zarrify time and makes
the output depend on it, which breaks "Structure only"
([spec/zarrify.md §1.2](../../spec/zarrify.md#12-input)). The rule allows
copying an edge chunk's padding, not values, and inlining would give the
largest strings (the cached specifications, 10 to 26 KB each) a second
copy in the archive. **Decision: reference**
([§11](#11-open-questions-and-their-decisions)).

A string is UTF-8 by the datatype's character set. ASCII is a subset, so
both are `string`. A string whose bytes are not valid UTF-8 fails when it is
read (zarr-python's `vlen-utf8` decode), as a corrupt deflate stream does.
The profile cannot check it without reading the strings.

**Attributes** that are vlen strings are copied into JSON
([§2.7](#27-attributes)): an attribute is metadata, as a FITS card is, and a
Zarr attribute must be JSON.

### 2.5 Object references

An object reference is the address of an object header. The convention
gives each reference the target's canonical path ([§2.2](#22-nodes-and-paths)),
an absolute path from the root (`/general/extracellular_ephys/electrodes`).
A null (undefined) address, or an address that is not an object the walk
reached, is the empty string. The forms below are hdmf-zarr's:

- **Reference datasets** are `string` arrays of the paths with the
  attribute `_DTYPE: "object_reference"`. Their chunks are **bytes
  entries**, the `vlen-utf8` encoding of the paths. The paths are derived
  from the file's structure (the addresses and the walk), as a dataset
  layout's coordinate variables are derived from a header
  ([conventions §9](../../spec/conventions.md#9-dataset-layouts)). They are
  not a copy of any bytes. A reference array is cut as contiguous values
  with an element of 16 bytes (the stored pointer pair) and the limit 2^20.
- **Reference attributes** are `{"_REFERENCE": {"source": ".", "path":
  p}}`, an array of them for an attribute with several elements, and `null`
  for the empty path.

This gives `DynamicTableRegion.table`, `VectorIndex.target`,
`ElectricalSeries.electrodes`' table, `electrodes/group` and the root's
`.specloc` the form `NWBZarrIO` reads. hdmf-zarr dropped `object_id` and
`source_object_id` from references in 0.14. LINDI's older form carried
them, and the convention does not.

**Region references** (12 bytes, into the global heap) make a dataset or
attribute unpresented in version 0. hdmf-zarr has no form for them, NWB 2.x
does not write them (its `DynamicTableRegion` is integer indices plus an
object reference), and none appeared in the survey.

### 2.6 Compound datatypes

Zarr v3 core has no record type. Three options:

1. **The zarr-extensions `struct` data type.** It needs packed fields in
   order, with one byte order for the whole record (the `bytes` codec's
   `endian`). The bytes are HDF5's, referenced as they are, and hdmf-zarr
   0.14 writes and reads `struct`. It covers `pixel_mask`, `voxel_mask` and
   `image_mask`-style records. **Version 0 uses it** when the compound is
   packed, numeric and of one byte order ([§2.3](#23-datasets)).
2. **Bytes with a field table.** A `uint8` array `[..., size]`, with each
   member's name, offset and type in source metadata. This is the FITS
   tables precedent ([fits.md](../../spec/zarrify/fits.md)). It is lossless
   for any compound, including padded and mixed-endian ones, but no reader
   decodes it.
3. **One array per member.** Each member is a strided read. A reference
   could only express it with one range per element per chunk, which is far
   past the payload limit. So it would be a copy or a new
   strided-reference codec.

Compounds with a reference or string member are not covered by version 0.
NWB's `TimeSeriesReferenceVectorData` (`{idx_start: i4, count: i4,
timeseries: object reference}`, in `intervals/*/timeseries`) is one.
hdmf-zarr writes such members as fixed UTF-32 strings in a `struct`, which
transforms the bytes, so no reference can produce that form. Version 0
leaves them unpresented. Option 2 for them, and a later
member-splitting layout, are in
[§11](#11-open-questions-and-their-decisions).

### 2.7 Attributes

An attribute's JSON value follows
[conventions §6](../../spec/conventions.md#6-source-metadata-as-json)
(integers exact, floats as binary64 with `"NaN"` and the infinities as
strings, text as UTF-8 or the `{"latin1": …}` object). By datatype:

- **Scalars and arrays:** a scalar dataspace gives a value, a simple one
  nested JSON arrays by shape, and a null dataspace `null`.
- **Strings:** vlen and fixed-length strings are text. A fixed-length
  string's text ends at its first NUL.
- **Numbers and enumerations:** integers and floats are numbers, and h5py's
  bool enumeration is `true` or `false`. Other enumerations are their
  integer, with the members in source metadata.
- **References:** an object reference is `{"_REFERENCE": …}`
  ([§2.5](#25-object-references)).
- **Compounds:** an object of the member values.
- **Anything else is unpresented:** region references, opaque values and
  sequences.

Each attribute's HDF5 datatype is recorded in source metadata, because JSON
does not keep it (an `int32` and a `float64` of the same value are one JSON
number).

**Budget.** A node's attributes may take up to 2^16 bytes of JSON, as the
FITS cards and SAFE's XML do. Past that, the largest attributes are
unpresented, largest first, and their bytes stay in the mirror. NWB
attributes are short, and none in the survey came near the budget.

### 2.8 Links

A group's `_LINKS` lists, in ascending byte order of link name:

- every soft link, as `{"name": n, "source": ".", "path": p}`, with its
  path as written;
- every hard link that is not the target's canonical path, as
  `{"name": n, "source": ".", "path": canonical}`;
- every external link, recorded in the group's source metadata as `{"name",
  "file", "path"}` and **not** in `_LINKS`. hdmf-zarr's `source` names
  another Zarr store, and the target is an HDF5 file that this archive does
  not hold. It is not followed.

A relative soft link (no leading `/`) is kept as written. A dangling soft
link is kept too: Zarr has no links to check. The survey's soft links were
all absolute and resolved.

### 2.9 Source metadata

The root's `hdf5` member records the file's structure:

- the superblock's version and its sizes of offsets and lengths;
- the root group's object header address;
- `nwb` when the root has `neurodata_type = "NWBFile"`: `nwb_version` and
  the namespaces cached under `.specloc`, read from the group's link names
  (not the JSON). It is informative and read only for the summary
  ([§4.5](#45-summary)).

A dataset node's member records what the array does not express:

- `address`, its object header's address;
- `datatype`, a JSON description of the HDF5 datatype: its class, size,
  byte order, sign, string padding and character set, enumeration members,
  compound members with offsets, and the named datatype's path when it is
  shared;
- `maxshape`, with `null` for unlimited;
- `layout` (`compact`, `contiguous` or `chunked`) and, when chunked, the
  `index` type, the `chunk` shape and the `filters` (identifier, name,
  client data);
- `fill_value`, the raw bytes in base64 when it is defined;
- `attributes`, the HDF5 datatype of each attribute.

A group's member records:

- `address`;
- `attributes`;
- `external_links`;
- `unpresented`, its children and attributes that the hierarchy leaves
  out, each with a reason ([§2.10](#210-what-is-not-presented)).

### 2.10 What is not presented

An object or attribute that the convention cannot present is listed in its
parent's `unpresented`. It is listed by name with a reason drawn from a
fixed set:

| reason | when |
|---|---|
| `datatype` | the dataset's or attribute's datatype is outside the table |
| `filter` | a filter outside the version 0 set |
| `filter_mask` | a chunk's mask is not 0 |
| `edge_chunks` | partial edge chunks are left unfiltered |
| `layout` | the dataset is virtual |
| `external_storage` | it has an external files message |
| `null_dataspace` | the dataspace is null |
| `fill_value` | the fill value has no JSON form |
| `name` | the name is not a valid node name |
| `reserved` | the name collides with a reserved member or node |
| `budget` | over the attribute budget |

The file's bytes are all in the mirror, so nothing is lost: an unpresented
dataset is still rebuilt byte for byte from `refkv_source`, and h5py reads
the rebuilt file. "Lose no scientific information" holds for every file
the profile accepts. "Presented" is the stronger promise, made for the
table of [§2.3](#23-datasets).

This replaces rejection for everything that is a property of one object.
Other profiles reject what they do not cover, as FITS rejects random
groups. A generic HDF5 profile cannot do that: HDF5's space of types and
filters is large, and rejecting a 51 GB recording for one region-reference
attribute would present nothing. The profile rejects only a file whose
structure it cannot read ([§4.3](#43-checks)).

### 2.11 Source model (the IR)

These are the IMS elements of
[ims.md §5.2](../../spec/zarrify/ims.md#52-elements) unchanged, with three
changes:

- the walk reaches every object ([§4.2](#42-reading)), so there are no
  undescribed objects short of the budget;
- `objects/<i>/data` covers compact data as well as contiguous data;
- `global_heaps/<k>` covers the collections that dataset elements name, not
  only attributes'.

A dataset's chunks keep their `objects/<i>/chunks/<n>` elements. A vlen
string dataset's storage is a value of type `{len:<u4,heap:<u8,index:<u4}[n]`,
held by the parser and shown by the view. The projection reads the pointers
from it.

### 2.12 What the layout does not express

This list is informative, as
[mrc.md §7](../../spec/zarrify/mrc.md#7-what-the-layout-does-not-express)
is:

- NWB's dimension semantics (in the schema);
- dimension scales (kept as attributes);
- an object's several names, beyond `_LINKS`;
- region references, external links and external storage (recorded);
- the unpresented objects of [§2.10](#210-what-is-not-presented) (in the
  mirror);
- HDF5's free space and allocation (in the mirror).

## 3. The profile split

Three shapes were considered for the generic profile and its NWB layer.

1. **An `nwb` profile chosen by `hdf5`**, as the TIFF profile chooses NDPI
   and QPTIFF (`chosen_by` in [profiles/ndpi.json](../../spec/zarrify/profiles/ndpi.json)).
   Its hierarchy would be the same tree with NWB-specific attributes.
2. **An `nwb` layout** beside the generic tree (registry kind `layout`, as
   [profiles/mrc-dataset.json](../../spec/zarrify/profiles/mrc-dataset.json)),
   at `nwb/`, with hdmf-zarr's attributes on its nodes and the generic tree
   keeping source attributes under `refkv_zarrified`. `NWBZarrIO` would
   have to open a store rooted at `nwb/`.
3. **One `hdf5` profile whose convention is hdmf-zarr's**, with NWB files
   readable as they come.

**Recommendation: 3.** hdmf-zarr's convention is a generic HDF5-to-Zarr
convention: links, references, compounds and scalars. NWB's only
particulars, `.specloc` and `/specifications`, are HDF5 objects that come
out in hdmf-zarr's form unaided ([§2.5](#25-object-references)). Options
1 and 2 would duplicate the tree to change where attributes live. Option 2
would make every NWB user open a subpath. The registry gains `hdf5` alone,
and NWB appears in its phrases, the docs, the fixtures, the verifier and
the corpus.

**Layouts, later.** The layouts concept
([conventions §9](../../spec/conventions.md#9-dataset-layouts)) is for a
labeled-array view of the same bytes, which neither hdmf-zarr nor the
generic tree is. An `hdf5-dataset` layout would serve xarray in two cases.
For netCDF-4 and CF files, it would name dimensions from `DIMENSION_LIST`
and turn dimension scales into coordinates. For NWB, each `TimeSeries`
would become a dataset with `timestamps` (or `starting_time` and `rate`)
as its time coordinate. That is a convention of its own, not version 0.

**IMS.** Today the IMS profile sniffs the HDF5 signature at precedence 70
and rejects any non-Imaris HDF5 file. With a generic profile, `hdf5` takes
the signature and precedence. IMS becomes `sniff: null, chosen_by: "hdf5"`,
chosen when the root group has a hard link `DataSet` to a group with a
link `ResolutionLevel 0`, the test that
[ims.md §2.2](../../spec/zarrify/ims.md#22-the-imaris-layout) opens with.
An Imaris file then gets exactly today's output, and is still rejected
where IMS rejects it (LZ4). A non-Imaris HDF5 file, which IMS rejects
today, gets the generic hierarchy. A file named as `hdf5`
([spec/zarrify.md §1.7](../../spec/zarrify.md#17-named-inputs-and-layouts))
gets the generic hierarchy even when it is an Imaris file.

## 4. The profile (Part 2 of `spec/zarrify/hdf5.md`)

### 4.1 Choosing the profile

The signature is bytes 0–7, `89 48 44 46 0D 0A 1A 0A`, at IMS's precedence,
70. Version 0 does not search for a superblock at 512, 1024, … (a user
block). A file with a user block is rejected by its first bytes, as
today.

### 4.2 Reading

The profile reads HDF5 by the subset of
[ims.md §7.1](../../spec/zarrify/ims.md#71-hdf5-structures)–[§7.6](../../spec/zarrify/ims.md#76-attributes),
extended:

- **Data layout:** classes 0 (compact) and 1 (contiguous) are accepted,
  read as [ims.md §7.9](../../spec/zarrify/ims.md#79-source-metadata-structures)
  reads them. Class 3 and the external files message are read and recorded.
- **Datatypes:** versions 1 to 5, all classes decoded far enough to
  classify ([§2.3](#23-datasets)): the enumeration's base and members, the
  compound's members (offsets and nested types), and the variable-length
  type and character set.
- **Shared datatypes:** as [ims.md §7.9](../../spec/zarrify/ims.md#79-source-metadata-structures).
- **The global heap:** the collections that a presented vlen-string
  dataset's or attribute's elements name, read whole, as
  [ims.md §7.9](../../spec/zarrify/ims.md#79-source-metadata-structures)
  reads them.
- **Links:** external links (type 64) are decoded: a file name and a path,
  two NUL-terminated strings after a version byte.

**The walk** opens every group reachable by hard links from the root, by
level (all the groups of one level are opened in one round), and reads every
object's header, links and attributes. For a dataset it reads its messages
and either its chunk index or, for a vlen or reference dataset, its storage
(the pointers). This is the IMS walk with every object at level 0. The mirror
is described from what the walk read, and the IMS walk's code moves to a
shared module ([§5.1](#51-rust-core-rustrefkv-ir)).

**Rounds.** Each round asks for every structure its misses name. A
symbol-table group costs about three rounds (its B-tree, its symbol nodes
and the local heap, then the children's headers). A file's round count is
about 3 × its group depth, plus the chunk index depth, plus 2 for
strings. NWB depth is about 6, so about 25 rounds. 000728's
measurement goes in the corpus run.

### 4.3 Checks

Per structure, in this order, every listed check applying whatever the
others find:

- **Structure:** the rules of
  [ims.md §7.1](../../spec/zarrify/ims.md#71-hdf5-structures)–[§7.6](../../spec/zarrify/ims.md#76-attributes)
  for every structure the walk reads, as there. A rule failing in a structure
  the hierarchy depends on (a group's links, a presented dataset's messages
  or chunk index, an attribute's message, a presented string's collection)
  rejects the input, with the object's path in the message (`/units/spike_times:
  chunk 3 lies outside the file`).
- **Strings:** a presented vlen element's collection is defined and holds
  the object index, its length equals the object's data length, and an
  element of length 0 is accepted with any address.
- **References:** an object reference's address, when defined, is accepted
  whatever it is ([§2.5](#25-object-references): a non-object is the empty
  path).
- **Limits:** at most 2^17 objects; at most 64 levels; at most 2^22 vlen
  elements presented in all (2^26 bytes of pointers); a node's attributes as
  [§2.7](#27-attributes); the mirror's row budget of
  [spec/zarrify.md §1.1](../../spec/zarrify.md#11-output-and-equivalence).
  Over a limit, the input is rejected with `budget: …`.

There is no rejection by datatype, filter or layout:
[§2.10](#210-what-is-not-presented) lists those.

### 4.4 Chunks and the source table

Source 0 is the file. There is a zero data source when an edge chunk of a
contiguous array is padded, as for FITS. The reference forms are:

- contiguous: one range per chunk plus padding;
- compact: one range in the object header;
- chunked: one range per chunk;
- vlen strings: a list of literals and ranges;
- references: bytes entries.

`refkv_source` is the mirror.

### 4.5 Summary

Both implementations print:

- `objects` (groups, datasets and named datatypes);
- `arrays` (by layout);
- `unpresented` (count by reason, and the first few paths);
- `strings` (vlen elements presented);
- `references`;
- `links` (soft, hard and external);
- `chunks`;
- `nwb` (version and namespaces) when the file is NWB.

## 5. Modules

### 5.1 Rust core (`rust/refkv-ir`)

| file | what |
|---|---|
| `src/hdf5.rs` → `src/hdf5/mod.rs` | the reader as today, plus the datatype classifier ([§2.3](#23-datasets)): an enum of the presentable kinds with the Zarr data type, byte order, enum members, compound fields, vlen character set; the external link decode; a `vlen_elements(storage) -> Vec<(len, collection, index)>` helper |
| `src/hdf5/walk.rs` | the walk and mirror description moved out of `ims.rs` (`visit`, `walk`, `object`, `header`, `attribute`, `link`, `btree2`, `fheap`, `collection`), parameterized by the level-0 set and the room. `ims.rs` keeps its layout and calls it. A refactor with no change to IMS outputs, checked by the IMS fixtures and corpus |
| `src/hdf5/profile.rs` | the sans-IO parser: `detect`, `Hdf5::new(size)`, `step`, `feed`, `finish`, the canonical-path numbering, the per-object classification into presented and unpresented, the string pointers and collections, the reference resolution (address → canonical path) |
| `schema/hdf5.json` | the source model ([§2.11](#211-source-model-the-ir)), the datatype table, the unpresented reasons, and the checks of [§4.3](#43-checks) as roles with messages |
| `src/project/hdf5.rs` | the projection: group and array documents, attributes JSON with the budget, `_LINKS`, `_DTYPE`, the contiguous cut (`contiguous.rs`, `out::grid_chunks`, limit 2^20), compact and chunked references, the `vlen-utf8` reference lists, the reference arrays' bytes, source metadata, the summary |
| `src/run.rs`, `src/lib.rs`, `src/rules.rs`, `src/project/mod.rs` | `Format::Hdf5`, `detect`, `HDF5_SCHEMA`, dispatch, and IMS's selection by `chosen_by` |
| `tests/parsers.rs` | one `reject!` per rejection fixture |

No new codec: `string`/`vlen-utf8`, `struct`, `numcodecs.shuffle` and
`numcodecs.zlib` are zarr-python's.

### 5.2 Hosts

- **Python** (`python/src/refkv`): `ir/__init__.py` names the format. A
  helper so that `NWBZarrIO(path=refkv.zarr.open_store(url), mode="r")`
  works, documented with an example.
- **Browser** (`js/src/zarrify`): detection already finds the HDF5
  signature (for IMS), and `IrFormat` gains `"hdf5"`. The demo is
  unchanged: nothing here is an image.
- **CLI** (`rust/refkv-cli`): the format name. `tests/identical.py` picks
  up the fixtures.
- **Neuroglancer fork:** nothing.

## 6. Tests

`python/tests/ir/test_hdf5.py` and `test_nwb.py`, following the house rule:
one test of the accepted combinations, one per error.

- `test_zarrify_hdf5_outputs`, parametrized over the accepted fixtures,
  with h5py as the oracle:
  - the set of nodes equals h5py's `visititems` by canonical path;
  - each array read through the archive with zarr-python equals the
    dataset's value: strings decoded, references compared as
    `h[ref].name` against the canonical-path set, bool as bool, and a
    struct field by field;
  - each attribute equals `obj.attrs` by the rules of
    [§2.7](#27-attributes);
  - `_LINKS` equals the soft links and extra hard links;
  - `unpresented` names exactly the objects the fixture plants;
  - the mirror rebuilds the file byte for byte;
  - the browser twin's output is identical.
- `test_zarrify_nwb_reads`, over the NWB fixtures. pynwb's
  `NWBZarrIO` opens the archive through `refkv.zarr`, and the `NWBFile`
  equals `NWBHDF5IO`'s reading of the source:
  - `acquisition`, `processing` and `units` by name, with each `data` and
    `timestamps` equal;
  - `electrodes.to_dataframe()` equal, and each `DynamicTableRegion`
    resolving to the same rows;
  - the cached namespaces loaded.
  - The test pins that hdmf-zarr tolerates refkv's `zarr_conventions` and
    `refkv_zarrified` attributes and the `refkv_source` group
    ([§11](#11-open-questions-and-their-decisions), item 2).
- One test per rejection fixture (`hdf5_reject_*`).
- Unit tests of the classifier (each datatype class, enum detection,
  compound packing), the canonical-path order, the `vlen-utf8` reference
  list (literal joining, empty elements, edge padding, the payload bound),
  and the reference paths' encoding.
- The IMS tests run unchanged and pass: the selection moved, and the
  outputs did not.

## 7. Fixtures

`fixtures/generators/hdf5/write_fixtures.py` writes `fixtures/hdf5/*.h5` with
h5py (the IMS generator's untimed property lists, so the files are
reproducible). `fixtures/generators/nwb/write_fixtures.py` writes
`fixtures/hdf5/nwb_*.nwb` with pynwb, under fixed `identifier`,
`session_start_time`, `object_id`s and `file_create_date`, so the bytes
are stable. pynwb and hdmf-zarr become dev dependencies, and the package
never imports them. Values are seeded ramps.

Generic, accepted:

| fixture | covers |
|---|---|
| `hdf5_contiguous` | 1-D, 2-D and 3-D contiguous datasets of each numeric type, both byte orders; a cut into row bands; an edge chunk padded and one copied |
| `hdf5_contiguous_unallocated` | a contiguous dataset never written; a nonzero fill value |
| `hdf5_compact` | compact numeric and string datasets, a compact scalar |
| `hdf5_scalars` | scalar numeric, vlen-string and reference datasets |
| `hdf5_strings` | vlen strings: ASCII and UTF-8 character sets, empty, non-ASCII, 5000 elements (three chunks), 2-D; strings in several collections |
| `hdf5_fixed_strings` | fixed-length strings, null- and space-padded, as datasets and attributes |
| `hdf5_bool_enum` | h5py `bool`; an enum with other members; a bitfield |
| `hdf5_compound` | a packed `{u4,u4,f4}` (the `pixel_mask` shape), a nested compound, and the unpresented ones (a gap, mixed endianness, a string member, a reference member) |
| `hdf5_references` | reference datasets and attributes to groups and datasets, a null reference, a target with two hard links (the canonical path), a reference to a named datatype (empty path) |
| `hdf5_region_reference` | a region reference dataset and attribute (unpresented) |
| `hdf5_links` | absolute, relative and dangling soft links; a second hard link; an external link |
| `hdf5_external_storage`, `hdf5_virtual` | the unpresented layouts |
| `hdf5_filters` | deflate, shuffle + deflate, and the unpresented ones (fletcher32, lzf, a nonzero filter mask, unfiltered edge chunks) |
| `hdf5_unlimited` | unlimited dimensions under `earliest` (v1 B-tree) and `latest` (extensible array, v2 B-tree) |
| `hdf5_latest` | superblock 3, v2 object headers, dense groups and attributes, fixed array and single chunk indexes |
| `hdf5_names` | UTF-8 names, a name starting with `__`, a root link `refkv_source`, attributes named `_LINKS` and `refkv_zarrified` |
| `hdf5_attributes` | every attribute type of [§2.7](#27-attributes), a null-dataspace attribute, a node past the attribute budget |
| `hdf5_netcdf4` | a small netCDF-4 file written with h5netcdf: dimension scales and `DIMENSION_LIST` kept as attributes |

NWB (pynwb), accepted:

| fixture | covers |
|---|---|
| `nwb_ecephys` | `ElectricalSeries` with an electrodes `DynamicTableRegion`, devices and electrode groups (soft links), `electrodes/group` references |
| `nwb_ophys` | `ImagingPlane`, `PlaneSegmentation` with `pixel_mask` (struct) and `image_mask`, `RoiResponseSeries` linking `rois` |
| `nwb_units` | `Units` with ragged `spike_times` (`VectorIndex`), `obs_intervals` |
| `nwb_h5dataio` | `H5DataIO` with gzip and chunking, `maxshape` unlimited |
| `nwb_extension` | an `ndx-` extension namespace cached in `/specifications` |
| `nwb_trials_timeseries` | trials with a `timeseries` column (`TimeSeriesReferenceVectorData`): the compound with a reference member, unpresented in version 0, so that the gap is visible |
| `nwb_icephys` | the icephys tables (many `DynamicTable`s, `AlignedDynamicTable`) |

Rejected, one per check of [§4.3](#43-checks):

- `hdf5_reject_offsets_4`;
- `hdf5_reject_base_nonzero`;
- `hdf5_reject_truncated` (a group's B-tree beyond the end);
- `hdf5_reject_duplicate_link`;
- `hdf5_reject_chunk_outside`;
- `hdf5_reject_vlen_collection`, `hdf5_reject_vlen_index` and
  `hdf5_reject_vlen_length` (a string pointer naming no collection, a
  missing object, the wrong length);
- `hdf5_reject_too_many_objects`;
- `hdf5_reject_too_deep`;
- `hdf5_reject_too_many_strings`;
- `hdf5_reject_user_block`.

The IMS fixtures are run through `hdf5` detection to confirm the selection.

## 8. Verifier

`js/test/hdf5/verify.py` serves the fixtures over local HTTP and runs the
browser zarrifier under Node. It opens each archive through zarr-python
with `refkv.zarr` and checks:

- values and attributes against h5py ([§6](#6-tests));
- the NWB fixtures through `NWBZarrIO` against `NWBHDF5IO`;
- the mirror's rebuild;
- every `hdf5_reject_*` rejected with its message.

## 9. Corpus

`conformance/zarrify/corpus_hdf5.txt`, each entry `no-ref`, compared py with
web over HTTP ranges, from DANDI's public bucket (DANDI's dandisets are CC-BY
4.0 unless their metadata says otherwise, to record per entry):

| file | URL | why |
|---|---|---|
| 000006 `sub-anm372907_ses-20170613.nwb` (259 KB) | `https://dandiarchive.s3.amazonaws.com/blobs/321/9fd/3219fd98-6e52-4764-81ca-b780ee8fa326` | NWB 2.0.2, all contiguous, small: the quick case |
| 000006 `sub-anm369962_ses-20170313.nwb` (11 MB) | `https://dandiarchive.s3.amazonaws.com/blobs/1f1/a35/1f1a35b2-6025-4d93-a2bb-ba8aae74e7f5` | the same writer, larger tables |
| 000054 `sub-F3_ses-testing-day8_behavior+ophys.nwb` (81 MB) | `https://dandiarchive.s3.amazonaws.com/blobs/5ca/ea3/5caea343-caf7-4914-aa51-6379f9524c3b` | NWB 2.9.0, contiguous traces of 14.66 MB, the `pixel_mask` compound, six soft links, three cached namespaces |
| 000021 `sub-703279277_ses-719161530_probe-729445654_ecephys.nwb` (72 MB) | `https://dandiarchive.s3.amazonaws.com/blobs/a7f/c69/a7fc69c4-8fb2-4ee7-9e19-99222f31b8f4` | Allen LFP: chunked deflate-9 `(47193, 1)` chunks, bool data |
| 000409 `sub-UCLA037_ses-8d316998-…_desc-raw_ecephys.nwb` (51 GB) | `https://dandiarchive.s3.amazonaws.com/blobs/68f/d35/68fd35f1-8860-4612-9f00-c2a9582d41b3` | IBL raw ecephys: 46 chunked deflate datasets, 127 datasets, a large B-tree (chosen by the survey's size; its counts to confirm) |
| 000728 `sub-491604967_ses-501343881-StimC_behavior+image+ophys.nwb` (183 MB) | `https://dandiarchive.s3.amazonaws.com/blobs/0a5/f64/0a5f6405-d361-4d9e-8fde-8ca00be0f3b4` | Allen Brain Observatory, the dandiset whose metadata h5py timed out on (the surveyed file was not recorded; this is its smallest file): the rounds measurement |
| an OISST netCDF-4 day | the NOAA bucket of [docs/virtual-datasets.md](../../docs/virtual-datasets.md) | shuffle + deflate, dimension scales: the generic profile on a non-NWB file |

The IMS corpus (`corpus_ims.txt`) is re-run under the new selection, and its
outputs MUST NOT change. The URLs are DANDI's content-addressed blob URLs,
which do not change when a dandiset is republished. Each entry is pinned by
`ends-sha256`, as the IMS corpus is.

## 10. Order of work

1. **Interop spike.** Write by hand a tiny archive for `nwb_ecephys`'s
   shape and open it with `NWBZarrIO`. It must have attributes at top
   level, `_LINKS`, a `_DTYPE` reference array, `.specloc` as a
   `_REFERENCE`, and the refkv members beside them. This settles open
   question 2 before any code depends on it.
2. **Walk refactor:** move the IMS walk to `hdf5/walk.rs`; the IMS fixtures
   and corpus unchanged.
3. **Parser:** datatype classifier, external links, canonical paths, the
   presented/unpresented split, `schema/hdf5.json`, the generic fixtures,
   `tests/parsers.rs`.
4. **Projection, numbers:** groups, attributes, contiguous, compact,
   chunked, bool, enum, struct, `_LINKS`, source metadata, the summary.
   `test_hdf5.py` against h5py, and the mirror's rebuild.
5. **Strings and references:** pointer reads, collections, the
   `vlen-utf8` reference lists, the reference arrays and attributes.
6. **Selection:** registry entries (`hdf5`, IMS's `chosen_by`), detection
   in both hosts, CLI; `just ir-wasm`, `identical.py`, the verifier.
7. **NWB:** the pynwb fixtures, `test_nwb.py`, the `NWBZarrIO` helper and
   its docs.
8. **Spec, schema, docs** (`docs/zarrify/hdf5/README.md` with an NWB
   section), `just profiles`, `just sections`.
9. **Corpus run**, with the round counts and payload sizes recorded.

Steps 1–4 give every all-numeric HDF5 file. Step 5 gives NWB its tables and
metadata. Step 7 proves `NWBZarrIO`.

## 11. Open questions and their decisions

These are recommended answers; the convention states whatever is decided.

1. **Profile split.** **Decision:** one `hdf5` profile whose convention
   is hdmf-zarr's ([§3](#3-the-profile-split)). Not an `nwb` profile, and
   not an `nwb` layout. An `hdf5-dataset` layout for xarray comes later.
2. **hdmf-zarr's tolerance of refkv's members.** `NWBZarrIO` reads every
   attribute and group into builders. It is unverified whether pynwb
   ignores `zarr_conventions` and `refkv_zarrified` on each node and the
   root's `refkv_source` group, or fails. **Decision:** verify first
   ([§10](#10-order-of-work), step 1). If it fails, the fix belongs in
   refkv's reader: `refkv.zarr` can hide `refkv_*` members and nodes when
   asked, so the hierarchy keeps them. Changing the hierarchy for one
   reader is the last resort.
3. **Vlen strings: reference or inline.** **Decision:** reference, as a
   list of literals and heap ranges ([§2.4](#24-variable-length-strings)).
   Inlining reads data at zarrify time and breaks "Structure only". The
   cost is payload per element and requests per element; both are bounded,
   and reader-side range joining addresses the requests.
4. **Reference encoding.** **Decision:** hdmf-zarr's absolute paths
   (`_DTYPE: "object_reference"`, `{"_REFERENCE": …}`), by canonical path.
   Not VirtualiZarr's relative paths, LINDI's `object_id`s, or R-CF `ref`.
   The paths are the one form a released NWB reader consumes.
5. **Compounds with string or reference members.** **Decision:**
   unpresented in version 0. Then choose by corpus frequency of
   `TimeSeriesReferenceVectorData` between bytes with a field table
   ([§2.6](#26-compound-datatypes), option 2), which no reader decodes,
   and a member-splitting layout that copies.
6. **Fixed-length strings.** **Decision:** `uint8` with a trailing byte
   axis. Neither `string` (it needs the length without padding, which is
   data) nor `fixed_length_utf32` (not HDF5's bytes) references them. They
   are rare in NWB datasets: none in the survey, and three attributes.
7. **Contiguous chunk limit.** **Decision:** 2^20, as MRC and FITS.
   zindi uses 4 MiB and LINDI 20 MB. A smaller limit means more entries but
   reads closer to what was asked, and the conventions' cut keeps padding
   small.
8. **Unpresented, not rejected,** for per-object gaps. **Decision:** yes
   ([§2.10](#210-what-is-not-presented)). The mirror keeps the bytes, so
   no information is lost. Rejection is kept for unreadable structure and
   budgets.
9. **External links, external storage, virtual datasets.** **Decision:**
   record them, don't follow them, and don't reject the file. None appeared
   in the survey. Following them would make the profile a store input
   (other files as `url` sources), which is a later decision with its own
   rules for relative file names.
10. **Filters beyond deflate and shuffle.** **Decision:** add each when
    the corpus has a file that needs it, with fixtures:
    - zstd (32015, a raw zstd frame per chunk, to `zstd`);
    - blosc (32001, to `blosc`);
    - lz4 (32004, to `imagecodecs_lz4h5`);
    - fletcher32 (3, numcodecs' `fletcher32` if it is HDF5's layout,
      to verify);
    - lzf (32000, `imagecodecs_lzf`).

    The NWB survey found deflate alone.
11. **Sharding many small chunks** (the Allen LFP's 256 × 96 grid).
    **Decision:** no in version 0. Virtual shards
    ([spec/zarrify.md §2.1](../../spec/zarrify.md#21-virtual-shards)) cut
    keys, not reads, and these chunks are already the reads.
12. **The HDF5 subset's home.** The subset lives in [ims.md §7.1](../../spec/zarrify/ims.md#71-hdf5-structures)–[§7.6](../../spec/zarrify/ims.md#76-attributes), and
    two profiles would then read it. **Decision:** `hdf5.md` cites it in
    version 0 and states its extensions. Moving the text into `hdf5.md`,
    with ims.md citing it, is an editorial change after both are settled.
13. **User blocks** (the superblock at 512, 1024, …). **Decision:**
    rejected in version 0, as today. None of the corpus has one.
