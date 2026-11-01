# safetensors

The Zarr layout of a safetensors file: the named tensors of a machine learning model's weights,
as Hugging Face's `safetensors` library writes them (`model.safetensors`). What all of refkv's
conventions share is in [spec/conventions.md](../conventions.md), cited here as "conventions
§n". How refkv produces this layout as a reference store, and which files it accepts, is the
safetensors profile, [Part 2](#part-2-the-profile) below. The format is the one the library's
README defines ([huggingface/safetensors](https://github.com/huggingface/safetensors#format)),
cited here as "the format"; where the README is silent, the library's reader
(`safetensors/src/tensor.rs`, version 0.7) decides.

Convention version: 0 (until release, [conventions §1](../conventions.md#1-conventions)) · UUID: `15e7ce39-d4ca-4514-8144-6e19d2a0341b` ·
Schema: [schema.json](safetensors/schema.json) · Added 2026-11-01.

This document has two parts ([conventions §11.1](../conventions.md#111-parts)).

Like NumPy's, a safetensors file's tensors are not images: the hierarchy holds each tensor as a
plain Zarr array, with no OME-NGFF metadata. The values stay in the file: each chunk is a range
of it.

# Part 1. The convention

## Layout

The hierarchy's entries ([conventions §11.5](../conventions.md#115-layout-tables)):

| prefix | type | structure | description |
|---|---|---|---|
| `zarr.json` | group | [`GroupDocument`](../conventions.md#type-groupdocument), its attributes a [`Declaration`](../conventions.md#type-declaration) and nothing else | the root ([§4](#4-the-hierarchy)) |
| `zarr.json#/attributes/zarr_conventions` | attribute | array of [`ConventionMetadata`](../conventions.md#type-conventionmetadata) | the CMO of [§1](#1-declaration) |
| `zarr.json#/attributes/refkv_zarrified` | attribute | [`RootProperty`](../conventions.md#type-rootproperty) | `profile` `"safetensors"`, `version` `0`, `source.url` the file's URL |
| `zarr.json#/attributes/refkv_zarrified/safetensors` | attribute | [`SafetensorsRoot`](#type-safetensorsroot) | the root's source metadata ([§5.1](#51-the-root)) |
| `<group>/zarr.json` | group | [`GroupDocument`](../conventions.md#type-groupdocument), attributes `{}` | a group that holds tensors whose names have a `/` ([§4.1](#41-paths)) |
| `<path>/zarr.json` | array | [`SafetensorsArray`](#type-safetensorsarray) | a presented tensor ([§4.2](#42-the-arrays)) |
| `<path>/c/<coords>`, `<path>/c` | chunk | [`SafetensorsChunk`](#type-safetensorschunk) | every chunk of the tensor ([§8.3](#83-chunks)); `<path>/c` for a tensor of rank 0 |
| `refkv_source/**` | subtree | [the mirror's entries](../conventions.md#table-mirror) | the IR mirror ([§5.2](#52-the-ir-mirror)): its elements are [§5.3](#53-elements)'s |

| placeholder | value |
|---|---|
| `<path>` | a presented tensor's path, its name ([§4.1](#41-paths)) |
| `<group>` | a proper prefix of a presented tensor's path that ends before one of its `/` |
| `<coords>` | the chunk's coordinates along the tensor's axes, in decimal, one key segment each |

## 1. Declaration

The root and `refkv_source` declare the convention
([conventions §11.2](../conventions.md#112-declaration)), the root with its source
metadata of [§5.1](#51-the-root) as the member `"safetensors"`. Its CMO is:

```json
{
  "uuid": "15e7ce39-d4ca-4514-8144-6e19d2a0341b",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/zarrify/safetensors/schema.json",
  "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md",
  "name": "refkv_zarrified",
  "description": "The Zarr layout of a safetensors source zarrified by refkv, and the source's metadata"
}
```

The root's attributes are `zarr_conventions` and `refkv_zarrified` and nothing else: there is
no `ome` member, since the hierarchy is not an image. The arrays and the other groups have no
attributes.

## 2. The safetensors format

A safetensors file is a **length**, then a **header**, then the **buffer**:

| bytes | field | type | value |
|---|---|---|---|
| 0–7 | `length` | `u64`, little-endian | `N`, the header's length in bytes |
| 8 to `8 + N − 1` | `header` | UTF-8 text | a JSON object, which starts with `{`; spaces (0x20) may pad it at its end |
| `8 + N` to the end | the buffer | bytes | the tensors' data, one after another, with no gap |

The library writes `N` as a multiple of 8, padding the header with spaces; real files that
other writers made have other lengths (the survey of [§7](#7-what-the-layout-does-not-express)
found `N` of 14283 and 10830).

The header's members are the **tensors**, each a tensor's **name** and its **entry**, and
optionally the member `__metadata__`, an object of string values (the file's free-form metadata,
often `{"format": "pt"}`). Their order is the writer's: the library writes `__metadata__` first
and the tensors by data type and name, so a tensor's place in the header is not its data's place
in the buffer. A tensor's entry is an object with these members:

| member | type | value |
|---|---|---|
| `dtype` | string | the tensor's **data type** ([§3](#3-data-types)) |
| `shape` | array of integer | the tensor's size along each of its axes, `[]` for a scalar (rank 0) |
| `data_offsets` | array of two integers | `[begin, end]`: its data is the bytes `[begin, end)` of the buffer, `8 + N + begin` to `8 + N + end` of the file |

An entry's other members, which the library ignores, are kept in the source metadata
([§5.1](#51-the-root)). A tensor's elements are in C order (the last index varying fastest), each
in little-endian byte order. Its data is `B = ⌈e · k / 8⌉` bytes, `e` the product of its shape
(1 for `[]`) and `k` its data type's bits; `e · k` is a multiple of 8. Sorted by `data_offsets`,
the tensors' data covers the buffer from its start to the end of the file, with no gap and no
overlap ([§8.2](#82-checks)).

## 3. Data types

A tensor's data type `dtype`, its bits `k`, and its Zarr data type and codecs
([§4.2](#42-the-arrays)):

| `dtype` | `k` | Zarr data type | from | `ml_dtypes` (Python) |
|---|---|---|---|---|
| `BOOL` | 8 | `bool` | Zarr v3 core | (NumPy's `bool`) |
| `U8`, `I8` | 8 | `uint8`, `int8` | core | |
| `U16`, `I16`, `U32`, `I32`, `U64`, `I64` | 16, 32, 64 | `uint16` … `int64` | core | |
| `F16`, `F32`, `F64` | 16, 32, 64 | `float16`, `float32`, `float64` | core | |
| `C64` | 64 | `complex64` | core | |
| `BF16` | 16 | `bfloat16` | [zarr-extensions](https://github.com/zarr-developers/zarr-extensions/tree/main/data-types/bfloat16) | `bfloat16` |
| `F8_E5M2` | 8 | `float8_e5m2` | [zarr-extensions](https://github.com/zarr-developers/zarr-extensions/tree/main/data-types/float8_e5m2) | `float8_e5m2` |
| `F8_E4M3` | 8 | [`refkv.float8_e4m3fn`](#31-the-refkvfloat8_e4m3fn-data-type) | this document | `float8_e4m3fn` |
| `F8_E4M3FNUZ` | 8 | `float8_e4m3fnuz` | [zarr-extensions](https://github.com/zarr-developers/zarr-extensions/tree/main/data-types/float8_e4m3fnuz) | `float8_e4m3fnuz` |
| `F8_E5M2FNUZ` | 8 | `float8_e5m2fnuz` | [zarr-extensions](https://github.com/zarr-developers/zarr-extensions/tree/main/data-types/float8_e5m2fnuz) | `float8_e5m2fnuz` |
| `F8_E8M0` | 8 | `float8_e8m0fnu` | [zarr-extensions](https://github.com/zarr-developers/zarr-extensions/tree/main/data-types/float8_e8m0fnu) | `float8_e8m0fnu` |
| `F4` | 4 | `float4_e2m1fn`, packed ([§3.2](#32-packed-data-types)) | [zarr-extensions](https://github.com/zarr-developers/zarr-extensions/tree/main/data-types/float4_e2m1fn) | `float4_e2m1fn` |
| `F6_E2M3` | 6 | `float6_e2m3fn`, packed | [zarr-extensions](https://github.com/zarr-developers/zarr-extensions/tree/main/data-types/float6_e2m3fn) | `float6_e2m3fn` |
| `F6_E3M2` | 6 | `float6_e3m2fn`, packed | [zarr-extensions](https://github.com/zarr-developers/zarr-extensions/tree/main/data-types/float6_e3m2fn) | `float6_e3m2fn` |

These are the library's data types (its `Dtype`), all of them. A Zarr data type of
zarr-extensions is used only where its definition is the library's type bit for bit, as the
library reads it into PyTorch (`safetensors/torch.py`): `F8_E5M2` is `torch.float8_e5m2`, which
has infinities, as zarr-extensions' `float8_e5m2` has. `F8_E4M3` is `torch.float8_e4m3fn`, which
has none: the bytes `0x78` to `0x7E` are the finite numbers 256 to 448, where zarr-extensions'
`float8_e4m3` reads infinity and NaNs. It is refkv's own data type,
[§3.1](#31-the-refkvfloat8_e4m3fn-data-type).

The fill value is the data type's zero: `false` for `bool`, `0` for a number (a float's too),
`[0, 0]` for `complex64`; `float8_e8m0fnu`, a power of two with no zero, has `"NaN"`. No chunk of
a tensor is missing, but those of a tensor of size 0.

### 3.1 The `refkv.float8_e4m3fn` data type

<a id="datatype-refkv-float8_e4m3fn"></a>`refkv.float8_e4m3fn` is an 8-bit floating-point data
type, the E4M3 format of the OCP 8-bit floating point specification (OFP8, 2023), which
`ml_dtypes` and PyTorch call `float8_e4m3fn` ("finite"):

- **Name:** `"refkv.float8_e4m3fn"`, with no configuration.
- **Bits:** `0bSEEEEMMM`: a sign bit, 4 exponent bits with a bias of 7, and 3 mantissa bits.
  A biased exponent of 0 is a subnormal number, `(−1)^S · 2^−6 · M / 8`; otherwise the number is
  `(−1)^S · 2^(E − 7) · (1 + M / 8)`. There are no infinities: `0x7F` and `0xFF`
  (`S.1111.111`) are NaN, and the largest finite number is 448 (`0x7E`).
- **Fill value:** as Zarr v3 core's floating-point types: a JSON number, which MUST be exactly
  representable; `"NaN"`, the bits `0x7F`; or a hexadecimal string `"0xNN"`, the bits. The
  strings `"Infinity"` and `"-Infinity"` are not allowed, since there is no such value.
- **Codecs:** with `bytes`, each element is one byte and `endian` has no effect; with
  `packbits`, it is 8 bits.
- **Readers:** a Python reader maps it to `ml_dtypes.float8_e4m3fn`, as refkv's zarr extra does
  (`refkv.zarr.dtypes`). TensorStore has the same type under the name `float8_e4m3fn`, which
  zarr-extensions does not define; zarr-extensions' `float8_e4m3` is the IEEE-style E4M3, with
  infinities, a different type.

### 3.2 Packed data types

`F4`, `F6_E2M3` and `F6_E3M2` have `k` of 4 and 6 bits: the buffer holds a tensor's elements as
one sequence of bits, element `i` its bits `[i · k, (i + 1) · k)`, and bit `j` of the sequence is
bit `j mod 8` (from the least significant) of the data's byte `⌊j / 8⌋`. For `F4` this is
PyTorch's `float4_e2m1fn_x2`, two elements a byte with the first in bits 3 to 0, which the
library writes (its header's shape counts elements, not bytes). It is the order of
zarr-extensions' [`packbits`](https://github.com/zarr-developers/zarr-extensions/tree/main/codecs/packbits)
codec with `padding_encoding` `"none"`, which decodes such a tensor's chunks
([§4.2](#42-the-arrays)). No writer of `F6_E2M3` or `F6_E3M2` tensors was found; the library
defines their bits, and this order is the one it gives `F4`.

## 4. The hierarchy

The hierarchy is a group at the root: the root `zarr.json` is
`{"zarr_format": 3, "node_type": "group", "attributes": A}`, where `A` has `zarr_conventions`
and `refkv_zarrified` ([§1](#1-declaration)) and nothing else. Each presented tensor is an array
at its path.

### 4.1 Paths

A tensor's **path** is its name, its `/` separating Zarr node names: `model.layers.0.weight`
is the root's child of that name, and `encoder/conv/kernel` the array `kernel` of the group
`encoder/conv`. A tensor is **presented** unless:

- its name is not a path of node names: a part between two `/` (or before the first, or after
  the last) is empty, `.` or `..`, starts with `__`, or is `zarr.json` (reason `name`);
- its path's first node name is `refkv_source` (reason `reserved`);
- its path is a proper prefix, ending before a `/`, of another tensor's name, which makes it a
  group (reason `path`).

Every proper prefix of a presented tensor's path that ends before a `/` is a group, with the
attributes `{}`. A tensor that is not presented is in the root's source metadata, with its reason
([§5.1](#51-the-root)), and its data is in the mirror.

### 4.2 The arrays

<a id="type-safetensorsarray"></a>**`SafetensorsArray`**, a presented tensor's `zarr.json`: an
[`ArrayDocument`](../conventions.md#type-arraydocument) with these members:

| member | type | presence | value |
|---|---|---|---|
| `shape` | array of integer | required | the entry's `shape` |
| `data_type` | string | required | the Zarr data type of [§3](#3-data-types) |
| `chunk_grid` | object | required | regular, its chunk shape the cut of [§8.3](#83-chunks) |
| `fill_value` | as the data type | required | the data type's zero ([§3](#3-data-types)) |
| `codecs` | array of object | required | `[{"name": "bytes", "configuration": {"endian": "little"}}]` for a data type of more than 8 bits; `[{"name": "bytes"}]` for one of 8; `[{"name": "packbits", "configuration": {"padding_encoding": "none"}}]` for a packed one ([§3.2](#32-packed-data-types)) |
| `dimension_names` | array of string | required | `dim_0`, `dim_1`, … `dim_{r−1}`, `r` the tensor's rank (NumPy's and xarray's names for unnamed axes) |
| `attributes` | object | required | `{}` ([§1](#1-declaration)) |

A value of the array at index `i` is the tensor's element at index `i`, as the library reads it.

## 5. Source metadata

The root's source metadata `S` ([conventions §2](../conventions.md#2-attributes)) is a
[`SafetensorsRoot`](#type-safetensorsroot), translated by
[conventions §6](../conventions.md#6-source-metadata-as-json). Everything the file holds is
on `refkv_source`, the file's **IR mirror** ([§5.2](#52-the-ir-mirror)), from which the file
is rebuilt byte for byte.

### 5.1 The root

<a id="type-safetensorsroot"></a>**`SafetensorsRoot`**, the root's `S`:

| member | type | presence | value |
|---|---|---|---|
| `header_length` | integer | required | `N` |
| `metadata` | object of string | when the header has `__metadata__` | its members, in the header's order |
| `tensors` | array of [`SafetensorsTensor`](#type-safetensorstensor) | required | the header's tensors, in its order |

<a id="type-safetensorstensor"></a>**`SafetensorsTensor`**, a tensor:

| member | type | presence | value |
|---|---|---|---|
| `name` | string | required | its name |
| `dtype` | string | required | its entry's `dtype` |
| `shape` | array of integer | required | its entry's `shape` |
| `data_offsets` | array of integer | required | its entry's `data_offsets` |
| `extra` | object | when its entry has other members | those members, in its order, as written |
| `path` | string | when presented | its array's path ([§4.1](#41-paths)) |
| `reason` | string | when not presented | why ([§4.1](#41-paths)): `name`, `reserved` or `path` |

### 5.2 The IR mirror

`refkv_source` is the file's **IR mirror**
([conventions §11.3](../conventions.md#113-the-ir-mirror)). The IR's elements (the
safetensors **source model**) are below; nothing is left out: the length, the header with its
padding, and every tensor's data.

### 5.3 Elements

Paths are relative to the IR's root, whose path is `""`
([conventions §8.1](../conventions.md#81-elements)). Types are those of
[conventions §8.6](../conventions.md#86-types).

| path | kind | type | extent: what |
|---|---|---|---|
| `""` | struct | | the root: the whole file, `(0, size)` |
| `length` | value | `<u8` | `N`, `(0, 8)` |
| `header` | value | `ascii[N]` | the header, `(8, N)`, when `N > 0` |
| `tensors/<i>` | data | | the data of the header's tensor `i` (from 0, in the header's order), `(8 + N + begin, B)`, when `B > 0`; its form's geometry is `{"data_type": D, "shape": S, "bits": k}` (`D` the Zarr data type, `S` the shape), its codec `{"endian": "little" \| null}` (`null` when `k ≤ 8`), and its recipe `[["src", 0, null]]` |

The view ([conventions §8.7](../conventions.md#87-the-view)) shows the values its rules
show: the length, and the header when it is at most 1024 bytes; not the data. A parser reads
them all.

### 5.4 Equivalence

Hierarchies of one file are compared entry for entry, `refkv_source` included
([conventions §11.4](../conventions.md#114-equivalence)).

## 6. Example

A safetensors file at `https://example.org/model.safetensors` (the fixture
`fixtures/safetensors/st_two.safetensors`) holds a `2 × 3` float32 tensor `weight` and a
`3`-element bfloat16 tensor `bias`, with the metadata `{"format": "pt"}`: an 8-byte length, a
152-byte header (the library's, padded to a multiple of 8), then 24 bytes of `weight` and 6 of `bias`. Its root `zarr.json` has these
attributes:

```json
{
  "zarr_conventions": [{
    "uuid": "15e7ce39-d4ca-4514-8144-6e19d2a0341b",
    "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/zarrify/safetensors/schema.json",
    "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md",
    "name": "refkv_zarrified",
    "description": "The Zarr layout of a safetensors source zarrified by refkv, and the source's metadata"
  }],
  "refkv_zarrified": {
    "profile": "safetensors",
    "version": 0,
    "source": {"url": "https://example.org/model.safetensors"},
    "safetensors": {
      "header_length": 152,
      "metadata": {"format": "pt"},
      "tensors": [
        {"name": "weight", "dtype": "F32", "shape": [2, 3], "data_offsets": [0, 24], "path": "weight"},
        {"name": "bias", "dtype": "BF16", "shape": [3], "data_offsets": [24, 30], "path": "bias"}
      ]
    }
  }
}
```

The array `bias/zarr.json`:

```json
{
  "zarr_format": 3,
  "node_type": "array",
  "shape": [3],
  "data_type": "bfloat16",
  "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [3]}},
  "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
  "fill_value": 0,
  "codecs": [{"name": "bytes", "configuration": {"endian": "little"}}],
  "dimension_names": ["dim_0"],
  "attributes": {}
}
```

Its one chunk, `bias/c/0`, is the range `(184, 6)` of source 0: `8 + 152 + 24`.

## 7. What the layout does not express

This section is informative.

- **Sharded checkpoints.** A large model is several files, `model-00001-of-00004.safetensors`
  and so on, with a `model.safetensors.index.json` that maps each tensor's name to its file. This
  profile zarrifies one file; the index, and the hierarchy of a whole model, are left to a later
  profile of the index as a store input, or to a combine step.
- **Readers of the data types.** zarr-python 3.4 has no `bfloat16`, `float8_*`, `float4_*` or
  `float6_*` data types, and no `packbits` codec: refkv's zarr extra registers them, through
  `ml_dtypes` (`refkv.zarr.dtypes`, `refkv.zarr.codecs.packbits`). TensorStore 0.1.86 reads
  `bfloat16`, the `float8_*` types of zarr-extensions and `float4_e2m1fn` with the `bytes` codec,
  but not `refkv.float8_e4m3fn` (it names that type `float8_e4m3fn`), the `float6_*` types, or
  `packbits`. A tensor's data type is also in the root's source metadata, as the file names it.
- **The header's text.** Its spacing, its padding, its members' order and the place of
  `__metadata__` are in the mirror (`header`), not in the layout.
- **Model structure.** The names' dots (`model.layers.0.mlp.up_proj.weight`) are a framework's
  module path; the layout keeps each name whole, as one node, rather than guess a hierarchy from
  them.
- **Survey.** The design was checked against the headers of ten public files on the Hugging Face
  Hub (2026-11-01, over range reads): GPT-2, Qwen2.5-0.5B (`BF16`), a FP8 checkpoint of it
  (`BF16` and `F8_E4M3`), BERT, all-MiniLM-L6-v2, DINOv2, Whisper, a VAE and a timm ResNet. Their
  headers start with `{"`, two have an `N` that is not a multiple of 8, one lists its tensors in no
  order of their data, one has scalars (shape `[]`), and none names a tensor with a `/`.

# Part 2. The profile

The safetensors profile of [spec/zarrify.md](../zarrify.md) is [§8](#8-safetensors-profile) of
this document. What every profile's Part 2 shares is in
[spec/zarrify.md, Profiles](../zarrify.md#profiles).

## 8. safetensors profile

### 8.1 Signature

The input is a safetensors file, which its **signature**
([spec/zarrify.md §1.2](../zarrify.md#12-input), tested at the precedence of
[profiles/safetensors.json](profiles/safetensors.json), before the TIFF profile's) chose: `H` has
at least 10 bytes; the `u64` at byte 0, little-endian, is at least 2 and at most 100000000; byte 8
is `{` (0x7B); and byte 9 is `"`, `}`, or a space, tab, line feed or carriage return. (Its
precedence is before TIFF's because a header of 2771273 bytes starts with `II*\0`, TIFF's
signature; a TIFF file, whose first IFD is not at offset 0, never has byte 8 `{` with bytes 4–7
zero.)

The profile reads the file's first `min(size, 65536)` bytes, which hold the length and, in
every file of the survey, the header; then the rest of a longer header. It reads no tensor's
data, but for the edge chunks it copies ([§8.3](#83-chunks)).

### 8.2 Checks

The input is rejected unless all of these hold, checked in this order:

- **Length:** the file has at least 8 bytes.
- **Limit:** `N` is at most 100000000, the library's limit (`MAX_HEADER_SIZE`).
- **Header:** `8 + N` is at most the file's size.
- **Text:** the header is UTF-8.
- **Start:** the header starts with `{`.
- **JSON:** the header is JSON (RFC 8259, with its trailing spaces), whose objects have no member
  twice ("Duplicate keys are disallowed", the format), and it is an object.
- **Metadata:** `__metadata__`, when present, is an object whose values are strings.
- **Entries:** every other member's value is an object with the members `dtype`, a string;
  `shape`, an array of integers from 0 to 2^53 − 1; and `data_offsets`, an array of two such
  integers.
- **Data types:** each tensor's `dtype` is one of [§3](#3-data-types)'s.
- **Offsets:** taken in the order of their `data_offsets`, `[begin, end]` (and of the header,
  for equal ones), each tensor's `end` is at least its `begin`, and its `begin` is the previous
  tensor's `end` (0 for the first): no overlap, and no gap.
- **Size:** in the same order, each tensor's `e · k` is a multiple of 8, and `end − begin` is
  `e · k / 8`, computed exactly.
- **Buffer:** the last tensor's `end` (0 for none) is the buffer's size, `size − 8 − N`.

The library's reader checks the same, but for three rules its README states and its reader does
not test: **Start** (it reads a header that starts with spaces, which the signature never
chooses); a member written twice, of which it keeps the last; and the limit of 2^53 − 1 on
integers, where it takes any 64-bit one (no file can hold a buffer that large). A file with no
tensors is accepted: its hierarchy is the root alone.

### 8.3 Chunks

A presented tensor's data is `B` contiguous bytes at offset `v = 8 + N + begin`, its elements in
C order. Every chunk of the tensor is present. A tensor of `k ≥ 8` bits is cut as
[conventions §7](../conventions.md#7-source-values-as-arrays) cuts contiguous values of its
shape, with elements of `k / 8` bytes and the **chunk limit 2^20 bytes** (1 MiB). A tensor of
rank 0 is one chunk, `<path>/c`, the range `(0, v, k / 8)`; a tensor of size 0 has none.

A packed tensor ([§3.2](#32-packed-data-types)) is cut as contiguous values of its shape with
elements of one byte, the same limit: each chunk is a run of the elements `[s, s + m)` of the
tensor, of a chunk shape of `C` elements. When `s · k` is a multiple of 8 for every chunk, the
chunk is the bytes `[v + s · k / 8, v + (s + m) · k / 8)`, padded with zero bytes to
`⌈C · k / 8⌉`. Otherwise the tensor is one chunk, its whole data, of the chunk shape `shape` (each
size at least 1).

<a id="type-safetensorschunk"></a>**`SafetensorsChunk`**, the chunk that starts `o` bytes into
the data and holds `l` of its bytes, padded with `p` zero bytes
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

The archive has no other entries than the groups' and the arrays' `zarr.json`, the arrays'
chunks, and the mirror's.

### 8.4 Summary

This section is informative. Both implementations print a one-line JSON summary, with these
members:

| member | value |
|---|---|
| `tensors` | each presented tensor's `{"path", "dtype", "dataType", "shape", "chunkShape", "chunks"}`, in the header's order |
| `unpresented` | the tensors not presented, a count by reason |
