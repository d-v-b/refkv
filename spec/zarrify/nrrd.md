# NRRD

The Zarr layout of a NRRD input (Teem's "nearly raw raster data": a `.nrrd` file whose text
header is followed by its data, or a detached header, `.nhdr`, and the data file it names), and
the translation of its header into JSON. What all of refkv's conventions share is in
[spec/conventions.md](../conventions.md), cited here as "conventions §n". How refkv produces
this layout as a reference store, which files it reads and which inputs it rejects, is the NRRD
profile, [Part 2](#part-2-the-profile) below. The format is Teem's
[Definition of NRRD File Format](https://teem.sourceforge.net/nrrd/format.html), cited here as
"the NRRD definition".

Convention version: 0 (until release, [conventions §1](../conventions.md#1-conventions)) · UUID: `23e0c600-8da6-40f1-81e7-ab246e948e88` ·
Schema: [schema.json](nrrd/schema.json) · Added 2026-10-25 (a profile's changes are recorded
here, dated, from then on)

This document has two parts ([conventions §11.1](../conventions.md#111-parts)).

# Part 1. The convention

## 1. Declaration

The root and `refkv_source` declare the convention
([conventions §11.2](../conventions.md#112-declaration)), the root with the header file's URL
as `source.url` (the object URL of its key in the store,
[spec/zarrify.md §1.4](../zarrify.md#14-store-inputs)) and its source metadata of
[§5.1](#51-the-root) as the member `"nrrd"`. Its CMO is:

```json
{
  "uuid": "23e0c600-8da6-40f1-81e7-ab246e948e88",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/zarrify/nrrd/schema.json",
  "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md",
  "name": "refkv_zarrified",
  "description": "The Zarr layout of a NRRD source zarrified by refkv, and the source's metadata"
}
```

The array has no attributes.

## 2. The header

A NRRD input is one array. Its **header** is text: lines, each ended by a LF (`0A`), a CR
(`0D`) before the LF being part of the line end. A line's **text** is its bytes without its line
end; the last line of a file may end at the file's end, without a LF, its text all of it. The
first line is the **magic**, `NRRD0001` to `NRRD0005`. Each later line is, in this order of
tests:

- a **comment** when its text starts with `#`: the comment is the text from its first byte that
  is neither `#` nor a space (a comment of no bytes is not kept);
- a **key/value pair** when its text holds `:=`: the key is the text before the first `:=`, the
  value the text after it, each unescaped (`\n` is a LF and `\\` a backslash; any other
  backslash is itself). A key given twice keeps its last value, at its first place;
- otherwise a **field**: its **identifier** is the text before the first `:`, in any case, and
  its **descriptor** the text after the `:` (which a space follows, [§8.1](#81-the-header)),
  without the spaces and tabs that start it and the whitespace (space, tab, CR, LF, VT, FF) that
  ends it.

The header of an **attached** input ends with a **blank line**, a line of no text after the
magic, and the data follows it in the same file. A **detached** header (one with a `data file`
field) ends with a blank line or at its file's end, and its data is in its **data file**
([§8.3](#83-the-data-file)).

The fields are those of the NRRD definition, each named here by its name there; some have an
alternate identifier, which names the same field:

| field | alternate identifier | what it holds |
|---|---|---|
| `dimension` | | the number of axes, `n` |
| `type` | | the data type ([§3](#3-data-types-and-encodings)) |
| `block size` | `blocksize` | the bytes of a `block` |
| `encoding` | | how the data is written ([§3](#3-data-types-and-encodings)) |
| `endian` | | `little` or `big`: the data's byte order |
| `content` | | a description of the array |
| `min`, `max` | | the data's least and greatest values |
| `old min`, `old max` | `oldmin`, `oldmax` | the range quantized integer data came from |
| `data file` | `datafile` | a detached header's data file or files |
| `line skip`, `byte skip` | `lineskip`, `byteskip` | the lines, then the bytes, before the data |
| `number` | | the number of values (ignored by the NRRD definition) |
| `sample units` | `sampleunits` | the values' unit |
| `sizes` | | the axes' sizes, one per axis |
| `spacings`, `thicknesses` | | per axis, numbers |
| `axis mins`, `axis maxs` | `axismins`, `axismaxs` | per axis, numbers |
| `centers` | `centerings` | per axis, `cell`, `node`, `???` or `none` |
| `labels`, `units` | | per axis, quoted strings |
| `kinds` | | per axis, what the axis's samples are ([§4.1](#41-the-array)) |
| `space`, `space dimension` | | the space the array lies in, or its dimension `d` |
| `space units` | | per space coordinate, quoted strings |
| `space origin` | | a vector: the first sample's position |
| `space directions` | | per axis, a vector or `none` |
| `measurement frame` | | `d` vectors |

The axes are numbered from 0, the **fastest** first: the data holds the array in the order in
which axis 0 varies fastest and axis `n − 1` slowest. These conventions read the descriptors:

- an **integer** is an optional sign and decimal digits; a **number** is read as the NRRD
  definition (its section 2) reads floating-point text: one that holds `nan` (in any case) is
  NaN, else one that holds `-inf` is −∞, else one that holds `inf` is +∞, and any other is a
  decimal number (an optional sign, digits with an optional fraction, and an optional exponent),
  correctly rounded to binary64;
- per-axis numbers and kinds are separated by spaces and tabs;
- **quoted strings** are `"`, any bytes (`\"` standing for a `"`), `"`, separated by spaces and
  tabs;
- a **vector** is `(`, numbers separated by `,` (spaces and tabs allowed around them), `)`, and a
  list of vectors is vectors and `none` (in any case) separated by spaces and tabs.

## 3. Data types and encodings

The `type` field's descriptor, in lower case, gives the data type:

| `type` | Zarr data type | bytes `b` |
|---|---|---|
| `signed char`, `int8`, `int8_t` | `int8` | 1 |
| `uchar`, `unsigned char`, `uint8`, `uint8_t` | `uint8` | 1 |
| `short`, `short int`, `signed short`, `signed short int`, `int16`, `int16_t` | `int16` | 2 |
| `ushort`, `unsigned short`, `unsigned short int`, `uint16`, `uint16_t` | `uint16` | 2 |
| `int`, `signed int`, `int32`, `int32_t` | `int32` | 4 |
| `uint`, `unsigned int`, `uint32`, `uint32_t` | `uint32` | 4 |
| `longlong`, `long long`, `long long int`, `signed long long`, `signed long long int`, `int64`, `int64_t` | `int64` | 8 |
| `ulonglong`, `unsigned long long`, `unsigned long long int`, `uint64`, `uint64_t` | `uint64` | 8 |
| `float` | `float32` | 4 |
| `double` | `float64` | 8 |

The layout has two **encodings**, from the `encoding` field's descriptor in lower case: `raw`,
whose data is the array's `B = b × s_0 × … × s_{n−1}` bytes (`s_i` the sizes) as they are in
memory, in the byte order of the `endian` field; and `gzip` (or `gz`), whose data is those bytes
compressed into a gzip stream (RFC 1952), which runs to the end of its file. The profile rejects
the others ([§8.2](#82-the-fields)).

## 4. The image

The hierarchy is one image at the root ([conventions §4](../conventions.md#4-images)), without
a name, with one array at path `"0"`: the root `zarr.json` is `{"zarr_format": 3, "node_type":
"group", "attributes": A}`, where `A` has `ome` (`M`, below), `zarr_conventions` and
`refkv_zarrified` ([§1](#1-declaration)).

### 4.1 The array

**Axes.** Each header axis `i` has a **role**:

- its kind, when the header has `kinds` and the kind is neither `???` nor `none`: `domain` and
  `space` are **space**, `time` is **time**, and every other kind (`list`, `vector`, `RGB-color`,
  `3D-symmetric-matrix`, ..., `stub`, `scalar`) is **channel**;
- otherwise, **channel** when the header has `space directions` and axis `i`'s is `none`, and
  **space** when it does not.

The profile accepts 2 or 3 axes of the role space, at most one of time and at most one of
channel ([§8.2](#82-the-fields)). The axes of the role space are, in the header's order, `x`,
`y` and `z`; the axis of the role time is `t` and that of the role channel `c`. The array's
axes are `t`, `c`, `z`, `y`, `x`, each only when present, in that order ([conventions §4](../conventions.md#4-images)), and
the root's source metadata records each header axis's image axis ([§5.1](#51-the-root)).

- **Shape:** each axis's size, in the array's order.
- **Data type:** from [§3](#3-data-types-and-encodings).
- **Stored order.** The data is a row-major array `V` whose axes are the header's, slowest
  first (axis `n − 1` outermost, axis 0 innermost), each of its size; its elements are `b`
  bytes. Its axes are the array's, in another order when the header's axes are not in the order
  `x`, `y`, `z`, `c`, `t` (a color axis stored fastest, a time axis inside the volumes).
- **Chunk shape.** For the raw encoding, `V` is cut as
  [conventions §7](../conventions.md#7-source-values-as-arrays) cuts contiguous values, with
  the chunk limit `L = 131072` (2^17) bytes, as the NIfTI convention cuts its voxels; the
  array's chunk shape is that chunk shape, each axis's size in the array's axis order. For the
  gzip encoding, it is the array's shape: one chunk ([§4.2](#42-the-gzip-encoding)).
- **Codecs** ([conventions §3](../conventions.md#3-arrays)): when the axes of `V` are in another
  order than the array's, `transpose`, with `order` the index in the array's axes of each axis
  of `V`, in the order of `V` (`[1, 2, 0]` for an image whose axis 0 is a color axis, `c`, `y`,
  `x`); then `bytes`, with `"endian"` the `endian` field's (`"little"` or `"big"`) when the data
  type is larger than 1 byte; then, for the gzip encoding, `{"name": "gzip", "configuration":
  {"level": 1}}`, whose level, which decoding ignores, is written as the `zlib` codec's is.
- **Chunks:** every chunk is present. The chunk with coords `q_a` along each axis `a` is the
  chunk of `V` with the same coords along the same axes: for the raw encoding, the bytes that
  [conventions §7](../conventions.md#7-source-values-as-arrays) gives it, in the order of `V`, which the transpose codec puts in the array's
  order, an edge chunk along the cut axis followed by zero bytes up to the whole chunk; for the
  gzip encoding, the gzip stream.

The array holds the data's values; nothing the header says about them (`min`, `max`, `old min`,
`old max`, `sample units`) is applied. They are in the source metadata ([§5](#5-source-metadata)).

### 4.2 The gzip encoding

A gzip-encoded NRRD's data is one deflate stream of the whole array, as the `gzip` program
writes it. A deflate stream has no index: its blocks start at bit offsets that only decoding
finds, and each block may refer to the 32 KiB of output before it, so no part of the stream can
be decoded without decoding the stream up to it. The layout could therefore only cut the array
into chunks at block boundaries by decompressing the data (which a zarrifier must not read,
[spec/zarrify.md §1.2](../zarrify.md#12-input), "Structure only"), and then would need a codec
that starts mid-stream from a given window. It takes the one form that keeps the stream as it
is: **one chunk of the whole array**, a reference to the stream, which Zarr's `gzip` codec
decodes.

The trade-off: nothing is copied and every value is exact, but the chunk is coarse. A reader
decompresses the whole stream to read any value, holds the whole array in memory to do so, and
reads it with no concurrency. This suits the inputs gzip NRRDs usually are (volumes of a few
hundred megabytes at most, such as the IMPC's embryo scans); a reader that needs random access
to a large volume should decompress it once and store it chunked. The profile does not decompress
the stream, so a stream that does not decode to exactly `B` bytes (a damaged file, or one with
other bytes after its gzip members) is not detected: reading its chunk fails.

The bzip2 encoding is rejected for the same reason and one more: bzip2's blocks are independent,
but they too start at bit offsets that only a scan of the compressed data finds, and Zarr v3 has
no `bzip2` codec that both of refkv's readers decode (zarr-python has none of its own; numcodecs'
`numcodecs.bz2` has no counterpart in the browser).

### 4.3 Scales, units and position

A number is **valid** if it is finite and positive. The NRRD definition places an image in a
**space** by `space directions` (a vector per axis: the step in space from one sample to the next
along it) and `space origin` (the first sample's position), and otherwise by `spacings`.
OME-NGFF 0.5 places an image only by a scale and a translation per axis, so the orientation is
**applied** only when it is **axis-aligned**: when the header has `space directions`, every axis
of the role space has a vector, and the vector of the axis `x`, `y` or `z` (the `j`-th, from 0)
has a component `j` (the space has more than `j` coordinates), which is valid, and every other
component 0. (A space of more coordinates than spatial
axes, such as a 2-D image in a 3-D space, or a space with a time coordinate, is axis-aligned when
those components are 0.)

**Scales.**

- `x`, `y`, `z`: with an applied orientation, component `j` of the axis's vector. Otherwise, for
  an axis that has a vector, the vector's length, `sqrt(v_0² + … + v_{d−1}²)` (squares summed in
  order, each operation in binary64, [spec/zarrify.md §1.3](../zarrify.md#13-arithmetic)), when
  every component is finite and the length is valid; and for an axis without one, its `spacings`
  value when the header has `spacings` and the value is valid. Otherwise 1.
- `t`: its `spacings` value when valid, else 1.
- `c`: 1.

So an oblique or flipped orientation, such as the common left-posterior-superior one whose `x`
step is negative, places the image by the directions' lengths alone: it is not rotated or
flipped, and has no translation. The directions themselves are always in the source metadata.

**Units.** An axis whose scale is not 1 for a lack of a valid value has a unit when:

- with an applied orientation, axis `x`, `y` or `z` (the `j`-th): `space units` value `j` is a
  length symbol of [conventions §5](../conventions.md#5-units) (`mm`, `um`, `µm`, `nm`, ...);
- with an orientation that is not applied, an axis with a vector: every `space units` value is
  the same length symbol;
- an axis without a vector, or `t`: its `units` value is a length symbol (`x`, `y`, `z`) or a
  time symbol (`s`, `ms`, `min`, `h`; `t`).

The unit is the symbol's, as [conventions §5](../conventions.md#5-units) maps it; any other symbol gives no unit.

**Translation.** With an applied orientation and a `space origin` whose components are all
finite, the image has a translation ([conventions §5](../conventions.md#5-units)): component `j`
of the origin for the axis `x`, `y` or `z` (the `j`-th), and 0 for `t` and `c`. Otherwise it has
none.

### 4.4 Channels

`M` has an `"omero"` member in these two cases, and in no other:

- **Color:** the image has a `c` axis whose kind is `RGB-color` of size 3 or `RGBA-color` of
  size 4, and its data type is `uint8`: `{"channels": [...]}` with one object per sample,
  `{"label": l, "color": k, "active": true, "window": {"min": 0, "max": 255, "start": 0, "end":
  255}}`, where `(l, k)` are `("R", "FF0000")`, `("G", "00FF00")`, `("B", "0000FF")` and, for
  `RGBA-color`, `("A", "FFFFFF")`.
- **Display range:** otherwise, when the `min` and `max` fields are numbers ([§2](#2-the-header))
  that are finite and `max > min`, and the image's `c` axis has at most 64 samples (1 when it has
  none): `{"channels": [...]}` with one object per index `k` along `c`, `{"label": "C<k>",
  "color": "FFFFFF", "active": true, "window": {"min": lo, "max": hi, "start": lo, "end": hi}}`,
  `lo` and `hi` the two numbers, `k` in decimal.

## 5. Source metadata

The root's source metadata `S` ([conventions §2](../conventions.md#2-attributes)) is an object
with the members of [§5.1](#51-the-root), in that order, translated by
[conventions §6](../conventions.md#6-source-metadata-as-json). Everything the files hold is on
`refkv_source`, the input's **IR mirror** ([§5.2](#52-the-ir-mirror)).

### 5.1 The root

- `magic`: the magic, `"NRRD0001"` to `"NRRD0005"`.
- `fields`: an object with one member per field of the header, in the header's order, named by
  the field's name of [§2](#2-the-header) (a field given by its alternate identifier is named as
  its field: `datafile` is `"data file"`), whose value is its descriptor as a text value. Every
  field is here, those the layout does not read included (`content`, `labels`, `centers`,
  `thicknesses`, `measurement frame`, ...), and as written: `"space directions": "(0.5,0,0)
  (0,0.75,0) (0,0,1.25)"`.
- `key_values`: an object with one member per key/value pair, named by its key (unescaped, a
  name as [conventions §6](../conventions.md#6-source-metadata-as-json) reads one), whose value is the pair's last value, unescaped, as a text
  value. Spaces around `:=` belong to the key and the value, as the NRRD definition says.
- `comments`: the comments, in order, as text values.
- `data_type`: the Zarr data type; `encoding`: `"raw"` or `"gzip"`; `byte_order`: `"little"` or
  `"big"`, present when the header has an `endian` field.
- `axes`: each header axis's image axis (`"x"`, `"y"`, `"z"`, `"t"` or `"c"`), in the header's
  order, the fastest first.
- `data_file`: a detached header's data file, its key ([§8.3](#83-the-data-file)).
- `orientation`: `{"applied": A}`, present when the header has `space directions`: whether the
  orientation is applied ([§4.3](#43-scales-units-and-position)).

`fields`, `key_values` and `comments` hold the whole header but its line ends, so that a reader
who needs, say, a diffusion scan's gradients (key/value pairs such as `DWMRI_gradient_0001`), its
measurement frame, or the full orientation of an oblique volume finds it here, as the header
wrote it. The other members are derived from them, so that a reader need not repeat the rules of
[§3](#3-data-types-and-encodings) and [§4](#4-the-image).

### 5.2 The IR mirror

`refkv_source` is the input's **IR mirror** ([conventions §8](../conventions.md#8-the-ir-mirror)),
a **store IR** ([conventions §8.9](../conventions.md#89-stores)) whose objects are the files the
profile read: the header file, at its key, and a detached header's data file, at its key below
the header's directory. Its leaves partition each object. A file the chunks reference (an attached
header's, which holds the data, and a detached header's data file) is **described in place**: its
bytes stay in the file, which is a source of the archive ([§8.5](#85-chunks)), and the table holds
none of them. A detached header's file, which no chunk references and so no source names, is
**held**: the table holds its bytes (`refkv_source/ir/held`), as a store's documents are held
([conventions §8.9](../conventions.md#89-stores)), so that the mirror alone rebuilds it. The view,
`refkv_source/tree`, shows the header's lines; the data and the gaps are not shown.

### 5.3 Elements

Paths are relative to the IR's root ([conventions §8.1](../conventions.md#81-elements)), whose
children are the objects and directories; the paths below are relative to each object. Types are
those of [conventions §8.6](../conventions.md#86-types).

| path | kind | type | extent: what |
|---|---|---|---|
| `lines/<i>` | value | `ascii[k]` | in the header file: line `i` of the header, its line end included (`k` bytes): the magic first, the blank line last when one ends the header |
| `data` | data | | in the data's file (the header file for an attached header, the data file for a detached one): `(D, B)` for the raw encoding, `(D, size − D)`, the stream to the file's end, for the gzip encoding ([§8.4](#84-the-data)); its form `{"geometry": {"data_type": T, "shape": S}, "codec": {"encoding": E, "endian": e}, "recipe": [["src", 0, null]]}`, `S` the shape of `V` ([§4.1](#41-the-array)) and `endian` present when the header has an `endian` field |
| `gaps/<offset>` | gap | | bytes no other element claims: the lines and bytes line skip and byte skip pass over, the bytes after the data, and a detached header's bytes after its blank line |

### 5.4 Equivalence

The elements are determined by the files. Hierarchies of one input are compared entry for entry,
`refkv_source` included ([conventions §11.4](../conventions.md#114-equivalence)).

## 6. Example

The fixture `fixtures/nrrd/nrrd_space_lps_aligned.nrrd`, served at
`https://example.org/brain/nrrd_space_lps_aligned.nrrd`, is this header and 240 bytes of
little-endian `int16` data after it:

```
NRRD0004
type: short
dimension: 3
sizes: 6 5 4
endian: little
encoding: raw
space: left-posterior-superior
space directions: (0.5,0,0) (0,0.75,0) (0,0,1.25)
space origin: (-12,3.5,40)
space units: "mm" "mm" "mm"
kinds: domain domain domain

```

Its axes are `x`, `y` and `z`, in that order, and its orientation is axis-aligned, so its root
`zarr.json` has these attributes (the CMO elided):

```json
{
  "ome": {
    "version": "0.5",
    "multiscales": [{
      "axes": [{"name": "z", "type": "space", "unit": "millimeter"},
               {"name": "y", "type": "space", "unit": "millimeter"},
               {"name": "x", "type": "space", "unit": "millimeter"}],
      "datasets": [{"path": "0", "coordinateTransformations": [
        {"type": "scale", "scale": [1.25, 0.75, 0.5]},
        {"type": "translation", "translation": [40.0, 3.5, -12.0]}]}]
    }]
  },
  "zarr_conventions": [C],
  "refkv_zarrified": {
    "profile": "nrrd",
    "version": 0,
    "source": {"url": "https://example.org/brain/nrrd_space_lps_aligned.nrrd"},
    "nrrd": {
      "magic": "NRRD0004",
      "fields": {
        "type": "short", "dimension": "3", "sizes": "6 5 4", "endian": "little", "encoding": "raw",
        "space": "left-posterior-superior", "space directions": "(0.5,0,0) (0,0.75,0) (0,0,1.25)",
        "space origin": "(-12,3.5,40)", "space units": "\"mm\" \"mm\" \"mm\"", "kinds": "domain domain domain"
      },
      "key_values": {},
      "comments": [],
      "data_type": "int16",
      "encoding": "raw",
      "byte_order": "little",
      "axes": ["x", "y", "z"],
      "orientation": {"applied": true}
    }
  }
}
```

Its array has the shape and chunk shape `[4, 5, 6]`, the codecs `[{"name": "bytes",
"configuration": {"endian": "little"}}]`, and its one chunk `0/c/0/0/0` is the range `(241, 240)`
of the file, the data after the header's 241 bytes.

The detached header `fixtures/nrrd/nrrd_detached_gzip.nhdr` names the data file
`./nrrd_detached_gzip_files/data.raw.gz`, whose first line its `line skip: 1` passes over: the
array's one chunk is the range `(8, 140)` of the object `nrrd_detached_gzip_files/data.raw.gz`,
the gzip stream to the file's end, its codecs `bytes` and `gzip`; the archive's one url source is
that file's URL, and the root's `source.url` the header's.

## 7. What the layout does not express

- **Several data files.** A detached header whose `data file` field is a list (`LIST`) or a
  pattern (`slice%03d.raw 1 60 1`) splits the array across files, one slab per file, each with its
  own line and byte skips. Each could be a run of chunks of its own file, but a gzip slab is a
  chunk only when the slabs cut the array along chunk boundaries, and the profile's store run
  would probe a file per slab; the profile reads one data file.
- **Text and hex data**, which no chunk can reference as binary values, and **bzip2**
  ([§4.2](#42-the-gzip-encoding)).
- **Block data** (`type: block`), opaque records of no data type.
- **Axes OME-NGFF cannot place**: one spatial axis or more than three, two time axes, or two
  channel axes (a list axis beside a color axis, a matrix stored as two axes). Such inputs are
  rejected rather than given a layout of their own.
- **Oblique orientations**, which are recorded but not applied ([§4.3](#43-scales-units-and-position)).

# Part 2. The profile

The NRRD profile of [spec/zarrify.md](../zarrify.md) is [§8](#8-nrrd-profile) of this document.
What every profile's Part 2 shares is in [spec/zarrify.md, Profiles](../zarrify.md#profiles).
The input is the header file and, for a detached header, its data file, and the profile also
says which of them are read.

## 8. NRRD profile

The input is a **file input** that names the header file (a URL, or a local path), which its
**signature** ([spec/zarrify.md §1.2](../zarrify.md#12-input), tested at the precedence of
[profiles/nrrd.json](profiles/nrrd.json)) chose: bytes 0–6 of `H` are `NRRD000` (`4E 52 52 44
30 30 30`), byte 7 is a digit from `1` to `5`, and byte 8 is a LF or a CR. (The magic of 1998's
files, `NRRD00.01`, does not match.) As the VSI profile reads its slide
([vsi.md §9](vsi.md#9-vsi-profile)), the profile reads the input as a **store**
([spec/zarrify.md §1.4](../zarrify.md#14-store-inputs)) whose URL is the header file's
directory (the URL up to and including its last `/`; for a local path, the URI reference of its
directory, ending in `/`, or `--url`'s directory when `--url` names the header file; a bare file
name with no `--url` records the empty store URL), and whose objects are the header file, at its
key (its name, percent-decoded from the URL), and a detached header's data file, which the
profile finds by key. The store is not listed: the data file is **probed**
([§8.3](#83-the-data-file)). The root's `source.url` is the header file's object URL. The header
file's key holds no `/`; its URL has no query or fragment.

### 8.1 The header

The header file is read from its start: its first `min(size, 65536)` bytes, then, while the
bytes read hold no blank line after the magic and do not reach the file's end, as many bytes
again as are held (each read doubling them), up to 2^24 bytes in all. A detached header's file,
which the mirror holds ([the convention §5.2](#52-the-ir-mirror)), is read whole, in one request:
by its first read when it is at most 65536 bytes, else again, whole, in the run's last batch. The header
([§2](#2-the-header)) is the bytes through the blank line that ends it, or, when none does, the
whole file. The input is rejected when:

- its first line is not `NRRD0001` to `NRRD0005` (the file's first line is checked as soon as it
  is read, or its first 8 bytes when the line is longer): `not a NRRD header: its first line is
  not NRRD0001 to NRRD0005`;
- 2^24 bytes do not hold a blank line and the file is longer: `no blank line ends the NRRD
  header within its first 16777216 bytes`;
- a line is none of a comment, a key/value pair and a field; a key/value pair's key is empty; a
  field's identifier is not followed by a space or the line's end, is not a field's of
  [§2](#2-the-header) in any case, or names a field the header has given already: `line <n> of
  the header: ...`, `<n>` the line's number, the magic's 1;
- the header has no `data file` field and no blank line ends it: `the header ends at the end of
  the file, without the blank line before the data`.

The lines after a `data file` field whose descriptor's first word is `LIST` are the names of the
data files (the NRRD definition's third form, which ends the header), not fields: they are not
read as lines of the header, and [§8.3](#83-the-data-file) rejects the input.

### 8.2 The fields

The fields are checked in this order, each only by what the layout reads of it; the descriptor
is read without the spaces and tabs around it where one value is read. A field the header lacks
is not checked, unless it is required.

- `dimension` is required, an integer from 1 to 16: `n`.
- `type` is required, in the table of [§3](#3-data-types-and-encodings) (in lower case); `block`
  is rejected: `the NRRD type block is not supported: its blocks are opaque, of no data type`.
- `encoding` is required, `raw`, `gz` or `gzip` (in lower case). `ascii`, `text`, `txt` and `hex`
  are rejected (their values or bytes are text, which no chunk can reference as binary), and so
  are `bz2` and `bzip2` ([§4.2](#42-the-gzip-encoding)): `the NRRD encoding <e> is not supported:
  <why>`.
- `endian`, when present, is `little` or `big` (in lower case); it is required when the data
  type is larger than 1 byte.
- `sizes` is required: `n` integers from 1 to 2^53 − 1.
- `line skip` is an integer from 0 to 2^53 − 1, and `byte skip` from −1 to 2^53 − 1. With the
  gzip encoding, `byte skip` MUST be 0: the NRRD definition skips those bytes in the decompressed
  data, which no Zarr codec does (and −1 is the raw encoding's alone).
- `data file`: [§8.3](#83-the-data-file).
- `space` and `space dimension` are not both present. `space` is one of the NRRD definition's
  spaces (in lower case: `right-anterior-superior` or `RAS`, ..., `3D-left-handed-time`), whose
  dimension is `d`; `space dimension` is an integer from 1 to 16, `d`. `space directions`, `space
  origin`, `space units` and `measurement frame` are present only with one of them.
- `space directions`: `n` vectors and `none`, each vector of `d` numbers. `space origin`: one
  vector of `d` numbers. `space units`: `d` quoted strings.
- `spacings`: `n` numbers. `units`: `n` quoted strings. `kinds`: `n` kinds of the NRRD
  definition (in lower case: `domain`, `space`, `time`, `list`, ..., `???`, `none`).
- The axes' roles ([§4.1](#41-the-array)): 2 or 3 of the role space, at most one of time and at
  most one of channel.

A value that is not as these require is rejected: `the <field> field "<descriptor>" ...`. `min`
and `max` are read as numbers when they are ([§4.4](#44-channels)) and are never rejected; no
other field is checked: `content`, `block size`, `old min`, `old max`, `number`, `sample units`,
`thicknesses`, `axis mins`, `axis maxs`, `centers`, `labels` and `measurement frame`'s vectors
are recorded as written ([§5.1](#51-the-root)).

### 8.3 The data file

A detached header's `data file` descriptor names its one data file: a path relative to the header file's directory. Its **key** is the descriptor's
segments (the parts between `/`) without the segments `.`, joined by `/`. The input is rejected
when:

- the descriptor's words (separated by spaces and tabs) are `LIST` and any others, or are 4 or 5
  words of which the first holds `%` and the others are integers: `the data file field
  "<descriptor>" names several data files, which the profile does not read`;
- the descriptor is not UTF-8, starts with `/`, has no segment but `.`, or has an empty segment,
  a segment `..`, or a segment that holds `\` or `:` (a Windows path, a drive, a URL's scheme,
  which could name a file outside the directory): `the data file "<descriptor>" is not a path
  below the header's directory`;
- the key is the header file's: `the data file "<descriptor>" is the header's own file`.

The data file is then **probed**, as the VSI profile probes a tile file
([vsi.md §9.2](vsi.md#92-the-tile-files)): the host says whether the object at its key exists,
and its size (a local file that is there, or an HTTP object whose response to the first request
is not a 404; a 404 is absent, any other failure a failure, not a rejection). An absent data file
rejects the input: `the data file "<key>" does not exist`.

### 8.4 The data

The data's **file** is the header file for an attached header and the data file for a detached
one; its **start** is the header's end for an attached header, and 0 for a detached one.

**Line skip.** When `line skip` is `L > 0` and `byte skip` is not −1, the file is read in windows
of 65536 bytes from the start, and the start moves past the `L`-th LF. The input is rejected when
the file ends first (`line skip <L> passes the end of <file>`), or when 2^24 bytes are read first.

**Byte skip.** The data starts at `D`: the start plus `byte skip`, or, for `byte skip` −1, at
`size − B` of its file, `B` the raw data's bytes ([§3](#3-data-types-and-encodings)), so that the
data ends at the file's end. The input is rejected unless all of these hold:

- `B` is at most 2^53 − 1;
- for the raw encoding, `D + B` is at most the file's size;
- `D` is at least the start (the data does not begin inside the header or the skipped lines);
- for the gzip encoding, the stream, the `size − D` bytes from `D` to the file's end, is at
  least 18 bytes (a gzip member's header and trailer), and its first three bytes are `1F 8B 08`
  (a gzip member of deflate data). The stream is not otherwise read: the output does not depend
  on the pixel data ([spec/zarrify.md §1.2](../zarrify.md#12-input)).

Bytes of the file after the data are not read, as the NRRD definition says.

### 8.5 Chunks

Every chunk of the convention's array ([§4.1](#41-the-array)) is present, at `0/c/<coords>`,
and references the data's file; the output copies no data, but an edge chunk below.

- **Raw:** the chunk is referenced as [conventions §7](../conventions.md#7-source-values-as-arrays)
  references a chunk of contiguous values of `V` at `D` (with the chunk limit 2^17 bytes): one
  range of the data's file, followed for an edge chunk by its zero padding as a literal range;
  when that reference does not fit in a payload ([spec/zarrify.md §1.2](../zarrify.md#12-input)),
  the chunk is copied, with its padding (its bytes are read in the run's last batch).
- **Gzip:** the one chunk, `0/c/0/.../0`, is one range of the data's file: `(D, size − D)`.

The source table is one url source per file some reference uses, its size pinned
([spec/zarrify.md §1.4](../zarrify.md#14-store-inputs)): the data's file, which is the header
file for an attached header. A detached header's file is no source: the mirror holds it
([the convention §5.2](#52-the-ir-mirror)). The output has no data sources. `refkv_source` is the IR mirror ([the convention §5.2](#52-the-ir-mirror)). The archive
has no other entries than the root's and the array's `zarr.json`, the array's chunks, and the
mirror's.

### 8.6 Summary

This section is informative. Both implementations print a one-line JSON summary: `sizes` (each
axis's size), `dataType`, `encoding`, `detached` (whether the data is in a data file),
`dataFile` (the data's file's key), `chunkShape`, `chunks`, `orientation` (whether it is applied,
or `null` without space directions), `translation`, `batches` (the read batches, the probe
included), `elements`, `objects`, `listingRequests` (the files opened beyond the header file),
and the run's counts.
