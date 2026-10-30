# NumPy

The Zarr layout of a NumPy file: a `.npy` file, which holds one array, or a `.npz`
archive, a ZIP file of `.npy` members such as `np.savez` and `np.savez_compressed` write. What all of refkv's
conventions share is in [spec/conventions.md](../conventions.md), cited here as
"conventions §n". How refkv produces this layout as a reference store, and which files it
accepts, is the NumPy profile, [Part 2](#part-2-the-profile) below.

Convention version: 0 (until release, [conventions §1](../conventions.md#1-conventions)) · UUID: `62b1d1ba-8eb3-465c-a333-75e096fce519` ·
Schema: [schema.json](npy/schema.json)

Added 2026-10-30.

This document has two parts ([conventions §11.1](../conventions.md#111-parts)).

Like MP3 and HDF5, NumPy's arrays are not images: the hierarchy holds each array as a plain
Zarr array, with no OME-NGFF metadata. The values stay in the file; each chunk is a range of
it, or, for a member that `np.savez_compressed` deflated, the member's deflate stream as a gzip
member.

# Part 1. The convention

## Layout

The hierarchy's entries ([conventions §11.5](../conventions.md#115-layout-tables)):

| prefix | type | structure | description |
|---|---|---|---|
| `zarr.json` | group | [`GroupDocument`](../conventions.md#type-groupdocument), its attributes a [`Declaration`](../conventions.md#type-declaration) and nothing else | the root ([§4](#4-the-hierarchy)): no `ome` member, since the hierarchy is not an image |
| `zarr.json#/attributes/zarr_conventions` | attribute | array of [`ConventionMetadata`](../conventions.md#type-conventionmetadata) | the CMO of [§1](#1-declaration) |
| `zarr.json#/attributes/refkv_zarrified` | attribute | [`RootProperty`](../conventions.md#type-rootproperty) | `profile` `"npy"`, `version` `0`, `source.url` the file's URL |
| `zarr.json#/attributes/refkv_zarrified/npy` | attribute | [`NpyRoot`](#type-npyroot) | the root's source metadata ([§5.1](#51-the-root)) |
| `<name>/zarr.json` | array | [`NpyArray`](#type-npyarray) | an array ([§4.3](#43-the-arrays)): `array` for a `.npy` file, each presented member for a `.npz` archive ([§4.2](#42-an-npz-archive)) |
| `<name>/c/<coords>`, `<name>/c` | chunk | [`NpyChunk`](#type-npychunk) | every chunk of the array ([§8.4](#84-chunks)); `<name>/c` for an array of rank 0 |
| `refkv_source/**` | subtree | [the mirror's entries](../conventions.md#table-mirror) | the IR mirror ([§5.2](#52-the-ir-mirror)): its elements are [§5.3](#53-elements)'s |

| placeholder | value |
|---|---|
| `<name>` | `array` for a `.npy` file; a presented member's name without `.npy` for a `.npz` archive ([§4.2](#42-an-npz-archive)) |
| `<coords>` | the chunk's coordinates along the array's axes, in decimal, one key segment each, in the axes' order ([§4.3](#43-the-arrays)) |

## 1. Declaration

The root and `refkv_source` declare the convention
([conventions §11.2](../conventions.md#112-declaration)), the root with its source
metadata of [§5.1](#51-the-root) as the member `"npy"`. Its CMO is:

```json
{
  "uuid": "62b1d1ba-8eb3-465c-a333-75e096fce519",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/zarrify/npy/schema.json",
  "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md",
  "name": "refkv_zarrified",
  "description": "The Zarr layout of a NumPy source zarrified by refkv, and the source's metadata"
}
```

The root's attributes are `zarr_conventions` and `refkv_zarrified` and nothing else: there is
no `ome` member, since the hierarchy is not an image. The arrays have no attributes.

## 2. The NPY format

A `.npy` file, and each `.npy` member of a `.npz` archive, is the NPY format of NumPy's
NEP 1 ("A simple file format for NumPy arrays"), in one of its versions 1.0, 2.0 and 3.0. It
is a **preamble**, then a **header**, then the **data**. All integers are little-endian.

| bytes | field | type | value |
|---|---|---|---|
| 0–5 | `magic` | `bytes[6]` | `93 4E 55 4D 50 59` (`\x93NUMPY`) |
| 6 | `major` | `u8` | the version's major number: 1, 2 or 3 |
| 7 | `minor` | `u8` | its minor number: 0 |
| 8–9, or 8–11 | `header_length` | `u16` (version 1), `u32` (versions 2 and 3) | `n`, the header's length in bytes |

The preamble is `p` bytes: 10 for version 1, 12 for versions 2 and 3. The **header** is the
`n` bytes after it: text, in ISO 8859-1 for versions 1 and 2 and in UTF-8 for version 3,
holding a Python dictionary literal. NumPy writes it as `repr` writes a dictionary of three
members, `descr`, `fortran_order` and `shape`, followed by spaces and a newline that pad the
preamble and header to a multiple of 64 bytes (16 in older versions of NumPy):

```
{'descr': '<f8', 'fortran_order': False, 'shape': (3, 4), }
```

The data is the `B` bytes after the header, at offset `p + n`: the array's elements, each of
`e` bytes, in C order (the last index varying fastest) when `fortran_order` is false, and in
Fortran order (the first index varying fastest) when it is true. `B` is `e` times the
product of `shape` (1 for the shape `()`, an array of rank 0).

**The literal.** The header's text is read as this subset of Python's literals, the one NumPy
writes:

```
header  = ws "{" ws [entry {ws "," ws entry} [ws ","]] ws "}" ws
entry   = string ws ":" ws value
value   = string | integer | "True" | "False" | tuple | list
tuple   = "(" ws ")" | "(" ws value ws "," ws ")" | "(" ws value {ws "," ws value} [ws ","] ws ")"
list    = "[" ws [value {ws "," ws value} [ws ","]] ws "]"
integer = digit {digit} ["L"]
string  = "'" {char} "'" | '"' {char} '"'
ws      = {" " | "\t" | "\n" | "\r"}
```

An integer's `L` is a Python 2 long, as files that Python 2 wrote have them (`(2L,)`); the
integer is its digits. A `char` is any character but the quote that opens the string, a
backslash and a line break, or an escape: `\\`, `\'`, `\"`, `\n`, `\r`, `\t`, `\xHH`,
`\uHHHH` and `\UHHHHHHHH` (`H` a hexadecimal digit), the character they name. A tuple of one value needs its comma, as in
Python: `(3,)`.

**The members.** The dictionary has exactly the members `descr`, `fortran_order` and
`shape`, each once, in any order:

- `fortran_order` is `True` or `False`;
- `shape` is a tuple of integers, the array's size along each of its axes, at most 64 of them
  (NumPy's limit);
- `descr` is a **type string** ([§3](#3-data-types)), or a list of **fields**, a structured
  data type: each field a tuple `(name, descr)`, `name` a string and `descr` a type string or
  a list of fields.

## 3. Data types

A type string is a byte order, then a kind, then a size: `<` little-endian, `>` big-endian,
`|` not applicable. The Zarr data type, the element's size `e` and the codec's byte order:

| type string | Zarr data type | `e` | byte order |
|---|---|---|---|
| `|b1` | `bool` | 1 | none |
| `|i1`, `|u1` (`<` or `>` also) | `int8`, `uint8` | 1 | none |
| `Ei2`, `Ei4`, `Ei8`, `Eu2`, `Eu4`, `Eu8` | `int16` … `int64`, `uint16` … `uint64` | 2, 4, 8 | `E` |
| `Ef2`, `Ef4`, `Ef8` | `float16`, `float32`, `float64` | 2, 4, 8 | `E` |
| `Ec8`, `Ec16` | `complex64`, `complex128` | 8, 16 | `E` |
| `EM8[U]`, `EM8[kU]` | `{"name": "numpy.datetime64", "configuration": {"unit": U, "scale_factor": k}}` | 8 | `E` |
| `Em8[U]`, `Em8[kU]` | `{"name": "numpy.timedelta64", "configuration": {"unit": U, "scale_factor": k}}` | 8 | `E` |
| `EU<n>`, `n ≥ 1` | `{"name": "fixed_length_utf32", "configuration": {"length_bytes": 4n}}` | `4n` | `E` |
| `|S<n>`, `|V<n>`, `n ≥ 1` (`<` or `>` also; `a<n>`, an old name of `S<n>`, also) | `uint8`, with a trailing axis of `n` ([§4.3](#43-the-arrays)) | `n` | none |
| a list of fields | `{"name": "struct", "configuration": {"fields": F}}` | the fields' sizes, summed | the fields' (below) |

`E` is `<` or `>`. A datetime's or timedelta's unit `U` is one of `Y`, `M`, `W`, `D`, `h`,
`m`, `s`, `ms`, `us`, `ns`, `ps`, `fs` and `as`, and `k`, a positive integer, is 1 when it is
not written. A structured data type's `F` is its fields in order, each
`{"name": name, "data_type": D}`, `D` the field's Zarr data type: a field's type string is one
of the table's rows above `EU<n>` (numbers, booleans, datetimes and timedeltas), or a list of
fields of its own, a nested structure. A structure is presented only when its names are
unique and not empty, and its fields of more than one byte all have one byte order, the
codec's (none when every field is one byte).

Any other type string or list is **not presented**: an object array (`|O`, whose data is a
pickle), `f16` and `c32` (long doubles, which Zarr has no type for), a datetime or timedelta
without a unit, a size of 0, a type string with `=`, and a structure with a field of shape
(`(name, descr, shape)`), a padding field (an empty name), a string, bytes or void field, or two
byte orders. ([§7](#7-what-the-layout-does-not-express) lists what the layout does not
express.)

The fill value is the data type's zero: `false` for `bool`, `0` for a number (a float's
too), `[0, 0]` for a complex type, `0` for a datetime or timedelta, `""` for
`fixed_length_utf32`, and for a structure the object of its fields' fill values, by name. No
chunk of an array is missing, but those of an array of size 0.

## 4. The hierarchy

The hierarchy is a group at the root: the root `zarr.json` is
`{"zarr_format": 3, "node_type": "group", "attributes": A}`, where `A` has `zarr_conventions`
and `refkv_zarrified` ([§1](#1-declaration)) and nothing else.

### 4.1 An NPY file

A `.npy` file's array is the root's one child, at path `array`
([§4.3](#43-the-arrays)).

### 4.2 An NPZ archive

A `.npz` archive is a ZIP file ([§8.3](#83-the-archive)) whose **members** are its central
directory's entries, in its order. A member is **presented**, as an array at the path `K`,
its **name**, when:

- its file name ends with `.npy`, and `K` is the file name without it;
- `K` is a Zarr node name: it is not empty, has no `/`, is not `.` or `..`, does not start
  with `__`, and is not `zarr.json`; and it is not `refkv_source`;
- it is stored (method 0) or deflated (method 8), its preamble and header are those of
  [§2](#2-the-npy-format), its data type is one of [§3](#3-data-types)'s, and its data lies
  within its stored bytes; a deflated member's preamble, header and data are its **stream**,
  the bytes its deflate data inflates to, and its data ends the stream.

A presented member's array is what `np.load` gives for the key `K`: NumPy's `NpzFile` names
its arrays so. Every other member is **not presented**, with a reason
([§5.1](#51-the-root)): it is in the root's source metadata, and its bytes are in the mirror.

### 4.3 The arrays

An array's **stored shape** is `shape` reversed when `fortran_order` is true and the rank `r`
is at least 2, and `shape` otherwise: the shape whose C order is the data's order. A data
type with a trailing axis (`|S<n>` and `|V<n>`) adds `n` to the end of both: an element's
bytes are its last, fastest axis whatever the order.

<a id="type-npyarray"></a>**`NpyArray`**, an array's `zarr.json`: an
[`ArrayDocument`](../conventions.md#type-arraydocument) with these members:

| member | type | presence | value |
|---|---|---|---|
| `shape` | array of integer | required | `shape`, then `n` for a trailing axis |
| `data_type` | string or object | required | the Zarr data type of [§3](#3-data-types) |
| `chunk_grid` | object | required | regular; its chunk shape is the cut of the stored shape ([§8.4](#84-chunks)), reversed back (but for the trailing axis) when the stored shape is reversed; a deflated member's is its `shape` (each size at least 1): one chunk |
| `fill_value` | as the data type | required | the data type's zero ([§3](#3-data-types)) |
| `codecs` | array of object | required | `[{"name": "transpose", "configuration": {"order": O}}]` when the stored shape is reversed, `O` = `[r − 1, …, 1, 0]` (then `r` for a trailing axis); then `{"name": "bytes", "configuration": {"endian": E}}`, `E` the data type's byte order, `"little"` or `"big"`, or `{"name": "bytes"}` when it has none; then, for a deflated member, `{"name": "refkv.skip", "configuration": {"length": p + n}}` and `{"name": "gzip", "configuration": {"level": 6}}` ([§4.4](#44-the-refkvskip-codec)) |
| `dimension_names` | array of string | required | `dim_0`, `dim_1`, … `dim_{r−1}`, then `byte` for a trailing axis (xarray's names for a NumPy array's axes) |
| `attributes` | object | required | `{}` ([§1](#1-declaration)) |

A value of the array at index `i` is the element at index `i` of the NPY data, as NumPy reads
it; a value of a trailing axis is a byte of its element.

### 4.4 The `refkv.skip` codec

<a id="codec-refkv-skip"></a>`refkv.skip` is a bytes-to-bytes codec, configuration
`{"length": k}`, `k` a non-negative integer. Decoding drops the first `k` bytes of its input,
which MUST have at least `k`; encoding puts `k` zero bytes before its input. A deflated
member's chunk is its stream as a gzip member ([§8.4](#84-chunks)): `gzip` decodes it to the
stream, the NPY preamble and header and then the data, and `refkv.skip`, with `k` the `p + n`
bytes of the preamble and header, leaves the data, which `bytes` decodes. (No codec of
zarr-python, numcodecs or imagecodecs drops a prefix of more than a checksum's 4 bytes.)

## 5. Source metadata

The root's source metadata `S` ([conventions §2](../conventions.md#2-attributes)) is an
[`NpyRoot`](#type-npyroot), translated by
[conventions §6](../conventions.md#6-source-metadata-as-json). Everything the file holds is
on `refkv_source`, the file's **IR mirror** ([§5.2](#52-the-ir-mirror)), from which the file
is rebuilt byte for byte.

### 5.1 The root

<a id="type-npyroot"></a>**`NpyRoot`**, the root's `S`:

| member | type | presence | value |
|---|---|---|---|
| `kind` | string | required | `"npy"` or `"npz"` |
| `array` | [`NpyHeader`](#type-npyheader) | a `.npy` file's | its array's header |
| `members` | array of [`NpyMember`](#type-npymember) | a `.npz` archive's | its members, in central directory order |

<a id="type-npyheader"></a>**`NpyHeader`**, an NPY preamble and header:

| member | type | presence | value |
|---|---|---|---|
| `version` | array of integer | required | `[major, minor]` |
| `descr` | string or array | required | the header's `descr`: a type string as it is, or an array of fields, each `[name, descr]` (`[name, descr, shape]` for a field of shape, its shape an array of integers) |
| `fortran_order` | boolean | required | the header's `fortran_order` |
| `shape` | array of integer | required | the header's `shape` |
| `offset` | integer | required | the data's offset in the file; a deflated member's, in its stream (`p + n`) |

<a id="type-npymember"></a>**`NpyMember`**, a member of a `.npz` archive:

| member | type | presence | value |
|---|---|---|---|
| `name` | [`Text`](../conventions.md#type-text) | required | its file name |
| `method` | integer | required | its compression method (0 stored, 8 deflated) |
| `size` | integer | required | its size, uncompressed |
| `compressed_size` | integer | required | its size as stored |
| `path` | string | when presented | `K`, its array's path |
| `reason` | string | when not presented | why ([§4.2](#42-an-npz-archive)): `not_npy` (its file name does not end with `.npy`), `name` (`K` is not a node name), `reserved` (`K` is `refkv_source`), `method` (it is neither stored nor deflated), `header` (its preamble or header is not [§2](#2-the-npy-format)'s, or its deflate data ends or is invalid before its header does), `datatype` (its data type is not [§3](#3-data-types)'s), `size` (its data does not lie within its stored bytes, or does not end its stream) |
| `header` | [`NpyHeader`](#type-npyheader) | when it was read | its header: a stored or deflated `.npy` member whose header is [§2](#2-the-npy-format)'s, presented or not |

### 5.2 The IR mirror

`refkv_source` is the file's **IR mirror**
([conventions §11.3](../conventions.md#113-the-ir-mirror)). The IR's elements (the NumPy
**source model**) are below; nothing is left out: the preamble and header of every NPY
array the profile read in a range of the file, every member (a deflated member's preamble and
header are in its deflate data, and in the root's source metadata), the ZIP structures, and
every byte between or after them.

### 5.3 Elements

Paths are relative to the IR's root, whose path is `""`
([conventions §8.1](../conventions.md#81-elements)). Types are those of
[conventions §8.6](../conventions.md#86-types). In a `.npy` file the NPY elements are the
root's children, at offsets from 0; in a `.npz` archive, a stored member's, below its entry's
`data`, at its offsets in the file.

| path | kind | type | extent: what |
|---|---|---|---|
| `""` | struct | | the root: the whole file, `(0, size)` |
| `…/preamble` | value | `{magic:bytes[6],major:u1,minor:u1,header_length:<u2}`, or `<u4` for `header_length` | its preamble, `(o, p)` |
| `…/header` | value | `ascii[n]` | its header, `(o + p, n)`, when `n > 0` |
| `…/data` | data | | its data, `(o + p + n, B)`, when it is presented and `B > 0`; its form's geometry is `{"data_type": D, "shape": S, "order": "C" \| "F"}` (`D` the Zarr data type, `S` the header's shape), its codec `{"endian": "little" \| "big" \| null}`, and its recipe `[["src", 0, null]]` |
| `end`, `end_comment`, `zip64_locator`, `zip64_end`, `central_directory/**` | | | a `.npz` archive's end records and central directory, as [safe.md §6.3](safe.md#63-the-source-metadata-node) names the zip form's |
| `entries/<i>` | struct | | a `.npz` archive's central directory entry `i`: its local header and data, `[lho, ds + cs)` |
| `entries/<i>/local_header`, `…/name`, `…/extra` | value | | as [safe.md §6.3](safe.md#63-the-source-metadata-node) names them |
| `entries/<i>/data` | struct | | a stored member whose preamble and header were read, `(ds, cs)`: the NPY elements above |
| `entries/<i>/data` | derived | | a deflated member, `(ds, cs)`: form `{"transform":"deflate"}` |
| `entries/<i>/data` | value | `bytes[cs]` | any other member, when `cs > 0`, opaque |
| `gaps/<offset>` | gap | | bytes no other element claims: a stored member's bytes after its header when it is not presented, and after its data; data descriptors; and the bytes after a `.npy` file's data |

The view ([conventions §8.7](../conventions.md#87-the-view)) shows the values its rules
show: the preambles, the headers of at most 1024 bytes, and the ZIP structures and names; not
the data, nor the opaque values. A parser reads them all, a member's local file name
included.

### 5.4 Equivalence

Hierarchies of one file are compared entry for entry, `refkv_source` included
([conventions §11.4](../conventions.md#114-equivalence)).

## 6. Example

A `.npy` file at `https://example.org/a.npy` (the fixture `fixtures/npy/npy_c_float64.npy`)
holds a `3 × 4` float64 array, little-endian, in C order: a 128-byte preamble and header,
then 96 bytes of data. Its root `zarr.json` has these attributes:

```json
{
  "zarr_conventions": [{
    "uuid": "62b1d1ba-8eb3-465c-a333-75e096fce519",
    "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/zarrify/npy/schema.json",
    "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md",
    "name": "refkv_zarrified",
    "description": "The Zarr layout of a NumPy source zarrified by refkv, and the source's metadata"
  }],
  "refkv_zarrified": {
    "profile": "npy",
    "version": 0,
    "source": {"url": "https://example.org/a.npy"},
    "npy": {
      "kind": "npy",
      "array": {"version": [1, 0], "descr": "<f8", "fortran_order": false, "shape": [3, 4], "offset": 128}
    }
  }
}
```

Its array, `array/zarr.json`:

```json
{
  "zarr_format": 3,
  "node_type": "array",
  "shape": [3, 4],
  "data_type": "float64",
  "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [3, 4]}},
  "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
  "fill_value": 0,
  "codecs": [{"name": "bytes", "configuration": {"endian": "little"}}],
  "dimension_names": ["dim_0", "dim_1"],
  "attributes": {}
}
```

Its one chunk, `array/c/0/0`, is the range `(128, 96)` of source 0. Written in Fortran order
(`npy_f_float64.npy`), the same array has the codecs
`[{"name": "transpose", "configuration": {"order": [1, 0]}}, {"name": "bytes", ...}]`: its
chunk's bytes are the array's transpose in C order, which is the file's order.

A `.npz` archive of `np.savez(f, image=a, labels=b)` has the arrays `image` and `labels`.
`np.savez_compressed` deflates its members: then each array is one chunk, the member's deflate
data between a gzip header and trailer, and its codecs end with `refkv.skip` and `gzip`
([§4.4](#44-the-refkvskip-codec)). The member `fortran.npy` of `fixtures/npy/npz_compressed.npz`,
a `3 × 4 × 5` big-endian `uint16` array in Fortran order, has the codecs

```json
[
  {"name": "transpose", "configuration": {"order": [2, 1, 0]}},
  {"name": "bytes", "configuration": {"endian": "big"}},
  {"name": "refkv.skip", "configuration": {"length": 128}},
  {"name": "gzip", "configuration": {"level": 6}}
]
```

and its one chunk, `fortran/c/0/0/0`, is the literal `1F 8B 08 00 00 00 00 00 00 FF`, the
member's 189 bytes of deflate data, and the member's CRC-32 and size (248) as two `u32`s.

## 7. What the layout does not express

This section is informative.

- **Compressed archives.** `np.savez_compressed` deflates each member, its NPY header and its
  data together, so no range of the file holds the array's elements: a deflate stream can only
  be read from its start. Such a member is one chunk, however large, and reading any of its
  elements inflates all of it: a 1 GiB member is a 1 GiB chunk, which a reader holds in memory
  whole, and fetches whole over HTTP. A viewer that reads an array in tiles (Neuroglancer)
  reads it at most once at a time, but cannot show a part of it sooner. Members compressed
  with another method (bzip2, LZMA) are not presented (`method`).
- **Object arrays** hold Python objects as a pickle, which is code, not data: an object array
  is not presented, and `np.load` reads one only with `allow_pickle=True`.
- **Long doubles** (`f16`, `c32`, and `f12` on some platforms) have no Zarr data type.
- **Structures** with padding, fields of shape, or string fields have no `struct` of the
  zarr-extensions that holds them field for field; the record's bytes are in the mirror.
- **Fixed-length bytes and voids** (`|S<n>`, `|V<n>`) are `uint8` with a trailing axis, as the
  HDF5 convention's fixed-length strings are ([hdf5.md §3.1](hdf5.md#31-data-types)): a reader
  that wants NumPy's `S<n>` views the last axis as one element.
- **Field titles and the header's other text.** The header's spacing, its member order, and
  anything a later NumPy writes beyond the three members are in the mirror (`header`), not in
  the layout.

# Part 2. The profile

The NumPy profile of [spec/zarrify.md](../zarrify.md) is [§8](#8-numpy-profile) of this
document. What every profile's Part 2 shares is in
[spec/zarrify.md, Profiles](../zarrify.md#profiles).

## 8. NumPy profile

### 8.1 Signature

The input is a `.npy` file or a `.npz` archive, which its **signature**
([spec/zarrify.md §1.2](../zarrify.md#12-input), tested at the precedence of
[profiles/npy.json](profiles/npy.json), before the SAFE profile's) chose: bytes 0–5 of `H` are
`93 4E 55 4D 50 59` (a `.npy` file); or bytes 0–3 are `50 4B 03 04`, a ZIP local file header,
whose file name (its length the `u16` at byte 26, the name at byte 30, within `H`) ends with
`.npy` (a `.npz` archive). A ZIP file whose first member is not a `.npy` member does not
match.

### 8.2 An NPY file

The preamble is the file's first bytes, and the header the `n` after it
([the convention §2](#2-the-npy-format)). The input is rejected unless all of these hold,
checked in this order:

- **Preamble:** the file has at least 10 bytes, and `major` is 1, 2 or 3 and `minor` 0, and,
  for versions 2 and 3, at least 12 bytes.
- **Header:** `p + n` is at most the file's size.
- **Text:** a version 3 header is UTF-8.
- **Literal:** the header is a literal of [the convention §2](#2-the-npy-format), a
  dictionary of exactly the members `descr`, `fortran_order` and `shape`, each of its type.
- **Data type:** `descr` is presented ([the convention §3](#3-data-types)).
- **Data size:** `B` computed exactly is at most 2^53 − 1, and `p + n + B` is at most the
  file's size.

Bytes after the data are allowed (a gap).

### 8.3 The archive

The archive is read as [safe.md §8.4](safe.md#84-the-zip-form) reads the zip form, for its
**end of central directory**, its **central directory** and its **local headers**, with the
same checks: the end records, ZIP64, the central directory's entries (disk 0, not encrypted,
a UTF-8 name), each local header's signature, and the members' ranges `[lho, ds + cs)` lying
before the central directory and not overlapping. Every member's local header is read,
whatever its size. Besides:

- **Names:** no two members have the same file name.
- **No product:** the members are not a SAFE product: none of [safe.md §8.4](safe.md#84-the-zip-form)'s rules on names,
  directories and methods apply.
- **Stored members:** a stored member (method 0) MUST have `cs = us`.
- **Members:** a stored or deflated member whose file name ends with `.npy` has its preamble
  and header read from its data, at `ds` (a deflated member's from its stream, which is
  inflated as far as its header ends), by
  [§8.2](#82-an-npy-file)'s checks, each of which makes the member not presented
  ([the convention §4.2](#42-an-npz-archive)) instead of rejecting the input: the preamble and
  header (`header`), the data type (`datatype`), and the data size, `p + n + B` at most `cs`
  (`size`); a deflated member's deflate data that ends, or is invalid, before its header does,
  or a header that ends past byte 2^20 of its stream, `p + n > 2^20` (`header`: the profile
  inflates no more of a member than that to read its header); and a deflated member's data
  that does not end its stream, `p + n + B = us` (`size`). A member that is neither stored nor deflated is not read (`method`).
- **Arrays:** at least one member is presented; otherwise the input is rejected, with each
  member's reason: `no member can be presented: a.npy (method), b.txt (not_npy)`.

### 8.4 Chunks

A stored array's data is `B` contiguous bytes at offset `v`, its elements in the order of its
stored shape ([the convention §4.3](#43-the-arrays)), C order. Every chunk of the array is
present. The data is cut as [conventions §7](../conventions.md#7-source-values-as-arrays)
cuts contiguous values of the stored shape (the trailing axis included, with the element its
byte), with the **chunk limit 2^20 bytes** (1 MiB); the chunk shape along each axis is the
cut's, put in the array's axis order. An array of rank 0 is one chunk, `<name>/c`, the range
`(0, v, e)`; an array with a size of 0 has none.

<a id="type-npychunk"></a>**`NpyChunk`**, the chunk that starts `o` bytes into the data and
holds `l` of its bytes, padded with `p` zero bytes
([conventions §7](../conventions.md#7-source-values-as-arrays); `p` is 0 but for an edge chunk
along the cut axis):

| part | range | presence | value |
|---|---|---|---|
| the values | source range `(0, v + o, l)` | required | the chunk's bytes in the file |
| the padding | source range `(1, 0, p)` | optional | when `p > 0`: the first `p` bytes of the output's **zero data source** ([spec/zarrify.md §1.2](../zarrify.md#12-input)), which holds as many zero bytes as the most padding any reference of the output holds |

Where [conventions §7](../conventions.md#7-source-values-as-arrays) copies the chunk (its
reference, with the padding as a literal range, would not fit in a payload), it is instead
`bytes`: the `l` bytes and the `p` zero bytes. The output's source table is source 0,
followed by the zero data source when a reference is padded.

A deflated member's array, of `B > 0` bytes, is one chunk, `<name>/c/0/…/0` (one `0` for each
of its axes, a trailing axis included; `<name>/c` for rank 0): its deflate data, the `cs`
bytes at `ds`, as a gzip member (RFC 1952), which inflates to its stream. Its reference is
never copied: its literals are 18 bytes. With `B = 0` it has none.

<a id="type-npydeflatedchunk"></a>**`NpyDeflatedChunk`**:

| part | range | presence | value |
|---|---|---|---|
| the gzip header | literal | required | `1F 8B 08 00 00 00 00 00 00 FF`: deflate, no flags, no time, no extra flags, an unknown operating system |
| the deflate data | source range `(0, ds, cs)` | required | the member's data in the file |
| the gzip trailer | literal | required | the member's CRC-32 (the central directory's), then `us` modulo 2^32, each a `u32` |

The archive has no other entries than the root's and the arrays' `zarr.json`, the arrays'
chunks, and the mirror's.

### 8.5 Summary

This section is informative. Both implementations print a one-line JSON summary, with these
members:

| member | value |
|---|---|
| `kind` | `"npy"` or `"npz"` |
| `arrays` | each presented array's `{"path", "dataType", "shape", "order", "chunkShape", "chunks"}`, `order` `"C"` or `"F"`, in the archive's central directory order |
| `unpresented` | the members not presented, a count by reason (`{}` for a `.npy` file) |
