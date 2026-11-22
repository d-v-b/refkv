# netCDF3

The Zarr layout of a netCDF3 file: an array per variable, with the file's and the variables'
attributes and the variables' dimension names, so that xarray reads a zarrified netCDF3 file as it
reads the file itself. What all of refkv's conventions share is in
[spec/conventions.md](../conventions.md), cited here as "conventions §n". How refkv produces this
layout as a reference store, which files it reads and which inputs it rejects, is the netCDF3
profile, [Part 2](#part-2-the-profile) below.

The format is the netCDF classic format in its three versions: the classic format (CDF-1) and the
64-bit offset format (CDF-2) of the NetCDF Users Guide's "File Format Specification", and the
64-bit data format (CDF-5) of PnetCDF's "CDF-5 file format specification", which netCDF-C 4.4 and
later also read and write. They are cited here as "the format specification". The netCDF-C library
(`libsrc/v1hpg.c`, `libsrc/nc3internal.c`) is cited by its source files where it fixes what the
specification leaves open.

Convention version: 0 (until release, [conventions §1](../conventions.md#1-conventions)) · UUID: `b708fd91-025a-4c2e-91e2-543503ea163a` ·
Schema: [schema.json](netcdf3/schema.json)

Changes: [changes/netcdf3/](changes/netcdf3/), one file per change.

Added 2026-11-20.

This document has two parts ([conventions §11.1](../conventions.md#111-parts)).

Like HDF5's and HDF4's, a netCDF3 file's variables are not images: the hierarchy holds no OME-NGFF
metadata.

# Part 1. The convention

## Layout

| prefix | type | description |
|---|---|---|
| `zarr.json` | group | the file ([§2](#2-the-hierarchy)): its attributes, and the declaration with the root's source metadata ([§6.1](#61-the-root)) |
| `<name>/zarr.json` | array | a variable ([§3](#3-variables)), with its attributes and its source metadata ([§6.2](#62-variables)) |
| `<name>/c/<coords>`, `<name>/c` | chunk | a chunk of a variable's array ([§3.2](#32-chunks)); `<name>/c` for a variable of rank 0 |
| `refkv_source/**` | subtree | the IR mirror ([§6.3](#63-the-ir-mirror)) |

| placeholder | value |
|---|---|
| `<name>` | the variable's name ([§2](#2-the-hierarchy)) |
| `<coords>` | the chunk's grid coordinates, in decimal, one key segment each |

## 1. Declaration

The root and every variable's array declare the convention
([conventions §11.2](../conventions.md#112-declaration)), and so does `refkv_source`. Its CMO is:

```json
{
  "uuid": "b708fd91-025a-4c2e-91e2-543503ea163a",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/zarrify/netcdf3/schema.json",
  "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md",
  "name": "refkv_zarrified",
  "description": "The Zarr layout of a netCDF3 source zarrified by refkv, and the source's metadata"
}
```

## 2. The hierarchy

A netCDF3 file is a **header** and then the variables' data ([§7.2](#72-reading)). The header lists
the file's **dimensions**, each a name and a length, at most one of them of length 0, the
**unlimited** dimension; the file's **global attributes**; and its **variables**, each a name, the
dimensions of its axes, in order, its attributes, its type, and where its data begins. A variable
whose first dimension is of length 0 is a **record variable**; any other is a **fixed-size
variable**. The file is flat: the hierarchy's root is the file, its attributes the global
attributes ([§5](#5-attributes)), and its members an array per variable, in the header's order.

A variable is presented at its name, when that is a **node name**: valid UTF-8, not empty, not `.`,
`..` or `zarr.json` (the key of the root's own document), not starting with `__` (which Zarr v3
reserves), without `/`, and not `refkv_source` (reason `reserved`); otherwise it is not presented
(reason `name`). When two variables have one name, the first keeps it, and a later one is not
presented (reason `duplicate`). netCDF-C refuses to write a name that is empty, `.`, `..`, not
UTF-8, holds `/` or repeats another, so a file that has one was written by other software; it
writes `zarr.json`, a name starting with `__` and `refkv_source`. A variable not
presented is listed in the root's source metadata ([§6.1](#61-the-root)), and its bytes stay in the
IR mirror.

## 3. Variables

A variable's array:

- **Shape:** the lengths of its dimensions, in order; of a record variable, the first is the
  file's **record count** ([§7.3](#73-records)). A variable of no dimensions is a scalar, of shape
  `[]`.
- **Data type** by its type ([§3.1](#31-data-types)).
- **Fill value:** the variable's `_FillValue` attribute, when it is one value of the variable's
  type; else netCDF's default fill value of the type ([§3.1](#31-data-types)). An integer is the
  JSON integer of its value; a float is the encoding of [§5](#5-attributes). Every chunk the
  hierarchy writes holds bytes of the file, so the fill value is read only where a reader reads past
  them; it departs from [conventions §3](../conventions.md#3-arrays)'s fill value 0, as HDF5's and
  HDF4's do, because it is the value netCDF libraries read where a writer wrote nothing.
- **Dimension names:** the names of its dimensions, in order ([§4](#4-dimensions)).
- **Attributes:** the variable's ([§5](#5-attributes)).
- **Codecs:** `bytes`, `{"endian": "big"}` for a type of more than one byte: the format stores
  every value big-endian.

### 3.1 Data types

| type (code) | bytes | Zarr `data_type` | default fill value |
|---|---|---|---|
| `NC_BYTE` (1) | 1 | `int8` | −127 |
| `NC_CHAR` (2) | 1 | `{"name": "null_terminated_bytes", "configuration": {"length_bytes": 1}}` | `""` |
| `NC_SHORT` (3) | 2 | `int16` | −32767 |
| `NC_INT` (4) | 4 | `int32` | −2147483647 |
| `NC_FLOAT` (5) | 4 | `float32` | 9.9692099683868690 × 10^36 |
| `NC_DOUBLE` (6) | 8 | `float64` | 9.9692099683868690 × 10^36 |
| `NC_UBYTE` (7) | 1 | `uint8` | 255 |
| `NC_USHORT` (8) | 2 | `uint16` | 65535 |
| `NC_UINT` (9) | 4 | `uint32` | 4294967295 |
| `NC_INT64` (10) | 8 | `int64` | −9223372036854775806 |
| `NC_UINT64` (11) | 8 | `uint64` | 18446744073709551614 |

The default fill values are netCDF-C's `NC_FILL_*` (`netcdf.h`); the float one is 15 × 2^119,
exact in `float32` and `float64`. Codes 7 to 11 are of CDF-5 alone ([§7.4](#74-checks)).

A variable of `NC_CHAR` is an array of single characters, of its full shape, its last axis among
them, as netCDF-C, netCDF4-python and VirtualiZarr read it; xarray joins the characters of its last
axis into strings when it decodes it, as it does from the file. A `_FillValue` of one character
`c` makes its fill value `""` when `c` is 0, and otherwise the base64 of `c`, the encoding of a
`null_terminated_bytes` value.

### 3.2 Chunks

- **A fixed-size variable**'s data is its values, contiguous, in C order, from its `begin`
  ([§7.2](#72-reading)). They are cut as contiguous values
  ([conventions §7](../conventions.md#7-source-values-as-arrays)) with the chunk limit 2^20 bytes.
- **A record variable** is one chunk per record: its chunk shape is 1 along the record axis and its
  dimensions' lengths along the others, and the chunk at record `r` is the `s` bytes at
  `begin + r × recsize` ([§7.3](#73-records)), `s` the bytes of one record's values. The file
  interleaves the record variables a record at a time, so a record's values of one variable are the
  longest run of them that is one range of the file; a chunk of several records would be several
  ranges. A variable with no records has no chunk, and its chunk shape's first length is still 1.

Every chunk of a record variable, and every chunk of a fixed-size variable but an edge chunk of the
contiguous cut, is one range of the file ([§7.5](#75-chunk-references)).

## 4. Dimensions

A variable's `dimension_names` are its dimensions' names, each as text (UTF-8 if valid, else ISO
8859-1), in order. A dimension of one name is one dimension across the file, as netCDF's are, and a
variable named as a dimension, of rank 1 and of that dimension, is its **coordinate variable**,
which xarray reads as the coordinate of every array that has that dimension. Nothing marks it in
the hierarchy beyond its name and its dimension, as nothing marks it in the file.

A dimension no variable uses is in the root's source metadata alone ([§6.1](#61-the-root)).

## 5. Attributes

An attribute is a name, a type and its values. A node's attributes are, in the header's order, the
global attributes (the root's) or the variable's (its array's), each a member of the node's
attributes, by name, its value as JSON:

| type | JSON |
|---|---|
| `NC_CHAR` | its bytes, the trailing NUL bytes removed, as a text value ([conventions §6](../conventions.md#6-source-metadata-as-json)) |
| an integer type | a JSON integer, exact |
| `NC_FLOAT`, `NC_DOUBLE` | a **float value**: [hdf5.md §6](hdf5.md#6-attributes)'s encoding, the Zarr v3 fill value of the value in the float data type of its size |

One value is the value; none or several are an array of them (`NC_CHAR` is one text value of any
length). As in HDF5's convention ([hdf5.md §2.1](hdf5.md#21-declaration-and-attributes)), the
attributes are the node's own, beside `refkv_zarrified`, because they are the netCDF attributes
xarray reads.

**`_FillValue`.** A variable's `_FillValue` is in the form xarray's Zarr backend decodes for the
array's data type, as in [hdf5.md §6](hdf5.md#6-attributes): on a variable of a float type, one
number is the base64 of the 8 little-endian bytes of its value as a binary64; on a variable of an
integer type, one integer is the integer, and so is one float whose value is an integer. Any other
`_FillValue` is not presented (reason `fill_value`): one of several values, a float that is not an
integer (xarray-data's `eraint_uvz.nc` gives `int16` variables a `_FillValue` of NaN), and any on an
`NC_CHAR` variable, for which that backend decodes none. The header in the IR mirror still holds it,
and the array's fill value is still the `_FillValue` where it is one value of the variable's type
([§3](#3-variables)). A global attribute `_FillValue` is presented as any other.

When two attributes of a node have one name, the first is presented. An attribute named
`zarr_conventions` or `refkv_zarrified` is not presented (reason `reserved`). Each reason is in the
node's source metadata.

## 6. Source metadata

### 6.1 The root

<a id="type-netcdf3root"></a>**`Netcdf3Root`**, the root's `netcdf3` member:

| member | value |
|---|---|
| `format` | `"classic"` (CDF-1), `"64-bit offset"` (CDF-2) or `"64-bit data"` (CDF-5) |
| `version` | the version byte, 1, 2 or 5 |
| `numrecs` | the header's record count, or `"streaming"` for the all-ones value ([§7.3](#73-records)) |
| `records` | the record count the arrays use ([§7.3](#73-records)) |
| `recsize` | the bytes from a record's start to the next's |
| `header` | the bytes of the header ([§7.2](#72-reading)) |
| `dimensions` | each dimension's `{"name", "length"}`, in order, with `"unlimited": true` for one of length 0; the name a text value |
| `attributes` | each global attribute's `{"type", "count"}` (its type's name of [§3.1](#31-data-types), `NC_BYTE` and so on, and its count of values), by name |
| `unpresented_attributes` | when some are not presented: each one's reason, by name |
| `unpresented` | when some variables are not presented: `[{"name", "index", "reason"}]` in the header's order, the name a text value and `index` the variable's position in the header |

### 6.2 Variables

<a id="type-netcdf3variable"></a>**`Netcdf3Variable`**, a variable's array's `netcdf3` member: `index`, its position in the header's
variable list, from 0; `type`, its type's name; `record`, `true` for a record variable; `begin`, the
header's offset of its data; `vsize`, the header's `vsize`; and `attributes` and
`unpresented_attributes` as the root's.

### 6.3 The IR mirror

`refkv_source` is the file's IR mirror ([conventions §11.3](../conventions.md#113-the-ir-mirror)).
Its elements:

| path | kind | what |
|---|---|---|
| `header` | value, `bytes[n]` | the header, from byte 0 to the end of its variable list |
| `variables/<k>` | data | the values of the fixed-size variable at position `k` of the header, its form `{"recipe": [["src", 0, null]]}` |
| `records` | data | the records, from the first record variable's `begin` to the end of the last record or the file's end, whichever is first, when the file has a record variable and a record ([§7.3](#73-records)) |
| `gaps/<offset>` | gap | bytes no element claims: the padding after the header and between variables |

The mirror rebuilds the file.

### 6.4 Equivalence

Hierarchies of one file are compared entry for entry, `refkv_source` included
([conventions §11.4](../conventions.md#114-equivalence)).

### 6.5 What the layout does not express

This section is informative.

- **Strings.** An `NC_CHAR` variable is presented as characters, not as strings of its last axis,
  as HDF4's text datasets are ([hdf4.md §3.3](hdf4.md#33-text)): netCDF3 has no string type, and
  which axis holds a string's characters is a convention of readers (xarray's `_Encoding` and
  `char_dim_name`), not of the file.
- **Masked characters.** xarray's netCDF backend masks an `NC_CHAR` variable's values equal to its
  `_FillValue`; its Zarr backend decodes no `_FillValue` on such an array, so the zarrified
  variable reads unmasked ([§5](#5-attributes)).
- **The record count's growth.** The arrays hold the records the file has when it is zarrified.
- **Many small record chunks.** A record variable of rank 1, such as a time coordinate, is one
  chunk per value. A virtual shard ([zarrify §2.1](../zarrify.md#21-virtual-shards)) would hold
  them in fewer keys; this version keeps a chunk one range of the file, as VirtualiZarr's manifests
  need.
- **The header's reserved space** (netCDF-C's `h_minfree` and `v_align`), which is in the mirror's
  gaps only.

# Part 2. The profile

The netCDF3 profile of [spec/zarrify.md](../zarrify.md) is [§7](#7-netcdf3-profile) of this
document. What every profile's Part 2 shares is in
[spec/zarrify.md, Profiles](../zarrify.md#profiles).

## 7. netCDF3 profile

**Signature** ([spec/zarrify.md §1.2](../zarrify.md#12-input), tested at the precedence of
[profiles/netcdf3.json](profiles/netcdf3.json)): bytes 0–3 of `H` are `C`, `D`, `F` and the version
byte 1, 2 or 5.

### 7.1 Choosing the profile

The signature chooses the profile; no other profile reads a netCDF3 file. A netCDF4 file is an HDF5
file, which the HDF5 profile reads.

### 7.2 Reading

The header is, in the format specification's grammar, every number big-endian:

```
header    = magic numrecs dim_list gatt_list var_list
magic     = 'C' 'D' 'F' version            (version 1, 2 or 5)
dim_list  = ABSENT | NC_DIMENSION nelems [dim ...]
gatt_list = att_list
att_list  = ABSENT | NC_ATTRIBUTE nelems [attr ...]
var_list  = ABSENT | NC_VARIABLE nelems [var ...]
ABSENT    = ZERO nelems                    (both 0)
dim       = name dim_length
attr      = name nc_type nelems [values]   (padded to 4 bytes)
var       = name nelems [dimid ...] att_list nc_type vsize begin
name      = nelems namestring              (padded to 4 bytes)
```

The tags (`NC_DIMENSION` 10, `NC_VARIABLE` 11, `NC_ATTRIBUTE` 12, `ZERO`) and `nc_type` are 4
bytes. `numrecs`, `nelems`, `dim_length`, `dimid` and `vsize` are 4 bytes in CDF-1 and CDF-2 and 8
bytes in CDF-5; `begin` is 4 bytes in CDF-1 and 8 bytes in CDF-2 and CDF-5. Each is read unsigned.
Names, and attribute values, are padded with bytes to a multiple of 4. The header ends at the end of
the variable list. A name is its bytes to the first NUL byte, as netCDF-C reads it: the format
specification counts no NUL in `nelems`, but netCDF-C's own test file `ref_nctest_classic.nc` names
a variable `yy` in three bytes, `yy` and a NUL.

The profile reads in rounds: the first `min(2^16, size)` bytes, and then, while the header runs past
what was read, a prefix at least twice as long and at least as long as the structure that ran past
needs, to the file's end at most. It reads nothing else: the data
of a variable is never read, but for the edge chunks a projection copies
([§7.5](#75-chunk-references)).

The bytes of one record's values of a variable are its **slice** `s`: the element size of its type
times the lengths of its dimensions after the first. A variable's bytes are its element size times
its dimensions' lengths (a record variable's first counted as 1, for its slice); its **padded size**
is that rounded up to a multiple of 4. netCDF-C computes these itself (`NC_var_shape`), and so does
the profile: the header's `vsize` is recorded ([§6.2](#62-variables)), not used, since a writer
caps it at 2^32 − 1 for a variable of 4 GiB or more.

### 7.3 Records

The **record size** `recsize` is the sum of the record variables' padded sizes, but for a file of
one record variable, whose records are its slices one after another, unpadded (netCDF-C's
`NC_computeshapes`). The records start at `begin_rec`, the least `begin` of a record variable: the
record `r` of a record variable whose `begin` is `b` is the slice at `b + r × recsize`.

The **record count** is `numrecs`, unless `numrecs` is all ones (4 or 8 bytes of `0xFF`), which a
writer that streams the file leaves there: it is then `floor((F − begin_rec) / recsize)`, `F` the
file's size, as netCDF-C reads it (`NC_STREAMING`), and 0 for a file with no record variable.

### 7.4 Checks

A zarrifier MUST reject the input, with these messages, in this order:

- **Magic:** a file of fewer than 4 bytes, or not starting with `CDF` and the version byte 1, 2 or 5
  (`not a netCDF3 file`).
- **The header:** a header that runs past the file (`the header runs past the end of the file`), or
  past 2^26 bytes (`budget: the header is more than 67108864 bytes`); a list whose tag and count are
  neither its own tag nor both 0 (`the <list> has the tag <t> and <n> elements`, `<list>` the
  dimension list, the global attribute list, the variable list, or the attribute list of variable
  `<k>`); an attribute's type not among [§3.1](#31-data-types)'s (`the attribute <a> has the unknown
  type <t>`), or one of CDF-5 alone in a CDF-1 or CDF-2 file (`the attribute <a> has the type <t>,
  of CDF-5 only`); a variable's likewise (`variable <k> has the unknown type <t>`, `variable <k> has
  the type <t>, of CDF-5 only`); a variable's dimension that is not one of the file's (`variable <k>
  names the dimension <d>, of <n>`); a dimension of length 0 after a variable's first (`variable
  <k> has the unlimited dimension at axis <a>`). `<k>` is the variable's position in the header,
  and `<a>` the attribute's name as text.
- **The data**, of the variables in the header's order within each check: a variable of more than
  2^64 bytes (`variable <k> holds more than 2^64 bytes`); a fixed-size variable whose bytes run past
  the file (`variable <k> (<n> bytes at byte <o>) runs past the end of the file`) or begin inside
  the header (`variable <k> begins at byte <o>, inside the header of <h> bytes`); a record variable
  that begins inside the header, with the same message; two record variables whose slices overlap in
  a record, or one whose slice runs past the record's end (`the slices of record variable <k>
  overlap the next`, `<k>` the one of the lesser offset in the record); records that run past the
  file (`record <r> of variable <k> runs past the end of the file`, of the last record, the first of
  the variables whose slice runs past); and two fixed-size variables, or one and the records, whose
  bytes overlap (`variables <j> and <k> overlap`, `variable <k> and the records overlap`), the first
  such pair in the order of their first bytes.

netCDF-C reads some of these files: it reads bytes past the file's end as zeros, so a truncated
header or truncated data opens, and it reads the types of CDF-5 in a CDF-1 or CDF-2 file. The
profile rejects them, as scipy and VirtualiZarr do: no chunk can reference bytes the file does not
hold, and the format specification gives CDF-1 and CDF-2 six types.

No attribute, name or variable that the profile does not present rejects the input: the source
metadata records it ([§6.1](#61-the-root)).

### 7.5 Chunk references

<a id="type-netcdf3chunk"></a>**`Netcdf3Chunk`**, a chunk of a variable's array: one range of source
0, as [§3.2](#32-chunks) cuts it, but for an edge chunk of a fixed-size variable's contiguous cut,
which is the range and its zero padding, a range of the zero data source
([spec/zarrify.md §1.2](../zarrify.md#12-input)), or, where that reference would pass the payload
limit, the chunk's bytes, copied ([conventions §7](../conventions.md#7-source-values-as-arrays)).

### 7.6 Summary

Both implementations print `format`, `dimensions`, `variables` (the arrays presented),
`unpresented` (a count by reason), `records`, `header` (its bytes), `chunk_entries` (the chunks
written) and `rounds`.

### 7.7 The description

This section is not part of the convention: nothing it specifies is written in an output. The
parser returns, beside the IR, a **description** of the file: what it found of the source, for a
projection to read with the IR and its stored arrays
([spec/stored-array.md §7](../stored-array.md#7-the-formats)). It holds none of an output's
decisions: which variables are nodes and why the others are not ([§2](#2-the-hierarchy)), the
attributes presented and their forms ([§5](#5-attributes)), and the arrays' Zarr data types, fill
values, codecs, dimension names and chunks ([§3](#3-variables)) are the projection's. It is a JSON
object with these members:

| member | value |
|---|---|
| `version` | the version byte, 1, 2 or 5 |
| `numrecs` | the header's record count, or null for the all-ones value ([§7.3](#73-records)) |
| `records` | the record count ([§7.3](#73-records)) |
| `recsize` | the record size ([§7.3](#73-records)) |
| `header` | the bytes of the header ([§7.2](#72-reading)) |
| `dimensions` | each dimension, in the header's order: `name`, a text value ([conventions §6](../conventions.md#6-source-metadata-as-json)), and `length`, 0 for the unlimited one |
| `attributes` | the global attributes, in the header's order, each as below |
| `variables` | each variable, in the header's order, each as below |
| `elements` | the ids of IR elements ([§6.3](#63-the-ir-mirror)) by name: `header`, which keys the variables' stored arrays ([spec/stored-array.md §7](../stored-array.md#7-the-formats)) |

An attribute has these members:

| member | value |
|---|---|
| `name` | its name, a text value |
| `type` | its type's name ([§3.1](#31-data-types)), `NC_BYTE` and so on |
| `count` | its count of values, `nelems` |
| `value` | its values as JSON ([§5](#5-attributes)'s table): one value alone, none or several an array, `NC_CHAR` one text value without its trailing NUL bytes |
| `data` | an attribute of one value only: its bytes, big-endian, in hexadecimal |

and a variable these:

| member | value |
|---|---|
| `name` | its name, a text value: a string where it is UTF-8 |
| `dimensions` | the ids of its axes' dimensions, positions in `dimensions` |
| `type` | its type's name |
| `record` | `true` for a record variable |
| `shape` | its dimensions' lengths, a record variable's first the record count |
| `begin`, `vsize` | the header's `begin` and `vsize` |
| `attributes` | its attributes, each as above |

The variable at position `k` of `variables` is the stored array of index `k` keyed by the `header`
element. The projection reads a node's name from a variable's `name`, its data type and default
fill value from its `type` ([§3.1](#31-data-types)), its fill value and `_FillValue`'s form from
the `data` of its first `_FillValue` ([§3](#3-variables), [§5](#5-attributes)), its chunks from its
`shape`, `begin`, `type` and the file's `recsize` ([§3.2](#32-chunks)), and the source metadata
of [§6](#6-source-metadata) from the description as it is.
