# ENVI

The Zarr layout of an ENVI raster (an "ENVI Standard" file: a flat binary data file and the text
header file beside it, `.hdr`, that says how its values are laid out), and the translation of its
header into JSON. What all of refkv's conventions share is in
[spec/conventions.md](../conventions.md), cited here as "conventions §n". How refkv produces this
layout as a reference store, which files it reads and which inputs it rejects, is the ENVI profile,
[Part 2](#part-2-the-profile) below. The format is NV5 Geospatial's
[ENVI Header Files](https://www.nv5geospatialsoftware.com/docs/ENVIHeaderFiles.html), cited here as
"the ENVI documentation".

Convention version: 0 (until release, [conventions §1](../conventions.md#1-conventions)) · UUID: `e9428528-2fe3-43f2-8c02-3850cfed2f62` ·
Schema: [schema.json](envi/schema.json)

Changes: [changes/envi/](changes/envi/), one file per change.

Added 2026-11-20.

This document has two parts ([conventions §11.1](../conventions.md#111-parts)).

# Part 1. The convention

## Layout

The hierarchy's entries ([conventions §11.5](../conventions.md#115-layout-tables)):

| prefix | type | structure | description |
|---|---|---|---|
| `zarr.json` | group | [`ImageGroup`](../conventions.md#type-imagegroup) | the root: the image ([§4](#4-the-image)) |
| `zarr.json#/attributes/ome` | attribute | [`OmeImage`](../conventions.md#type-omeimage) | `M`: one level, the array `0`, of scale 1 along every axis, without units, translation or `omero` |
| `zarr.json#/attributes/zarr_conventions` | attribute | array of [`ConventionMetadata`](../conventions.md#type-conventionmetadata) | the CMO of [§1](#1-declaration) |
| `zarr.json#/attributes/refkv_zarrified` | attribute | [`RootProperty`](../conventions.md#type-rootproperty) | `profile` `"envi"`, `version` `0`, `source.url` the header file's URL (the object URL of its key in the store, [spec/zarrify.md §1.4](../zarrify.md#14-store-inputs)) ([§1](#1-declaration)) |
| `zarr.json#/attributes/refkv_zarrified/envi` | attribute | [`EnviRoot`](#type-enviroot) | the root's source metadata ([§5.1](#51-the-root)) |
| `0/zarr.json` | array | [`EnviArray`](#type-enviarray) | the data's values ([§4.1](#41-the-array)) |
| `0/c/<c>/<y>/<x>` | chunk | [`EnviChunk`](#type-envichunk) or [`EnviGzipChunk`](#type-envigzipchunk) | every chunk of the array ([§8.5](#85-chunks)): an `EnviChunk` for a raw file, the one `EnviGzipChunk` for a gzip file |
| `refkv_source/**` | subtree | [the mirror's entries](../conventions.md#table-mirror) | the IR mirror, a store IR ([§5.2](#52-the-ir-mirror)): its elements are [§5.3](#53-elements)'s; the header file, held, is under `refkv_source/ir/held/**` |

| placeholder | value |
|---|---|
| `<c>`, `<y>`, `<x>` | the chunk's coordinates along the array's axes, in decimal: along the data's outermost axis, its index; 0 along the others, and along every axis for a gzip file's one chunk |

## 1. Declaration

The root and `refkv_source` declare the convention
([conventions §11.2](../conventions.md#112-declaration)), the root with the header file's URL as
`source.url` (the object URL of its key in the store,
[spec/zarrify.md §1.4](../zarrify.md#14-store-inputs)) and its source metadata of
[§5.1](#51-the-root) as the member `"envi"`. Its CMO is:

```json
{
  "uuid": "e9428528-2fe3-43f2-8c02-3850cfed2f62",
  "schema_url": "https://d-v-b.github.io/refkv/spec/zarrify/envi/schema.json",
  "spec_url": "https://d-v-b.github.io/refkv/spec/zarrify/envi.html",
  "name": "refkv_zarrified",
  "description": "The Zarr layout of a ENVI source zarrified by refkv, and the source's metadata"
}
```

The array has no attributes.

## 2. The header

An ENVI input is one array, of `bands` bands, each of `lines` lines of `samples` samples. Its
**header** is the whole header file: text, in lines, each ended by a LF (`0A`), a CR (`0D`) before
the LF being part of the line end. A line's **text** is its bytes without its line end; the last
line may end at the file's end, without a LF. The first line is the **magic**: its text, without
the spaces and tabs around it, is `ENVI`, in any case. Each later line is, in this order of tests
(the first row that matches it applies):

| line | when its text, without the spaces and tabs around it | what it holds |
|---|---|---|
| a **continuation** | follows a field whose braced value is open (below) | more of that value: the value gains a LF and the line's text. A line whose text, without the spaces and tabs around it, ends with `}` closes the value |
| **blank** | is empty | nothing |
| a **comment** | starts with `;` | the comment: the text after the `;`, without the spaces and tabs around it |
| a **field** | holds `=`, and the text before the first `=`, without the spaces and tabs around it, is not empty | its **name**, that text in lower case (ASCII letters only), and its **value**, the text after the first `=`, without the spaces and tabs around it. A value that starts with `{` and does not end with `}` is **open**: the lines after it continue it |
| any other | otherwise | nothing: such lines are not fields, and only the mirror keeps them ([§5.2](#52-the-ir-mirror)) |

A name given twice keeps its last value, at its first place. A value open at the end of the header
holds the lines to the end. These are the rules of VirtualiZarr's `ENVIParser`
([its source](https://github.com/zarr-developers/VirtualiZarr/blob/30221eb05a3bb0040956bda1dcc68e6bded5e149/virtualizarr/parsers/envi.py), newer than VirtualiZarr's releases; cited here as "`ENVIParser`" throughout),
whose reading of a header this profile follows; GDAL's ENVI driver reads the same fields of real files.

The layout reads these fields, each named by its name; the others (`description`, `wavelength`,
`map info`, `band names`, `data ignore value`, ...) are recorded as written ([§5.1](#51-the-root)):

| field | what it holds | absent |
|---|---|---|
| `samples`, `lines`, `bands` | the array's sizes | rejected |
| `interleave` | `bsq` (band sequential), `bil` (band interleaved by line) or `bip` (band interleaved by pixel), in any case: the order of the data's axes ([§4.1](#41-the-array)) | rejected |
| `data type` | the code of the values' type ([§3](#3-data-types)) | rejected |
| `byte order` | `0` little-endian, `1` big-endian | 0 |
| `header offset` | the bytes before the data in the data file (in the inflated data of a gzip file, [§4.2](#42-gzip-files)) | 0 |
| `file compression` | `0` none, `1` the data file is gzip ([§4.2](#42-gzip-files)) | 0 |
| `file type` | `ENVI Standard` (in any case) for a flat array; others are rejected | ENVI Standard |
| `major frame offsets`, `minor frame offsets` | frames around each line, which the profile rejects ([§8.2](#82-the-fields)) | |

## 3. Data types

The `data type` field's code gives the data type (the ENVI documentation, "data type"):

| code | Zarr data type | bytes `b` |
|---|---|---|
| 1 | `uint8` | 1 |
| 2 | `int16` | 2 |
| 3 | `int32` | 4 |
| 4 | `float32` | 4 |
| 5 | `float64` | 8 |
| 6 | `complex64` | 8 |
| 9 | `complex128` | 16 |
| 12 | `uint16` | 2 |
| 13 | `uint32` | 4 |
| 14 | `int64` | 8 |
| 15 | `uint64` | 8 |

The other codes (7, 8, 10, 11 and those above 15) are no flat array's, and are rejected
([§8.2](#82-the-fields)). A complex value is its real part, then its imaginary part, each a float
in the file's byte order.

## 4. The image

The hierarchy is one image at the root ([conventions §4](../conventions.md#4-images)), without a
name, with one array at path `"0"`: the root `zarr.json` is `{"zarr_format": 3, "node_type":
"group", "attributes": A}` (an [`ImageGroup`](../conventions.md#type-imagegroup)), where `A` has
`ome` (`M`, an [`OmeImage`](../conventions.md#type-omeimage), its axes `c` of type `channel`, `y`
and `x` of type `space`, its one dataset of scale `[1, 1, 1]`), `zarr_conventions` and
`refkv_zarrified` ([§1](#1-declaration)).

### 4.1 The array

**Axes.** The array's axes are `c`, the bands, `y`, the lines, and `x`, the samples
([conventions §4](../conventions.md#4-images)), whatever the interleave.

**Stored order.** The data is `B = b × bands × lines × samples` bytes: a row-major array `V` of
elements of `b` bytes whose axes, outermost first, the interleave gives:

| `interleave` | the axes of `V`, outermost first | the `transpose` codec's `order` |
|---|---|---|
| `bsq` | bands, lines, samples | none |
| `bil` | lines, bands, samples | `[1, 0, 2]` |
| `bip` | lines, samples, bands | `[1, 2, 0]` |

<a id="type-enviarray"></a>**`EnviArray`**, the array's `zarr.json`: an
[`ArrayDocument`](../conventions.md#type-arraydocument) with these members:

| member | type | presence | value |
|---|---|---|---|
| `shape` | array of integer | required | `[bands, lines, samples]` |
| `data_type` | string | required | from [§3](#3-data-types) |
| `chunk_grid` | object | required | regular. **Chunk shape:** for a raw file, one index of the outermost axis of `V` and the whole of the other two: `[1, lines, samples]` for `bsq`, and `[bands, 1, samples]` for `bil` and `bip`. For a gzip file, the array's shape: one chunk ([§4.2](#42-gzip-files)) |
| `fill_value` | number or array | required | `0`, or `[0, 0]` for a complex data type |
| `codecs` | array of object | required | the codecs below ([conventions §3](../conventions.md#3-arrays)), in their order |
| `dimension_names` | array of string | required | `["c", "y", "x"]` |
| `attributes` | object | required | `{}` ([§1](#1-declaration)) |

Its codecs, in this order:

| codec | when |
|---|---|
| `{"name": "transpose", "configuration": {"order": [...]}}` | the interleave is `bil` or `bip`: the table above |
| `{"name": "bytes", "configuration": {"endian": E}}` | the data type is larger than 1 byte: `E` is `"little"` for `byte order` 0, `"big"` for 1 |
| `{"name": "bytes"}` | the data type is 1 byte |
| `{"name": "refkv.skip", "configuration": {"length": k}}` | a gzip file with a `header offset` `k` above 0 ([§4.2](#42-gzip-files)) |
| `{"name": "gzip", "configuration": {"level": 1}}` | a gzip file: its level, which decoding ignores, is written as the `numcodecs.zlib` codec's is |

**Chunks:** every chunk is present. For a raw file, the chunk at index `i` of the outermost axis of
`V` is the `b × s_1 × s_2` bytes of `V` at that index (`s_1`, `s_2` the sizes of the other two
axes), which the transpose codec puts in the array's order ([`EnviChunk`](#type-envichunk)): a
band of a `bsq` file, a line of a `bil` or `bip` file, as VirtualiZarr's `ENVIParser` cuts them.
No chunk is padded. For a gzip file, the one chunk is the stream
([`EnviGzipChunk`](#type-envigzipchunk)).

The array holds the data's values; nothing the header says about them (`data ignore value`,
`default stretch`, `data gain values`, ...) is applied. They are in the source metadata
([§5](#5-source-metadata)).

### 4.2 Gzip files

With `file compression` 1, the data file is a gzip stream (RFC 1952) whose inflated bytes are what
a raw data file would hold: the header offset's bytes, then `V`. GDAL's ENVI driver reads such a
file as that inflated file, so the header offset counts inflated bytes, which the `refkv.skip`
codec passes over. (VirtualiZarr's `ENVIParser` instead references the stream from the header
offset on; the two agree when the offset is 0, as it is in the files gzip makes of ENVI's.)

A deflate stream has no index, so, as for NRRD's gzip encoding
([nrrd.md §4.2](nrrd.md#42-the-gzip-encoding)), the layout keeps the stream as it is: **one chunk
of the whole array**, a reference to the whole file, which Zarr's `gzip` codec decodes. A reader
decompresses the whole stream to read any value. The profile does not decompress the stream, so a
stream that does not inflate to the header offset and `B` bytes (a damaged file, or one whose
header is wrong) is not detected: reading its chunk fails.

## 5. Source metadata

The root's source metadata `S` ([conventions §2](../conventions.md#2-attributes)) is an object with
the members of [§5.1](#51-the-root), in that order, translated by
[conventions §6](../conventions.md#6-source-metadata-as-json). Everything the files hold is on
`refkv_source`, the input's **IR mirror** ([§5.2](#52-the-ir-mirror)).

### 5.1 The root

<a id="type-enviroot"></a>**`EnviRoot`**, the root's `S`:

| member | type | presence | value |
|---|---|---|---|
| `fields` | object of [`Text`](../conventions.md#type-text) | required | one member per field of the header ([§2](#2-the-header)), in the header's order, named by its name (lower case), whose value is its value as a text value: every field is here, those the layout does not read included (`description`, `wavelength`, `map info`, ...), and as written, a braced value with its braces and line ends: `"wavelength": "{\n 450.0, 550.0}"`. `samples`, `lines`, `bands`, `interleave` and `data type`, which the profile requires ([§8.2](#82-the-fields)), are always among them |
| `comments` | array of [`Text`](../conventions.md#type-text) | required | the comments, in order, as text values |
| `data_type` | string | required | the Zarr data type ([§3](#3-data-types)) |
| `interleave` | string | required | `"bsq"`, `"bil"` or `"bip"` |
| `byte_order` | string | optional | `"little"` or `"big"`, present when the data type is larger than 1 byte |
| `compression` | string | required | `"none"` or `"gzip"` |
| `data_file` | string | required | the data file's key ([§8.3](#83-the-data-file)) |

`fields` and `comments` hold the whole header but its line ends and the lines that are neither,
so that a reader who needs a hyperspectral image's wavelengths, its map projection or its band
names finds them here, as the header wrote them. The other members are derived from them, so that a
reader need not repeat the rules of [§3](#3-data-types) and [§4](#4-the-image).

### 5.2 The IR mirror

`refkv_source` is the input's **IR mirror** ([conventions §8](../conventions.md#8-the-ir-mirror)), a
**store IR** ([conventions §8.9](../conventions.md#89-stores)) whose objects are the two files. Its
leaves partition each object. The data file, which the chunks reference, is **described in
place**: its bytes stay in the file, which is a source of the archive ([§8.5](#85-chunks)). The
header file, which no chunk references, is **held**: the table holds its bytes
(`refkv_source/ir/held`), so that the mirror alone rebuilds it.

| object | key | in the mirror |
|---|---|---|
| the header file | its key | held |
| the data file | its key ([§8.3](#83-the-data-file)) | described in place |

The view, `refkv_source/tree`, shows the header's lines; the data and the gaps are not shown.

### 5.3 Elements

Paths are relative to the IR's root ([conventions §8.1](../conventions.md#81-elements)), whose
children are the objects; the paths below are relative to each object. Types are those of
[conventions §8.6](../conventions.md#86-types).

| path | kind | type | extent: what |
|---|---|---|---|
| `lines/<i>` | value | `ascii[k]` | in the header file: line `i`, its line end included (`k` bytes), the magic first |
| `data` | data | | in the data file: `(o, B)` for a raw file, `o` the header offset; `(0, Z)`, the whole file of `Z` bytes, for a gzip file; its form `{"geometry": {"data_type": T, "shape": S}, "codec": {"interleave": I, "endian": e, "compression": "gzip", "skip": o}, "recipe": [["src", 0, null]]}`, `T` the header's `data type` code (not a Zarr data type, which is the projection's, [conventions §8.5](../conventions.md#85-forms-and-recipes)), `S` the shape of `V` ([§4.1](#41-the-array)), `endian` present when the data type is larger than 1 byte, `compression` for a gzip file, and `skip` for a gzip file whose header offset is above 0 |
| `gaps/<offset>` | gap | | in the data file: the header offset's bytes of a raw file |

### 5.4 Equivalence

The elements are determined by the files. Hierarchies of one input are compared entry for entry,
`refkv_source` included ([conventions §11.4](../conventions.md#114-equivalence)).

## 6. Example

The fixture `fixtures/envi/envi_bil_int16_big.hdr`, served at
`https://example.org/scene/envi_bil_int16_big.hdr` beside its data file
`envi_bil_int16_big.img`, is this header:

```
ENVI
description = {a BIL test raster}
samples = 5
lines = 4
bands = 3
header offset = 0
file type = ENVI Standard
data type = 2
interleave = bil
byte order = 1
band names = {red, green, blue}
```

Its root `zarr.json` has these attributes (the CMO elided):

```json
{
  "ome": {
    "version": "0.5",
    "multiscales": [{
      "axes": [{"name": "c", "type": "channel"}, {"name": "y", "type": "space"}, {"name": "x", "type": "space"}],
      "datasets": [{"path": "0", "coordinateTransformations": [{"type": "scale", "scale": [1, 1, 1]}]}]
    }]
  },
  "zarr_conventions": [C],
  "refkv_zarrified": {
    "profile": "envi",
    "version": 0,
    "source": {"url": "https://example.org/scene/envi_bil_int16_big.hdr"},
    "envi": {
      "fields": {
        "description": "{a BIL test raster}", "samples": "5", "lines": "4", "bands": "3", "header offset": "0",
        "file type": "ENVI Standard", "data type": "2", "interleave": "bil", "byte order": "1",
        "band names": "{red, green, blue}"
      },
      "comments": [],
      "data_type": "int16",
      "interleave": "bil",
      "byte_order": "big",
      "compression": "none",
      "data_file": "envi_bil_int16_big.img"
    }
  }
}
```

Its array has the shape `[3, 4, 5]`, the chunk shape `[3, 1, 5]` and the codecs `[{"name":
"transpose", "configuration": {"order": [1, 0, 2]}}, {"name": "bytes", "configuration":
{"endian": "big"}}]`. Its chunk `0/c/0/2/0`, the third line, is the range `(60, 30)` of
`envi_bil_int16_big.img`: the line's 3 bands of 5 samples of 2 bytes, after the 2 lines before it.
The archive's one url source is the data file's URL, and the root's `source.url` the header's.

## 7. What the layout does not express

- **Georeferencing.** `map info`, `projection info` and `coordinate system string` place the image
  on the earth, and `x start` and `y start` give its first pixel's position. They are recorded,
  not applied: the image has scale 1 and no translation.
- **Wavelengths and band names** are recorded, not made coordinates or channel labels.
- **Other file types**: spectral libraries, classification images, and the TIFF, HDF and other
  files ENVI reads under a header of their own, whose data is not one flat array as this layout
  reads it, and **framed data** (`major frame offsets`, `minor frame offsets`), whose lines have
  bytes between them. Such inputs are rejected, as VirtualiZarr's `ENVIParser` rejects them.
- **Data files of other sizes.** A raw data file must hold the header offset and the array's bytes,
  and nothing else: ENVI writes a file of that size, and one of another size usually means a field
  that changes the layout is not read. VirtualiZarr's `ENVIParser` rejects such a file too.
- **Large bands.** A chunk of a raw file is a whole band or line, however large, as
  VirtualiZarr's `ENVIParser` cuts it, so that both tools reference the same byte ranges.

# Part 2. The profile

The ENVI profile of [spec/zarrify.md](../zarrify.md) is [§8](#8-envi-profile) of this document.
What every profile's Part 2 shares is in [spec/zarrify.md, Profiles](../zarrify.md#profiles). The
input is the header file and the data file beside it, and the profile also says which of them are
read.

## 8. ENVI profile

The input is a **file input** that names the header file (a URL, or a local path), which its
**signature** ([spec/zarrify.md §1.2](../zarrify.md#12-input), tested at the precedence of
[profiles/envi.json](profiles/envi.json)) chose: the bytes tested hold a LF, and the text before
the first, less a CR that ends it and the spaces and tabs around it, is `ENVI` in any case. The
data file has no signature, so it cannot be named: an application that has the data file's name
(VirtualiZarr's `ENVIParser` takes it) names the header beside it instead, `<name>.hdr` or the
data file's name with its extension replaced by `.hdr`, which are the names [§8.3](#83-the-data-file)
probes back. The profile reads the input as a **file read as a store**
([spec/zarrify.md §1.8](../zarrify.md#18-files-read-as-stores)): the store URL is the header file's
directory, its objects are the header file, at its key, and the data file, which the profile probes
for ([§8.3](#83-the-data-file)), and the root's `source.url` is the header file's object URL.

**Siblings** ([spec/zarrify.md §1.9](../zarrify.md#19-sources-beside-the-input)):

| what | it says |
|---|---|
| form | always: every input is a file read as a store |
| names | derived keys, from the header file's key `k` less its last four characters `.hdr` (in any case): [§8.3](#83-the-data-file) |
| probes | one key a batch, in [§8.3](#83-the-data-file)'s order, until one exists; each optional, so one outside the scope is absent. When none exists, the input is rejected |
| identity | the format records nothing that says which files belong together. So the profile checks what it can: that a raw data file is exactly the header offset and the array's bytes |
| order | first use: the data file is source 0 |
| reads and mirror | the header file whole, of at most 2^24 bytes, which the mirror holds; of the data file, only the first three bytes of a gzip file. The mirror's store IR describes the data file in place |

### 8.1 The header file

The header file is checked to be at most 2^24 bytes (`the ENVI header file is <size> bytes, more
than 16777216`) and its key is checked ([§8.3](#83-the-data-file)) before it is read, whole, in one
request. Its first line must be the magic ([§2](#2-the-header)): `not an ENVI header: its first line
is not ENVI`. No other line is rejected: a line that is no field, comment or blank line is not read
([§2](#2-the-header)).

### 8.2 The fields

The fields are checked in this order. A value is read as one **integer** when it is an optional
sign and decimal digits.

- `file type`, when present, is `ENVI Standard`, in any case: `the ENVI file type "<value>" is
  not supported: only an ENVI Standard file is a flat binary array`.
- `major frame offsets` and `minor frame offsets` are absent: `the header has a <field> field: the
  data is in frames, which the profile does not read`.
- `samples`, `lines` and `bands`, each required (`the header has no <field> field`), are integers
  from 1 to 2^53 − 1.
- `interleave` is required, and `bsq`, `bil` or `bip`, in any case.
- `data type` is required, and a code of [§3](#3-data-types)'s table.
- `byte order`, when present, is 0 or 1.
- `header offset`, when present, is an integer from 0 to 2^53 − 1.
- `file compression`, when present, is 0 or 1.

A value that is not as these require is rejected: `the <field> field "<value>" ...`. No other field
is checked: they are recorded as written ([§5.1](#51-the-root)).

### 8.3 The data file

The header file's key `k` MUST end in `.hdr`, in any case, after at least one other character:
`the ENVI header "<k>" is not named <name>.hdr, so its data file cannot be found`. With `s`, `k`
less those four characters, the data file is the first of these keys that exists, each **probed**
in turn, as the NRRD profile probes its data file
([nrrd.md §8.3](nrrd.md#83-the-data-file)): the host says whether the object at the key exists, and
its size (a 404 is absent, any other failure a failure, not a rejection):

1. `s` (`scene.img` for the header `scene.img.hdr`, `scene` for `scene.hdr`, ENVI's own name);
2. `s` followed by each of `.img`, `.dat`, `.raw`, `.bin`, `.bsq`, `.bil` and `.bip`, in that order,
   in capitals when the header's suffix is `.HDR`.

These are the names VirtualiZarr's `ENVIParser` finds a header by, read back: it looks for
`<data file>.hdr`, then the data file's name with its extension replaced by `.hdr`. When none
exists, the input is rejected: `no data file beside the ENVI header "<k>": none of "<key>", ...
exists`, naming each key probed.

### 8.4 The data

With `Z` the data file's size, `o` the header offset and `B` the array's bytes
([§4.1](#41-the-array)), the input is rejected unless all of these hold:

- `B` is at most 2^53 − 1;
- for a raw file, `o` is at most `Z` (`the header offset <o> is past the end of the <Z>-byte data
  file "<key>"`), and `o + B` is `Z` (`the <Z>-byte data file "<key>" does not hold the header
  offset <o> and the array's <B> bytes, and nothing else`);
- for a gzip file, `Z` is at least 18 (a gzip member's header and trailer), and the file's first
  three bytes are `1F 8B 08` (a gzip member of deflate data). The stream is not otherwise read: the
  output does not depend on the pixel data ([spec/zarrify.md §1.2](../zarrify.md#12-input)).

### 8.5 Chunks

Every chunk of the convention's array ([§4.1](#41-the-array)) is present, at `0/c/<c>/<y>/<x>`,
and references the data file; the output copies no data.

<a id="type-envichunk"></a>**`EnviChunk`**, a chunk of a raw file: the chunk at index `i` of the
outermost axis of `V`, of `l = b × s_1 × s_2` bytes:

| part | range | presence | value |
|---|---|---|---|
| the values | source range `(0, o + i × l, l)` | required | the bytes of `V` at index `i`, in the data file |

<a id="type-envigzipchunk"></a>**`EnviGzipChunk`**, a gzip file's one chunk, `0/c/0/0/0`:

| part | range | presence | value |
|---|---|---|---|
| the stream | source range `(0, 0, Z)` | required | the whole data file, which the `gzip` codec inflates and the `refkv.skip` codec, when the header offset is above 0, then passes over the offset's bytes of ([§4.2](#42-gzip-files)) |

The source table is one url source, source 0, the data file's object URL, its size pinned
([spec/zarrify.md §1.4](../zarrify.md#14-store-inputs)). The header file is no source: the mirror
holds it ([§5.2](#52-the-ir-mirror)). The output has no data source. `refkv_source` is the IR mirror
([§5.2](#52-the-ir-mirror)). The archive has no other entries than the root's and the array's
`zarr.json`, the array's chunks, and the mirror's.

### 8.6 Summary

This section is informative. Both implementations print a one-line JSON summary, with these
members:

| member | value |
|---|---|
| `sizes` | each axis's size, by the axis's name |
| `dataType` | the array's data type |
| `interleave` | `"bsq"`, `"bil"` or `"bip"` |
| `compression` | `"none"` or `"gzip"` |
| `dataFile` | the data file's key |
| `chunkShape` | the array's chunk shape |
| `chunks` | the number of the array's chunks |
| `batches` | the read batches, the probes included |
| `elements` | the IR's elements |
| `objects` | the IR's objects |
| `listingRequests` | the files opened beyond the header file |
| the run's counts | the other members the run gives (`reads`, `folded`, ...) |

### 8.7 The description

This section is not part of the convention: nothing it specifies is written in an output. The
parser returns, beside the IR, a **description** of the raster: what it found of the two files, for
a projection to read with the IR and its stored array
([spec/stored-array.md §7](../stored-array.md#7-the-formats)). It holds none of an output's
decisions: the Zarr data type ([§3](#3-data-types)), the axes' names, the `transpose` order, the
codecs, the chunk shape and the fill value ([§4.1](#41-the-array)) and the root's source metadata
([§5.1](#51-the-root)) are the projection's. It is a JSON object with these members:

| member | value |
|---|---|
| `header_file`, `data_file` | each file's `key` in the store and its `size` in bytes: the header file the input names, and the data file [§8.3](#83-the-data-file) found |
| `fields` | every field of the header ([§2](#2-the-header)), in its order, by its name, its value as written, a text value ([conventions §6](../conventions.md#6-source-metadata-as-json)) |
| `comments` | the comments, in order, each a text value |
| `samples`, `lines`, `bands` | the fields' integers ([§8.2](#82-the-fields)) |
| `interleave` | the field's value in lower case: `bsq`, `bil` or `bip` |
| `data_type` | the `data type` field's code, an integer of [§3](#3-data-types)'s table |
| `byte_order`, `header_offset`, `file_compression` | the fields' integers, each 0 where the header has no such field |
| `data_range` | `[start, length]`, the data's range in the data file ([§8.4](#84-the-data)): `[o, B]` for a raw file, `[0, Z]` for a gzip file |
| `elements` | the ids of IR elements ([§5.3](#53-elements)): `header`, the header file's object, which keys the stored array, and `data`, the data file's `data` element, its one unit |

The projection reads the data type and its bytes from `data_type` by [§3](#3-data-types)'s
table, the axes' stored order and the `transpose` order from `interleave`, the `bytes` codec's
endianness from `byte_order`, the gzip codecs from `file_compression` and `header_offset`, and
the chunks from the sizes and `data_range` ([§4.1](#41-the-array), [§8.5](#85-chunks)).

The parser still makes one decision of the output: the IR's `data` element's form names the
Zarr data type ([§5.3](#53-elements)).
