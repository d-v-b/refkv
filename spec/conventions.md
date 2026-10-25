# refkv's Zarr conventions

A Zarr hierarchy made from a source in another format (a TIFF, an ND2 file, an
N5 store, ...) looks like any other Zarr hierarchy. These conventions make
it describe itself: which source it presents, which format that source is
in, and everything the source's own header or metadata says. There is one
convention per source format, each specified in `zarrify/<format>.md` next to
its JSON Schema `zarrify/<format>/schema.json`. (Changed 2026-10-25: the property is
`refkv_zarrified`, in place of `refkv_virtualized`, [§2](#2-attributes).)

<!-- profiles:conventions-table -->
| format | convention | UUID |
|---|---|---|
| Zeiss CZI | [czi](zarrify/czi.md) | `7a0733c7-d4be-4482-a64f-6904d9354ea5` |
| DICOM | [dicom](zarrify/dicom.md) | `acf17198-e5a5-48d3-8187-22ec4bb40ea5` |
| FITS | [fits](zarrify/fits.md) | `4b0d3a7e-1c52-4f6a-9b8e-2d7f0c6a1e93` |
| FLAC (Free Lossless Audio Codec) | [flac](zarrify/flac.md) | `e9108d07-b24b-432f-a8ce-09d80c8073b4` |
| Imaris IMS | [ims](zarrify/ims.md) | `5067a535-8261-4b25-a93c-1985ed333bde` |
| MP3 (MPEG Layer III audio) | [mp3](zarrify/mp3.md) | `b6cb0047-9448-4fdc-b03e-10dd3d6aa829` |
| MP4 and QuickTime (H.264 video, AAC and MP3 audio) | [mp4](zarrify/mp4.md) | `bbe3f6b9-488d-4d8d-85ce-60a1649dd27e` |
| MRC2014 | [mrc](zarrify/mrc.md) | `2aacd010-2ed1-4748-bc3c-5dd5e840bbce` |
| MRC2014, as a dataset ([§9](#9-dataset-layouts)) | [mrc-dataset](zarrify/mrc-dataset.md) | `f139bf4e-7bab-4c3c-9b2d-66ba747f9b93` |
| N5 | [n5](zarrify/n5.md) | `ad5d4c39-c69e-48f7-a3ef-4cc8c607d416` |
| Nikon ND2 | [nd2](zarrify/nd2.md) | `59612f14-e314-4207-ba00-8f422ba71490` |
| Hamamatsu NDPI | [ndpi](zarrify/ndpi.md) | `6cac71ef-dbb2-4acd-b60c-00389aa4238a` |
| NIfTI-1 and NIfTI-2 | [nifti](zarrify/nifti.md) | `06e5809d-4d54-4b72-afd0-6bf61a7b4c85` |
| NRRD (Teem) | [nrrd](zarrify/nrrd.md) | `23e0c600-8da6-40f1-81e7-ab246e948e88` |
| OME-Zarr 0.4 | [ome-zarr](zarrify/ome-zarr.md) | `b74ea302-65bb-49ae-b81f-f9bb52cd4eed` |
| Akoya QPTIFF | [qptiff](zarrify/qptiff.md) | `329ea136-2f55-4c27-b6f4-8e60ca2109b8` |
| Sentinel-2 SAFE (Level-1C, Level-2A) | [safe](zarrify/safe.md) | `ef81346c-19e8-42ad-93b0-a279ccaf44c1` |
| TIFF, OME-TIFF, Aperio SVS | [tiff](zarrify/tiff.md) | `48e9ac4e-1156-4a62-955e-20467d9c2700` |
| Olympus VSI | [vsi](zarrify/vsi.md) | `c0a1d4e6-5b7f-4f3a-9c2d-8e1f6a7b3c45` |
| Zarr v2 | [zarr2](zarrify/zarr2.md) | `8e792619-d671-4687-ab51-752885dd3ee6` |
<!-- /profiles -->

A format's convention specifies the hierarchy's whole **Zarr layout**: which
groups and arrays it has and where, each array's metadata (shape, data
type, chunk grid, codecs, dimension names), the OME-NGFF metadata, and the
attributes under the convention's key, including the translation of the
source's header into JSON. It says what each chunk holds in terms of the
source (for example, "the chunk at `0/c/t/0/z/0/0` is frame `f`"), but not
how a store produces those bytes. refkv produces these hierarchies as
reference stores whose chunks are byte ranges of the source; that is
specified separately, in [spec/zarrify.md](zarrify.md) and its
profiles, which refer to these conventions for the layout.

Every convention is held to two requirements:

1. **Reconstruction.** A reader of the hierarchy can recover all the
   information of the source. This means its data, and its metadata, and
   every other value it holds, whether or not the layout uses it. What may
   be left out is only what can be derived from the rest, or that holds
   nothing:
   - the **layout**: where the bytes are and how they are framed (offsets,
     byte counts, chunk maps, block headers, the encoding tables a chunk
     already carries);
   - **dead space**: padding and unused bytes.

   Byte-for-byte reconstruction of the file is not required.
2. **Efficiency.** The hierarchy reads well:
   - Small typed metadata is JSON, in attributes.
   - Metadata that is large, or that grows with the data, is in arrays
     ([§7](#7-source-values-as-arrays)), or on the source metadata node ([§2](#2-attributes)), which a reader only opens
     when it wants it.
   - Values are referenced where the source holds them. They are copied
     only when they are scattered or must be reordered.

This document holds what every convention shares. The key words MUST, MUST
NOT, SHOULD, SHOULD NOT and MAY are to be interpreted as described in RFC
2119. Numbers follow [spec/zarrify.md §1.3](zarrify.md#13-arithmetic)
(IEEE 754 binary64, correctly rounded).

## 1. Conventions

Each convention follows the [Zarr conventions
framework](https://github.com/zarr-conventions/zarr-conventions-spec).

**Versions.** Each convention has a **version**, an integer stated in its
document's header.

- **Until refkv's first release**, every convention is at version **0**.
  Version 0 promises nothing: any change may change a layout; a convention's
  document records its changes.
- **At the release**, every convention becomes version 1, tagged
  `zarrify-<p>-v1` in the refkv repository. From then on a convention's
  version increases by 1 whenever the layout it specifies, or the reference
  store that refkv's profile for that format specifies, changes for some
  source, including whether the source is accepted (a **breaking change**);
  an editorial change, or a change to another format, leaves it as it is.
  Version `N` of the convention for format `p` is tagged
  `zarrify-<p>-v<N>`.
- A reader that relies on a convention MUST reject a hierarchy whose
  version it does not know, as each convention's JSON Schema does.

**The convention metadata object.** Format `p` at version `N` is declared by
the Convention Metadata Object (CMO)

```json
{
  "uuid": "<the UUID above>",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/tags/zarrify-<p>-v<N>/spec/zarrify/<p>/schema.json",
  "spec_url": "https://github.com/d-v-b/refkv/blob/zarrify-<p>-v<N>/spec/zarrify/<p>.md",
  "name": "refkv_zarrified",
  "description": "The Zarr layout of a <T> source zarrified by refkv, and the source's metadata"
}
```

with no other members. At version 0, which has no tag, the URLs name the
development branch instead: `.../refs/heads/main/spec/zarrify/<p>/schema.json`
and `.../blob/main/spec/zarrify/<p>.md`. `T` is the format's title, the `title` of its
entry in the profile registry ([zarrify/profiles/](zarrify/profiles/)`<p>.json`: `TIFF`,
`Zarr v2`, `Sentinel-2 SAFE`, ...). The UUID is the same for every version; the version is in
the URLs. All conventions nest their property under one key, `refkv_zarrified`, which is
also their `name`: the framework asks a nesting convention's `name` to be
its key, and a key without a colon is not mistaken for a namespace prefix.

## 2. Attributes

**Which nodes.** The root node of the hierarchy (the document `zarr.json`)
always declares its format's convention. Any other node declares it when,
and only when, the node has source metadata (below). A node that declares
it has, in its attributes, the member `zarr_conventions`, whose value is
`[C]` with `C` the format's CMO (for the SAFE convention, `C` followed by
the CMOs of the GeoZarr conventions the node uses), and the member
`refkv_zarrified`.

**The property.** On the root, `refkv_zarrified` is the object

```json
{"profile": "<p>", "version": N, "source": {"url": "<U>"}, "<p>": S}
```

and on any other node `{"<p>": S}`, where:

- `p` and `N` are the format and its convention's version;
- `U` is the URL of the source: of the file for a file, and of the store,
  ending in `/`, for a store;
- `S` is the node's **source metadata**, an object whose content the
  format's convention specifies (its "Source metadata" section): what the
  source's header or metadata holds for that node, translated by [§6](#6-source-metadata-as-json). The
  member `"<p>"` is present only when `S` has at least one member.

A root whose archive also holds the source under another layout
([§9](#9-dataset-layouts)) has, after `source`, the member `layouts`: an object
whose keys name the layouts (`"dataset"`) and whose values are the paths of
their nodes below the root, without a trailing `/`. There are no other
members. The property holds no time and nothing about the program that
made the hierarchy, so that two conforming producers make equivalent
hierarchies from the same source.

**The source metadata node.** A file format's metadata that grows with the
data (one item per frame, plane or channel), and the source's values kept as
arrays ([§7](#7-source-values-as-arrays)), are on the group `refkv_source`, a child of the root. It declares
the convention, with its own source metadata, when it has any, and holds the
arrays at paths the format's convention gives. OME-NGFF readers ignore it.
Producers SHOULD store its documents so that a reader fetches them only when
it opens the node (refkv writes them with the chunks, not with the documents
read when an archive is opened). The node exists only when it has
source metadata or arrays, or, for the store conventions, other objects of
the store, which it holds whole under `refkv_source/objects/`, or the
recorded ignored keys. For a file's convention,
`refkv_source` is the source's IR mirror ([§8](#8-the-ir-mirror)), from which the source is
rebuilt byte for byte; for a store's, the store's
([§8.9](#89-stores)), from which the listing and the documents read are rebuilt: always
present (under a root array, as plain keys of the archive).

**Nothing else.** Outside `refkv_zarrified`, a node's attributes hold only
members that the hierarchy's target formats define, where the format's
convention places them: `ome` (OME-NGFF 0.5, §4), the GeoZarr members of
the zarr-conventions `proj`, `spatial` and `multiscales` conventions
(`proj:code`, `proj:wkt2`, `spatial:*` and `multiscales`, for the SAFE
convention, which lists their CMOs in `zarr_conventions` after its own),
the CF attributes of a dataset layout's variables ([§9](#9-dataset-layouts)),
and `zarr_conventions`. Everything
taken from the source goes under the key, on the node it belongs to,
including attributes a source store had (and the conventions they
declare), so it can neither collide with nor masquerade as the target
formats' metadata. A source that is itself such a hierarchy therefore
nests its own record.

## 3. Arrays

Unless the format's convention says otherwise (the store conventions, N5,
Zarr v2 and OME-Zarr, define their arrays themselves; the CZI convention
gives clipped levels the zarr-extensions `rectilinear` chunk grid and adds
the codecs `imagecodecs_jpegxr` and `numcodecs.shuffle`; the MP3, MP4 and FLAC
conventions give their arrays the codec `refkv.stream` of [§10](#10-coded-streams) and dimension
names of their own, the MP4 convention gives video planes whose chunks differ in length the
`rectilinear` chunk grid, the FLAC convention shards its array, as
[spec/zarrify.md §2.1](zarrify.md#21-virtual-shards) says, and the NRRD convention compresses a
gzip-encoded array with the `gzip` codec), every array's
`zarr.json` is the JSON object:

```json
{
  "zarr_format": 3,
  "node_type": "array",
  "shape": [...],
  "data_type": "...",
  "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [...]}},
  "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
  "fill_value": 0,
  "codecs": [...],
  "dimension_names": [...],
  "attributes": {}
}
```

with no other members. Chunk keys are `<array path>/c/<i0>/<i1>/...`. A chunk
whose data the file does not contain has no entry (it reads as the fill
value).

**Axes.** An image's axes are a subsequence of `t, c, z, y, x`, in that
order: `t` (time), `c` (channel), `z`, `y`, `x` (space). `y` and `x` are
always present; the format's convention says when the others are.

**Codecs.** `codecs` is built from these, in this order:

1. `{"name": "transpose", "configuration": {"order": [...]}}` when a chunk's
   bytes hold the channel axis last ("interleaved"). `order` lists the array's
   axis indices in stored order: every axis except `c` in array order, then
   `c`.
2. The array-to-bytes codec, one of:
   - `{"name": "bytes"}` (no `configuration`) for 1-byte data types;
   - `{"name": "bytes", "configuration": {"endian": "little"}}` or
     `"big"` for larger ones;
   - `{"name": "imagecodecs_jpeg2k"}` for JPEG 2000 (one codestream per
     chunk, decoding to the chunk's `[y, x]`, or, when interleaved, to
     `[y, x, c]`, after which `transpose` applies). Decoding follows OpenJPEG,
     as imagecodecs does: a 3-component codestream whose first component is
     at full resolution and whose other two are subsampled holds YCbCr, and
     decodes to RGB (as in Aperio's compression 33003);
   - `{"name": "imagecodecs_jpeg"}` for JPEG (one complete JPEG stream, ISO/IEC
     10918-1 with the JFIF/Adobe color conventions, per chunk, decoding to
     the chunk's `[y, x]` or `[y, x, c]` like JPEG 2000).
3. A compressor, when the bytes are compressed:
   - `{"name": "zlib", "configuration": {"level": 1}}` (zlib streams);
   - `{"name": "zstd", "configuration": {"level": 0, "checksum": false}}`.

## 4. Images

An image is a group whose `zarr.json` is
`{"zarr_format": 3, "node_type": "group", "attributes": {"ome": M}}`, where `M`
is the OME-NGFF 0.5 object below. The image's other
attributes are those of [§2](#2-attributes) (the convention's declaration and property), and
no other member is allowed. `M` is:

```json
{
  "version": "0.5",
  "multiscales": [{
    "name": "...",
    "axes": [{"name": "t", "type": "time", "unit": "second"}, ...],
    "datasets": [{"path": "0", "coordinateTransformations": [{"type": "scale", "scale": [...]}]}, ...]
  }]
}
```

- `name` is present only when the format's convention gives one.
- Each axis has `name` and `type` (`"time"`, `"channel"` or `"space"`), and
  `unit` only when the format's convention gives one.
- `datasets` lists the pyramid levels, full resolution first, with paths
  `"0"`, `"1"`, ...; each has a `scale` transformation, one number per axis,
  followed by a `{"type": "translation", "translation": [...]}`
  transformation (one number per axis) only where the format's convention gives one.
- `M` has an `"omero"` member, next to `multiscales`, only where the format's
  convention says so.

## 5. Units

OME-XML unit symbols map to OME-NGFF units:

| symbol | unit |
|---|---|
| `µm` (U+00B5), `μm` (U+03BC), `um` | `micrometer` |
| `nm` | `nanometer` |
| `mm` | `millimeter` |
| `cm` | `centimeter` |
| `m` | `meter` |
| `Å` (U+00C5), `Å` (U+212B) | `angstrom` |
| `pm` | `picometer` |
| `in` | `inch` |
| `ft` | `foot` |
| `s` | `second` |
| `ms` | `millisecond` |
| `min` | `minute` |
| `h` | `hour` |

Any other symbol gives no unit (the scale is still used).

A **length** in one of these units converts to another by the units'
sizes in metres: micrometer 1e-6, nanometer 1e-9, millimeter 1e-3,
centimeter 1e-2, meter 1, angstrom 1e-10, picometer 1e-12, inch 0.0254,
foot 0.3048. A value `v` in unit `a` is `v × (size(a) / size(b))` in unit
`b` (the division first).

**Translations.** Where a convention places an image in space ([§4](#4-images)), the
translation applies to every level, and is 0 for every axis it does not
name. A position given for the centre of an image of level-0 size
`W0 × H0` with x and y scales `sx`, `sy` becomes the translation
`x = cx − W0 × sx / 2`, `y = cy − H0 × sy / 2` (products first).

## 6. Source metadata as JSON

A convention's "Source metadata" section lists, for each node, the source's
fields and how each becomes a member of `S`. Unless it says otherwise:

- **Names.** A field's member is named as the format's own specification
  names the field, and members appear in the order the section lists them.
  A field the source does not have is absent (not `null`). The order of an
  object's members is not significant: producers compare JSON values
  ([spec/zarrify.md §1.1](zarrify.md#11-output-and-equivalence)), and a
  JavaScript producer cannot keep integer-like names in place.
- **Integers** become JSON numbers when their magnitude is at most
  2^53 − 1, and otherwise the string of their decimal digits (with a
  leading `-` if negative).
- **Floating-point numbers** become JSON numbers, the source's value
  converted to binary64 exactly (a binary32 value included). NaN, +∞ and
  −∞ become the strings `"NaN"`, `"Infinity"` and `"-Infinity"`, as Zarr
  fill values do. −0 may be written as `0` or `-0.0`; the two are equal.
- **Text** fields become **text values**: for a fixed-size character
  field, its bytes up to the first NUL (all of them if there is none). If
  the bytes are valid UTF-8, the text value is the JSON string they
  encode. Otherwise it is the object `{"latin1": T}`, where `T` is the
  string in which each byte `b` is the code point U+00`bb` (ISO 8859-1, not
  windows-1252, which some decoders give for that label). The tag keeps two
  byte strings that would read alike apart (`C3 A9` is `"é"`, `E9` is
  `{"latin1": "é"}`), so the bytes can always be recovered.
- **Names** taken from the source (an attribute's or a chunk's name)
  become JSON strings by the same reading, without the tag: UTF-8 if
  valid, else ISO 8859-1. When two names of one object read alike, the
  first, in the order the convention gives, is kept and the others are
  left out (their values are not read).
- **Arrays** of values become JSON arrays, in order.
- **Opaque bytes** (binary fields and payloads a convention does not
  decode) become the string of their base64 encoding (RFC 4648 §4, with
  padding).

Producers compare these as JSON values: numbers by their binary64 values,
strings by their characters ([spec/zarrify.md §1.1](zarrify.md#11-output-and-equivalence)).

These rules apply to values the convention translates from the source's
bytes. Members copied from a JSON document of the source (a store's
attributes) are copied as JSON values, not translated: their numbers are
read as [spec/zarrify.md §1.6](zarrify.md#16-json-documents) says. A
number written as an integer (no fraction or exponent) is copied exactly,
every digit, even beyond 2^53 − 1, and written back so; any other number is
its binary64 value. Where a convention uses a number in the layout, it uses
the binary64 value.

## 7. Source values as arrays

A value that is too large for JSON, that is a column of numbers, or that is
opaque is an array of the source metadata node ([§2](#2-attributes)). Each such array's
`zarr.json` is as in [§3](#3-arrays), with `fill_value` 0 unless the convention says
otherwise, `codecs` `[{"name": "bytes", "configuration": {"endian": E}}]`
(`[{"name": "bytes"}]` for one-byte data types), where `E` is `"little"`
unless the convention gives the source's byte order, followed by `zlib`
(`{"name": "zlib", "configuration": {"level": 1}}`) when the convention
keeps the source's deflate-compressed chunks, and the `dimension_names` the
convention gives (none when it gives none). Its attributes are empty, or the
declaration of the convention with the array's own source metadata.

- **A stream** (one typed value per frame, or per plane) has the data type
  of its values. It is shaped to the image's frame grid, as the convention
  defines it, so that its index along each axis is the image's.
- **Bytes** (a value the convention does not decode) are a 1-D `uint8`
  array, dimension `byte`, of the value's length `n` (at least 1). With
  `k = ceil(n / 2^24)` chunks of `ceil(n / k)` bytes each, the last chunk
  is padded with zero bytes: Zarr stores edge chunks whole.
- **Contiguous values** (an array of shape `S`, of rank `r`, whose elements
  of `e` bytes the source holds contiguously in row-major order, as rows of
  numbers or of fixed-size records) are cut so that no chunk holds more than
  2^24 bytes and none lies outside the array. With `s_j = e × S[j+1] × … ×
  S[r−1]`, the bytes of one index along axis `j`, and `L` the chunk limit
  (2^24 bytes unless the convention gives another), an axis `j` with
  `s_j ≤ L` and `n = S[j]` is cut into `k = ceil(n / floor(L / s_j))`
  chunks, or rather, among the counts `q = k`, …, `min(2k, n)` (each with
  chunks of `c = ceil(n / q)` indices and the edge padding
  `(ceil(n / c) × c − n) × s_j` bytes), the first whose padding is at most
  1024 bytes, or, if none is, the one with the least padding (the first of
  equals), when that padding is at most 64512 bytes (2^16 − 2^10) and,
  times the number of edge chunks (`S[0] × … × S[j−1]`), at most 2^20
  bytes or 1/64 of the array's bytes (`floor(e × S[0] × … × S[r−1] / 64)`),
  whichever is larger. The cut axis `a` is the first `j` with
  `s_j ≤ L` (`e` is at most `L`) that some such count fits, or the last axis,
  with `k` chunks, if none does. The chunk shape is
  1 along the axes before `a`, `c` along `a` and `S[j]` along those after,
  and the grid has `ceil(n / c)` chunks along `a`. The chunk at
  `(i_0, …, i_{a−1}, q, 0, …, 0)` is the `m × s_a` bytes, `m = min(c, n −
  q × c)`, that start `((… (i_0 × S[1] + i_1) × … ) × S[a−1] + i_{a−1}) × n
  × s_a + q × c × s_a` bytes into the values; when `m < c` it is padded with
  `(c − m) × s_a` zero bytes, a literal of the reference when the reference
  fits in a payload ([spec/zarrify.md §1.2](zarrify.md#12-input)), and otherwise the chunk is copied with
  its padding. An array of rank 0 is one chunk; an array with a size of 0
  has no chunks, and the chunk shape `max(1, S[j])`.
- **A family of byte values** (one value per index `i`, such as a per-frame
  blob) is:
  - when every member has the same length `L`, a 2-D `uint8` array of shape
    `[count, L]`, dimensions `index` and `byte`, where `count` is the largest
    index plus 1. When every index has a member and each member starts where
    the one before it ends in the source, the array is contiguous values
    (below), cut as they are. Otherwise, when `L` is at most 2^24, its chunks
    are `[1, L]`, one per member, and an index without a member has no
    chunk; and when `L` is over 2^24, the family takes the next form.
  - otherwise, a group with two arrays:
    - `offsets`: `int64`, of shape `[count + 1]`, cut as contiguous values
      (below). `offsets[i]`
      is where member `i` starts in `data`, and `offsets[count]` is the total
      length (an absent member is empty).
    - `data`: `uint8`, of that total length, holding the members in index
      order, in `k = ceil(total / 2^20)` chunks of `ceil(total / k)` bytes
      (the last padded with zero bytes). It is present only when
      the total is at least 1.

What each chunk holds is these bytes. A producer references them in the
source where it can, and copies them where it must: when the values are
scattered, or when the convention reorders them. Within a chunk of a
family's `data`, the pieces of members that are adjacent in the source (one
ends where the next starts) are one range, so members stored one after
another, such as a TIFF's contiguous strips, are referenced, not copied.

## 8. The IR mirror

Every format's convention keeps the source's metadata as one
format-free structure on `refkv_source`: the source's **IR** (intermediate
representation), a tree of **elements** whose leaves partition the source's
bytes, stored as a table (the **mirror**) and shown as JSON (the **view**).
A reader rebuilds the source byte for byte from the table and the bytes of
source 0, with no knowledge of the format; the format's convention says what
the elements are (its **source model**). Sections 8.1 to 8.7 are all a reader
needs to read the table, check it, and rebuild the source; 8.8 is how a
producer writes it, one way only, so that the mirror of a source is the same
whoever writes it. A store (an N5, Zarr v2 or OME-Zarr store, a SAFE
directory) is many objects: its IR has many sources, one per object, and
[§8.9](#89-stores) says how 8.1 to 8.8 apply to it. An IR of one file is the
special case of one source, source 0, which 8.1 to 8.8 describe as they stand.

### 8.1 Elements

An element has a **kind**: `0` struct (a container), `1` value (a field, of
a declared type, 8.6), `2` data (a decodable unit: a tile, a frame, a
subblock's pixels, with a form, 8.5), `3` derived (bytes that exist only
after a transform, such as a deflated record, with a form that names the
transform and may give their `size`), `4` alias (a second name for an
element already described, its **target**; it claims no bytes), `5` gap
(bytes no other element claims) or `6` object (an object of a store, a source
of its own, only in a store's IR: [§8.9](#89-stores)). It has a **parent** (every element but the
root, which is element 0), a **name** and an optional **name index** (the
element's full name is the name followed by the index in decimal, so
`tags/` with index 256 is `tags/256`), a **type** (values), a **space** (0:
its extent is in the source; `d`: in the derived bytes of element `d`), and
an **extent** (start, length; length 0: none). An element's **path** is the
full names of its ancestors below the root and its own, joined by `/`; the
root's path is `""`.

**The root.** Element 0, the **root**, is a struct with the name `""`, no
name index, space 0 and the extent `(0, Z)`, `Z` the source's size (8.2):
its path, `""`, denotes the whole source, as `""` is the whole document in
JSON Pointer (RFC 6901) and the root node in Zarr. The root is not a run.
Its extent states the range the leaves partition; it claims nothing itself,
since a struct is not a leaf, so coverage and injectivity (below) concern
the leaves alone and are unchanged by it. (When `Z` is 0 the root's length
is 0, so it has no extent.) The empty path is an IR path only: archive keys
stay non-empty ([spec/refkv.md §2.1](refkv.md#21-keys)).

**Names.** Every element but the root has a full name that is not empty.
Among the children of one element (each member of a run being one child),
no full name equals another's, and none equals another's followed by `/`
and more characters (so `tags/256` and `tags/256~1` may be siblings, but
not `a` and `a/0`). Hence every element's path is its own, and no element
but the root has the path `""`: two elements with one path would have, at
the first depth where their chains from the root differ, two siblings
(ancestors, or the elements themselves) whose full names are equal or one
of which continues the other with `/`. A table that breaks these rules is
invalid (8.3). A path
therefore names one element; the view (8.7) gives an alias's target by its
path. A format whose names come from the source (a chunk's name, a record's
key, an XML tag), which may be empty, repeat, or hold `/`, names those
elements so that this holds; [ND2 §5.3](zarrify/nd2.md#53-elements) gives one
rule for that, which any format may adopt.

These rules are those of every file profile's IR (a `.SAFE.zip` included), and of any
format added later, without restating them. A format's convention
lists its elements by their paths below the root.

The **leaves** are the elements of kinds 1, 2, 3 and 5 with a length, in
space 0. The invariants: **coverage**, the leaves' extents partition
`[0, size)` of the source (they cover it and none overlap); **injectivity**,
no byte is claimed by two leaves (two names for the same bytes are an alias
and its target); every parent precedes its children; every alias's target
chain ends at an element that is not an alias, within 64 steps; for each
derived element whose form is a JSON object with a member `size`, the leaves
in its space partition `[0, size)` of its derived bytes; and the root and
the names are as above.

A **run** is an element whose subtree stands for `count` copies of itself
(its **members**): member `j` has every extent of the subtree shifted by
`j × stride` and the run's root's name index increased by `j`. A run's
leaves are its members' leaves. Runs do not nest. A parser may fold its IR
into runs; the canonical table (8.8) expands them, and a reader accepts them.

### 8.2 The table

`refkv_source` declares the convention ([§2](#2-attributes)). Its source metadata `S` is
`{"ir": I}`, where `I` is the object

```json
{"version": 2, "size": Z, "elements": n, "rows": m, "names": [...], "types": [...], "forms": [...]}
```

- `Z`: the source's size in bytes; `n`: the elements, with every column
  run expanded (8.3); `m`: the rows the table stores;
- `names`, `types`, `forms`: the interned strings, whose indexes the rows
  and tables use; `names[0]` and `types[0]` are `""` (`names[0]` is the
  root's name, 8.1).

The table is two arrays under `refkv_source/ir/` ([§3](#3-arrays) documents, data type
`int64`, `fill_value` 0, codecs `bytes` little-endian, which a reader also
accepts followed by `zlib` at any level, though the canonical table, 8.8, has
none; edge chunks padded with zeros; no attributes):

- **`rows`**, shape `[8, m]`, dimension names `column` and `index`, chunk
  shape `[8, c]` with `c = min(m, 16384)` (at least 1): row `k` of the
  stored rows has, in column `0` its kind, `1` its **up** (its expanded id
  minus its parent's; 0 for the root), `2` its name (into `names`), `3` its
  name index (the 64-bit two's-complement pattern of an unsigned index;
  `-1`: none), `4` its type (into `types`), `5` its space (0, or the
  expanded id of a derived element), `6` its **start** and `7` its length.
  The start is coded: for a row with a length, it is the distance from the
  **end** (start + length) of the last earlier stored row with a length (0
  before the first), usually 0, since leaves are mostly in order; for a row
  without a length, it is the start itself.
- **`tables`**, shape `[k, 7]`, dimension names `index` and `part`, chunk
  shape `[min(k, 18724), 7]` (at least 1; no chunk when `k` is 0): each row
  is a table code and up to six values (unused ones 0):
  - `0`, a run: (root, count, stride), `count` at least 1;
  - `1`, an alias: (alias, target), `-1`: no target;
  - `2`, a form: (element, form), into `forms`;
  - `3`, a **column run**: (first row, count, size), 8.3;
  - `4`, a column: (column run, row offset, field, encoding, x, y), 8.3;
  - `5`, six values of `column_values`: the rows of code 5, in order,
    concatenated, are the list `column_values` (padded with zeros to a
    multiple of six).

  Ids in the tables are expanded ids, and the alias, form, run and column
  run rows name stored rows. A row of another code makes the table invalid.

When the IR has shared data sources (bytes several recipes name, 8.5),
`refkv_source/ir/shared` is a group of two arrays holding them, in order:
`offsets`, `int64` `[s + 1]` (where each starts in `data`, then the total),
and `data`, `uint8`, the sources one after another (chunks of at most
131072 and 1048576 values, encoded as the table's). **The rows are
in depth-first order**: every element's subtree is contiguous, its id after
its parent's and before its next sibling's; ids are the order of the
expanded table (the canonical order of 8.8). Derived content is not needed to
rebuild the source: the table may leave out the elements of derived spaces
(the derived elements themselves stay), as 8.8 says.

### 8.3 Column runs

Consecutive siblings of one shape fold into a **column run**: `count`
members of `size` rows each, a member being one to four consecutive sibling
subtrees (so that, for example, a directory's alternating entries and
dimensions fold together). Member 0 is stored as ordinary rows starting at
its **first row**; members 1 to `count − 1` are not stored: their `size`
rows follow member 0 in the expanded order, member `j` at ids
`first + j × size` to `first + (j + 1) × size − 1`. Member `j`'s rows are
member 0's, except:

- **parent**: a row whose parent in member 0 is the first row's parent (a
  sibling at the member's top) has that parent; any other row's parent is
  shifted by `j × size`;
- **fields with a column** (a table row of code 4 for this column run and
  row offset `o`, the row at `first + o` of member 0) take the column's
  value for member `j`. Fields: `0` name index, `1` type, `2` start
  (absolute), `3` length, `4` form (`-1`: none), `5` alias target (`-1`:
  none), `6` start relative to the member's first row (the member's first
  row's start plus the value). Encodings, for member `j`:
  - `0`: `x + j × y` (wrapping 64-bit arithmetic);
  - `1`: `column_values[x] + … + column_values[x + j]` (the column stored as
    differences from the previous member's value; `column_values[x]` is
    member 0's);
  - `2`: entry `y + j` of the array of unsigned integers that row `x`
    holds: row `x` MUST be a value (kind 1) in space 0, with a length, of a
    type that is a one-dimensional array of unsigned integers (`<u2[n]`,
    `>u4[n]`, …, 8.6), not in a column run that has a column of encoding 2;
    its bytes are those of source 0 at its extent, and the column is entries
    `y` to `y + count − 1` (so a TIFF's tiles take their offsets and byte
    counts from its TileOffsets and TileByteCounts, which the source holds);
  - `3` (name index only): the row's start.
- **start, without a column**: a row with a length, in a member whose
  first row has a length, is shifted as its member's first row is (its start
  plus member `j`'s first-row start minus member 0's); any other row keeps
  member 0's.

Forms and alias targets without a column are member 0's. A reader expands
the column runs that have no column of encoding 2 first, then the others.

**Limits.** The expanded table has at most `2^22 + floor(Z / 4)` rows; a
table that would have more is invalid. A producer **rejects** a source whose
canonical mirror (8.8: runs expanded, derived spaces left out) would have more
than `N = 2^22 + floor(Z / 4)` rows, with the message
`budget: the mirror would have more than N rows` (N in decimal), and may do so
before expanding when the elements outside derived spaces, every run expanded,
are already more than `N`. (No source any parser accepts
comes near it today: they describe fewer than one element per four bytes of
the source; the limit bounds what a reader must expand.) So is one whose columns' lengths
differ, whose column refers to values past `column_values` or entries past
its array, whose column run's member 0 is not `size` rows of sibling
subtrees (every row's parent is the first row's parent or an earlier row of
the member), whose up is 0 for a row other than the root or larger than the
row's id, or whose kinds, names, types or forms are out of range. So is one
whose expanded rows break the rules of the root or of the names (8.1): a
root that is not a struct named `""` with no name index in space 0 with the
extent `(0, Z)`; an element other than the root with an empty full name; or
siblings whose full names are equal, or one of which is another's followed
by `/` and more. A reader rejects such a table (8.4) and a validator flags it
as invalid.

### 8.4 Reading the source back

A reader that rebuilds the source:

1. reads `rows` and `tables`, and expands the column runs (8.3);
2. checks the invariants of 8.1, with the runs expanded member by member
   (a run whose members' leaves overlap one another is invalid), the root
   and the names among them;
3. concatenates, for the leaves in order of their start, the bytes of
   source 0 at their extents.

The result is the source, byte for byte: its length is `Z`, which source 0
pins ([spec/zarrify.md §1.2](zarrify.md#12-input)), and a range checksum, when the archive carries
one, checks each range read. The extents index source 0 directly; there is
no array of the source's bytes. (A store's IR rebuilds each of its held
objects from the table alone: [§8.9](#89-stores).)

### 8.5 Forms and recipes

A form is a JSON object: for a data element, `geometry` (the format's
description of the unit, such as its shape and data type), `codec` and
`recipe`; for a derived element, the transform and, when known, `size`;
for an object of a store described in place, `{"bytes":"source"}`
([§8.9](#89-stores)).
A **recipe** is the bytes of the unit as a list of parts: `["src", o, n]`,
the `n` bytes (`null`: up to the element's end) at offset `o` from the
element's start; and `["shared", k]`, shared data source `k` (8.2). The
hierarchy's chunks are references built from recipes; the rebuild does not
need forms.

### 8.6 Types

A value's type is a string of this grammar, and its value is its bytes
decoded by it:

```
type  = base ["[" n {"," n} "]"]
base  = ["<" | ">"] num | "ascii" | "cstr" | "bytes" | "utf16" | "guid" | "{" field {"," field} "}"
num   = "u1" | "i1" | "u2" | "i2" | "u4" | "i4" | "u8" | "i8" | "f4" | "f8"
field = name ":" type
```

`<` and `>` are little- and big-endian (default little); `[n, m]` makes an
array (row-major) of a number or record type, and gives the length of
`ascii`, `cstr`, `bytes` and `utf16`. `ascii[n]` is text (UTF-8 when valid,
else its bytes); `cstr[n]` is the text up to the first NUL, or, when bytes
after it are not all NUL, the record `{text, after}`; `bytes[n]` and
`utf16[n]` are bytes; `guid` is 16 bytes, written
`xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` from a little-endian u32, two u16
and eight bytes; a record is its fields in order. The types `xml` and
`xml:<runtype>` are the UTF-8 text of an XML attribute value (in a derived
space).

### 8.7 The view

`refkv_source/tree` and the groups under it show the canonical IR (8.8) as
JSON. Each is a group whose source metadata `S` ([§2](#2-attributes)) is a struct's **view
document**, so it declares the convention when the document has a member. The
document of the root, the element whose path is `""` (8.1), is `tree`'s.
Sizes are those of the JSON text
ECMAScript's `JSON.stringify` writes (no whitespace), in UTF-8 bytes.

A document has a member per child, in canonical order, by the child's full
name, where a name that starts with `$` gets another `$` in front (so the
members `$vz`, `$form` and `$partial` below are never a child's):

- a **value** is **shown** when its type is a number type, a record type,
  `guid`, `xml`, or a **text type** (`ascii[n]`, `cstr[n]` or `utf16[n]`;
  `bytes[n]` is not text), its length is at most 1024 bytes, and, for ND2,
  it is not a frame's `timestamp` (`frames/<f>/timestamp`, which an ND2
  parser need not read). A producer reads every shown value (its parser
  holds those it reads; the others are read after it). A shown value is, as
  JSON:
  - of a text type, its [§6](#6-source-metadata-as-json) text value `T`: for `ascii` and `cstr`, its
    bytes up to the first NUL (all of them if there is none), the JSON
    string they encode when they are UTF-8, else `{"latin1": L}` with each
    byte `b` the code point U+00`bb`; for `utf16`, its 16-bit little-endian
    units up to the first 0 unit, the string they encode when they are
    UTF-16, else `{"$vz": "utf16", "b64": B}` of those bytes. When the bytes
    from that NUL (or 0 unit) on are not all zero, the value is
    `{"text": T, "after": {"$vz": "bytes", "b64": A}}`, `A` the base64 (RFC
    4648 §4, with padding) of every byte from the NUL on, the NUL included:
    the form a record's `cstr` field takes, so that the view loses nothing;
  - of any other type, its decoded value (a record whose field `v` is its
    value shown as that field; a `v` of type `utf16[n]`, its last two bytes
    dropped, as its text);

  when that JSON is at most 4096 bytes; any other value is the spill form
  `{"$vz": "element", "id": i}`, row `i` of the expanded table, whose bytes
  the source holds;
- a **struct or a derived element**: its own document, inline when at most
  16384 bytes; else `{"$vz": "group", "path": p}`, the group
  `refkv_source/p` holding it, while there are fewer than 4096 such groups,
  the documents stored so far and this one total at most 2^24 bytes, and no
  group has the path `p` yet (paths are the escaped names, each segment
  percent-encoded outside `[A-Za-z0-9._~-]`, an empty one as `%`); else the
  spill form. A derived element's document starts with `"$vz": "derived"`
  and `"$form"`, its form (`null`: none). A document is complete before it
  is sized: the groups its descendants store come first;
- an **alias** whose target is neither data nor a gap:
  `{"$vz": "alias", "path": p}`, the target's path, which names it alone
  (8.1; `""` for the root);
- data, gaps and aliases of data or gaps are not shown.

Values JSON cannot hold are tagged: `{"$vz": "int", "v": digits}` past
2^53 − 1, `{"$vz": "float", "v": "NaN" | "Infinity" | "-Infinity"}`,
`{"$vz": "bytes", "b64": ...}` and `{"$vz": "utf16", "b64": ...}`.

**Budgets.** A document holds at most 65536 bytes, and at most 2^20
elements are visited in all. Each document starts with 65536 bytes left;
for each child in order: the child is visited (counted); if fewer than 1024
bytes are left, or more than 2^20 children have been visited, the document
gets `"$partial": true` and stops. Otherwise the child's member is computed
as above (data and gaps add none), and its size `n` is the member's value's
size plus its key's (as a JSON string) plus 2; when `n` is more than what is
left, the value becomes the spill form and `n` is recomputed; then `n` (or
all that is left) is spent. A key already in the document keeps its first
value. The view need not be complete; the table is. Groups that only lead to
a group are present, with no attributes.

### 8.8 Canonical form

A producer writes the **canonical** mirror: the one table, description and
view this section derives from the source's IR (its elements, and the
decoded bytes of the values the view shows), whatever order the producer
found the elements in and however it folded them. Two conforming producers
therefore write identical `ir/rows`, `ir/tables` and `ir/shared`, the same
description in `refkv_source`'s source metadata and the same view for one
source, and [spec/zarrify.md §1.1](zarrify.md#11-output-and-equivalence)
compares mirrors entry for entry. In order:

1. **Runs.** Every run (8.1) is expanded into its members: member `j`'s root
   has the run root's name index plus `j`, and each element of member `j`
   the extent of member 0's shifted by `j × stride` (the same length), and
   its type, value and form. The table has no row of code 0.
2. **Order.** The elements are numbered depth first from the root (id 0), a
   parent before its children, each subtree contiguous. An element's **first
   byte** is its start when it has a length, else the least first byte of its
   children (none when no element of its subtree has a length; none comes
   after every byte; siblings share a space, so their starts compare). The
   siblings of one name form a **name group**, whose first byte is the least
   of its members'. Siblings are ordered by
   1. their name group: the groups by first byte, then by name (its UTF-8
      bytes);
   2. then, within a group, by name index, as an unsigned integer, with no
      index last;
   3. then by first byte.

   So differently named siblings follow the source's layout, and a family
   (`tiles/0`, `tiles/1`, …) its indexes, wherever its members lie. No two
   siblings tie: siblings of one name differ in name index, since their full
   names differ (8.1), so this order is total and depends on the IR alone.
   Where two leaves claim one byte, the one first in this order keeps it and
   the other becomes an alias (8.1, injectivity).
3. **Derived spaces.** The derived elements are taken in order, skipping
   those inside another derived element. Each keeps its descendants when
   they, with the descendants kept so far, number at most 2^20; otherwise its
   descendants are left out (it stays, with its extent and form). The kept
   elements are numbered again in the same order.
4. **Strings.** `names` and `types` are the distinct names and types of the
   elements, sorted by their UTF-8 bytes (so `""` is first). The shared data
   sources are numbered by first use: for each element in order that has a
   form, each part `["shared",k]` of its recipe in order (as the form's text
   writes it, without spaces) gives source `k` the next number when it has
   none yet. Each form's text is the producer's with every such `k` replaced
   by its number, nothing else changed; `forms` are the distinct texts,
   sorted by their UTF-8 bytes, and `ir/shared` holds the sources by number.
5. **Column runs.** The elements are taken in order; for each element that
   is in no member of a column run found so far, its children
   `c_0, …, c_{k−1}` (in order) are folded from `a = 0`:
   - a child is **eligible** when its subtree has no derived element and no
     element in a derived space; two subtrees have **one shape** when they
     have the same number of elements and, element by element in order, the
     same kind, the same name, both or neither a length, and (all but the
     first) the same parent relative to the subtree's first element;
   - for each **period** `p` from 1 to 4, while `c_a … c_{a+p−1}` exist and
     are eligible: `r` is the largest count such that for every member
     `q < r` and every `t < p`, `c_{a+q·p+t}` exists, is eligible and has
     `c_{a+t}`'s shape;
   - the period with the largest `r × p` among those with `r ≥ 3` wins (the
     smaller period on a tie): children `c_a … c_{a+r·p−1}` are a column run
     of `r` members of `p` siblings each, and `a` moves past them; with none,
     `a` moves to the next child.

   Members' subtrees are not searched for column runs of their own: a column
   run found folds whole subtrees, outermost first. Column runs are listed in
   order of their first row.
6. **Columns.** For each column run, for each row offset `o` of a member,
   the fields are, in this order: name index (`0`), type (`1`), start (`6`,
   relative to the member's first row, when `o > 0` and both row `o` and
   the member's first row have a length; else `2`), length (`3`), form
   (`4`) and alias target (`5`). Member `j`'s value `v_j` is the row's field
   (`-1` for no form or target; field `6` its start minus its member's
   first row's start, wrapping). When every `v_j` is `v_0`, there is no
   column; otherwise the column takes the first encoding that applies:
   - `3`, for a name index: every member's row has a length and `v_j` is
     its start;
   - `0`: `v_j = v_0 + j × (v_1 − v_0)` for every `j` (wrapping), with
     `x = v_0` and `y = v_1 − v_0`;
   - `2`, when one of the profile's **named array rules** (below) gives,
     for this column, a value row `x` and an index `y`, and entries `y` to
     `y + count − 1` of the array `x` holds are `v_0` to `v_{count−1}`;
   - `1`: `x` is the length of `column_values` so far, `y = 0`, and
     `v_0, v_1 − v_0, …, v_{count−1} − v_{count−2}` (wrapping) are appended
     to it.

   Columns are listed by column run, then row offset, then field, in that
   order.

   **Named array rules.** Encoding 2 reads a column from an array the source
   holds, which pays only where a format's layout keeps such arrays; each
   profile lists its rules, and a column takes encoding 2 only under one of
   them. A rule has a name and says, for a column run and a field: the
   conditions on the column run (at least 16 members, so that reading the
   array costs less than storing the column), the value row `x` (a value in
   space 0, with a length, of a one-dimensional unsigned integer array type,
   8.6) and the index `y`. The rules today:

   | profile | rule | applies to | `x` | `y` |
   |---|---|---|---|---|
   | TIFF | layout tables | the start or length of a column run of at least 16 members of one data row each, with a name index, in a struct `tiles` (or `strips`) with no index | the first child `value` (no index) of the first struct child `tags/` with index 324 for starts and 325 for lengths (273 and 279 for `strips`) of that struct's parent: TileOffsets and TileByteCounts (StripOffsets, StripByteCounts) | member 0's name index |

   The other profiles have none. A format adds a rule by a change of this
   section and its convention: a new row in this table, whose conditions
   depend only on the canonical IR (so that every producer finds the same
   columns), and whose array a reader can read from source 0.
7. **Rows and tables.** The stored rows are every row but those of members
   1 and later of column runs, in id order. `tables` holds, in this order:
   an alias row (code 1) for each stored alias, in id order (target `-1`
   when the target was left out, step 3); a form row (code 2) for each
   stored row with a form, in id order; the column runs (code 3) and the
   columns (code 4) as above; and `column_values` (code 5), padded with
   zeros to a multiple of six.
8. **Arrays.** The chunk shapes are those of 8.2, and the codecs `bytes`
   (little-endian) alone: no `zlib`. The archive's own entry compression
   (spec/bindings/vzip.md) does the work, and is not part of the output.
9. **Description and view.** `S` is `{"ir": I}` of 8.2 with `version` 2,
   and the view is 8.7's, from the elements in this order.

**Readers accept, validators flag.** A reader MUST NOT reject a valid table
(8.2 to 8.4) for not being canonical, and a validator SHOULD flag one that
is not. Canonical form serves producers: it makes outputs comparable entry
for entry and the archive of a source reproducible. It adds nothing a reader
needs: the rebuild, the elements and the view are read the same from any
valid table. Checking it means folding the whole table again and, for the
columns of encoding 2, reading arrays from the source, a cost a reader that
wants one value or a rebuild should not pay; and a reader that rejected a
valid but non-canonical table would turn a producer's defect into a reader's
loss of the data. So the check belongs where producers are checked:
`conformance/zarrify/compare.py` loads each mirror, folds it again and
compares the result with what is stored, flagging the first difference (a
run row, a compressed chunk, unsorted strings, rows out of order, a fold or
an encoding other than the one this section gives, a different entry). A
canonical table is a fixed point: loaded and folded again, it is the same,
byte for byte. The validator also rebuilds the view (8.7) from the table and
the source: each shown value's bytes from source 0 at its extent, or, in a
derived space, from that space's bytes, which it derives again by the
derived element's transform (`nd2-lv-zlib`: the record inflated;
`xml-variant`: the chunk; an `xml` value is then its span with its
references decoded); it compares the rebuilt view with the stored one group
by group and member by member, and flags the first group or member missing,
extra or different. So **a derived transform may hold shown values only if
validators can derive it again** from source 0: of the transforms the
parsers emit, `nd2-lv-zlib` and `xml-variant` can, and `gzip` (a CZI's
compressed attachment) and `deflate` (a SAFE zip entry's raw deflate stream),
whose spaces hold nothing, may hold none. A new
transform that would hold shown values comes with its re-derivation, in a
revision of this section.

### 8.9 Stores

A store input ([spec/zarrify.md §1.4](zarrify.md#14-store-inputs)) is
a set of objects named by keys. Its IR, a **store IR**, has many sources: each
object of the store is one. Sections [8.1](#81-elements) to
[8.8](#88-canonical-form) hold for a store IR with the changes this section
gives. An IR without objects, a **file IR**, is the special case of one
source, source 0: this section changes nothing in it, its table or its view.

**Objects and directories.** An element of kind `6` is an **object**: one
object of the store, and a source of its own. Its extent is `(0, s)`, where `s`
is the object's listed size (no extent when `s` is 0), in space 0. The extents
in space 0 of its descendants are ranges of that object's bytes, so an element
names its source by the object it is in, its nearest ancestor of kind 6.
Objects do not nest, and an object is not a run. The root is the store's root
directory: a struct named `""` with no name index, in space 0, with no extent
(in place of 8.1's `(0, Z)`). Every other element outside every object is a
**directory**: a struct with no extent, in space 0, that is not a run, one per
directory of the store
([spec/zarrify.md §1.4](zarrify.md#14-store-inputs)). An object's parent
is the root or a directory. The path of an object or a directory is its key:
the full name of each is its key's segment, by the rule below. So the tree is
the listing: the paths of the objects are the store's keys, and the lengths of
their extents its sizes. A key the listing does not have (a chunk an array
does not store) has no element. The keys a store reader ignores
([spec/zarrify.md §1.4](zarrify.md#14-store-inputs)) are not objects:
the store conventions record them as they do without an IR.

**Names.** A key segment `t` has the name and name index:

- when `t` holds no `~` and is not the key of an object that is also a
  directory's (S3 allows the keys `a` and `a/b` together): let `d` be the digits
  that end `t` (as many as there are). When there are none, the name is `t`, with
  no index. Otherwise the leading zeros of `d` but its last digit stay in the name:
  the index is the value of the rest of `d`, and the name is `t` without that rest.
  When that value is 2^64 − 1 or more, the name is `t`, with no index;
- otherwise the name is `t` with `%`, `/` and `~` written `%25`, `%2F` and `%7E`,
  then `~`, and the index 0, or 1 for an object whose key is also a directory's.

So `0` is (`""`, 0), `a.12` is (`a.`, 12), `007` is (`00`, 7), `.zarray` is
(`.zarray`, none), `c~d` is (`c%7Ed~`, 0), and the object `a` beside the directory
`a` is (`a~`, 1). A full name without `~` is its segment; one with `~` is a
segment the escape gives back. Siblings' full names are therefore distinct, and
hold no `/`: 8.1's rules for names hold. The rule makes the chunk keys of one
array siblings of one name with consecutive indexes (`0`, `1`, … in a nested
directory, `3.0`, `3.1`, … after a `.`), which canonical form folds (below).

**Held, referenced and empty objects.** An object that has children is
**held**: the leaves among its descendants partition `[0, s)` of its bytes, as
the leaves of a file IR partition its source (8.1's coverage and injectivity,
for each object), and the table holds its bytes (`ir/held`, below), so the
mirror alone rebuilds it. A store's documents (an N5 `attributes.json`, a Zarr
`.zarray`, `.zgroup` or `.zattrs`, a SAFE manifest) are held. An object with a
length and no children is **referenced**: it is a leaf, which claims its whole
source; the hierarchy references it whole (its convention says at which key,
such as a chunk object at its own key), and the mirror does not rebuild it. An
object whose size is 0 is **empty**: no element in it has a length.

**Described in place.** A held object's bytes are copied into the archive,
which suits documents, read whole and small. An object that a format reads
in ranges (a SAFE band file: its boxes, main header and tile-part headers,
a few kilobytes of a file of up to 160 MB) can be described without being
held: an object that has children and whose form ([8.5](#85-forms-and-recipes))
is `{"bytes":"source"}` is **described in place**. Its leaves partition
`[0, s)` of its bytes, as a held object's do, but the table does not hold its
bytes: they are its own source's, which a reader reads at the object's URL
([spec/zarrify.md §1.4](zarrify.md#14-store-inputs)), as it reads a file
IR's source 0 ([8.4](#84-reading-the-source-back)). A producer reads every value
the view shows in such an object (its parser holds them). A held object is
then an object with children that is not described in place.

The invariants of 8.1 then become, for a store IR: the elements outside every object
are the root and the directories above; each object's leaves (a referenced
object being its one leaf) partition `[0, s)` of that object, every member of a
run inside it included; and the objects number `o`, the description's (below).
A checker MAY check them at once on **virtual offsets**: byte `x` of object `j`
at `b_j + x`, where `b_j` is the sum of the sizes of the objects before `j` in id
order. The leaves then partition `[0, Z)`, as a file's do, once each leaf is
checked to end within its object.

**The table** (changes to [8.2](#82-the-table)). The description `I` has the
member `"objects": o` after `size`, `o` the number of objects; its presence is
what makes the IR a store's. `Z` is the sum of the objects' sizes. An object
row's start (column 6) is stored as it is (0, since its extent is `(0, s)`),
and the next row with a length codes its start from the object's start (its
distance from 0), as if the object row were a row of length 0 ending there: an
object's descendants' starts count from the start of their own source. The
kinds of rows go to 6. When the IR has held objects, `refkv_source/ir/held` is a
group of two arrays holding their bytes, `offsets` and `data`, as
`refkv_source/ir/shared` holds the shared data sources ([8.2](#82-the-table)):
one byte string per held object, in id order, each the object's `s` bytes. An object described in place has its form row (code 2) and no byte string
in `ir/held`. A column of encoding 2 ([8.3](#83-column-runs)) is invalid in a store IR (no
profile has a named array rule for stores).

**Limits** (changes to [8.3](#83-column-runs)). A store IR has at most 2^24
objects, and its expanded table at most `N = 2^22 + floor(Z / 4) + 4 × o` rows;
the distinct names (the description holds them) total at most 2^24 UTF-8 bytes.
A producer rejects a store past one of these with the message
`budget: more than 16777216 objects`,
`budget: the mirror would have more than N rows` (N in decimal) or
`budget: the mirror's names would total more than 16777216 bytes`, and a reader
rejects such a table. A reader also rejects a table whose held objects (objects with
children and a length, not described in place) are not as many as `ir/held`'s
byte strings, or one of
whose byte strings is not as long as its object.

**Reading the store back** (in place of [8.4](#84-reading-the-source-back)). A
reader reads `rows`, `tables` and `held`, expands the column runs, checks the
invariants above, and then has:

- **the listing**: every object's path, its key, and the length of its extent,
  its size;
- **every held object, byte for byte**: the bytes of its leaves, in order of
  their start, from its byte string in `ir/held`. (The byte string is the object;
  the leaves say what each of its bytes is.)

- **every object described in place, by its elements**: its leaves, which
  partition it, say what each of its bytes is; its bytes are its source's.

It reads no source to do so: a referenced object is not rebuilt, since the
hierarchy references it (a chunk's bytes are the archive's references to the
store), and an object described in place is its own source (the hierarchy
references its ranges; reading it rebuilds it).

**The view** (changes to [8.7](#87-the-view)). Referenced and empty objects are
neither shown nor visited (a store's chunk objects would fill the view's budget
of visits); a held object is shown as a struct is, by its own document; a
directory whose document would be empty is not shown. A shown value of a held
object is read from that object's bytes in `ir/held`, so a validator rebuilds
the view from the archive alone; one of an object described in place, from
that object's source.

**Canonical form** (changes to [8.8](#88-canonical-form)). An object's first
byte is 0, the start of its source, whether or not it has a length, so the
first byte of a directory is 0 too: the objects and directories of one
directory are ordered by name, then by name index, which is the order of the
keys' segments with each family of chunk keys in the order of its indexes.
Within an object, 8.8's order holds, its first bytes being those of that
object's source. The rest of 8.8 is unchanged. In particular, column runs fold
sibling subtrees of one shape: the chunk objects of one array, siblings of one
name with consecutive indexes, fold into a column run of one stored row whose
columns are the name index (encoding 0) and the length (encoding 1, the chunks'
sizes, or 0 when they are all one size). Nested chunk keys (`i/j/k`) fold whole
directories at once, one column run for a full grid; flat keys (`i.j.k`) fold one
column run per name (`i.j.`). The held objects' byte strings are in id order.

**Producers.** A producer lists the store
([spec/zarrify.md §1.5](zarrify.md#15-listing-a-store)), builds the
directories and objects from the listing, then reads the objects its format
names, whole, in as few rounds as the format's structure allows: a round is the
documents whose keys the listing and the documents read so far name (every
`.zarray`, `.zgroup` and `.zattrs` of a Zarr v2 store is one round), read
concurrently, each read once. A held object is an object it read whole.

## 9. Dataset layouts

A source whose image layout ([§4](#4-images)) serves viewers can also be
presented, in the same hierarchy, as a **dataset**: a group that xarray and
other labeled-array readers open as a dataset, whose arrays are its data
variables and coordinate variables, with dimensions named by
`dimension_names` and coordinates that give each index a position. The two
layouts reference the same bytes of the source; neither copies the other's
chunks. A format has a dataset layout when its dataset convention exists:
a convention of its own, named `<p>-dataset` for the format `p`, with its
own UUID, schema and document (`zarrify/<p>-dataset.md`), which
specifies the layout's variables and where each comes from. This section
holds what every dataset layout shares.

**The node.** The dataset is a child group of the root, at the path the
dataset convention gives (`dataset` unless it says otherwise). The root
names it in `layouts` ([§2](#2-attributes)). The group declares the dataset
convention as a root declares its format's ([§2](#2-attributes)): its
`refkv_zarrified` has `profile` (`<p>-dataset`), `version`,
`source` and the source metadata member `"<p>-dataset"`, so that the group
describes itself when it is copied out of the hierarchy. OME-NGFF readers
ignore it, as they do `refkv_source`. The group has no source metadata
node of its own: the source's mirror ([§8](#8-the-ir-mirror)) is the root's
`refkv_source`, which a copied-out group no longer has.

**Variables.** Every array of the group is as in [§3](#3-arrays), with
`dimension_names`, and is a data variable or a coordinate variable:

- A **data variable** holds the source's values. Its shape, data type,
  chunk grid and codecs are those of the image layout's array for the same
  values, its dimensions are named as the dataset convention says (a
  stack's index dimension by what it indexes, `section` or `volume`, not
  by an image axis type), and its chunks are the same references as that
  array's chunks, range for range (an edge chunk's copy is copied again).
  A data variable's attributes hold, when the convention gives them, the
  CF attributes `long_name` and `actual_range` (`[min, max]`, two numbers).
- A **coordinate variable** gives the positions along one dimension: a
  1-D array named as the dimension, of that dimension's length `n`, with
  the attribute `units`, a UDUNITS symbol or name (`angstrom`,
  `micrometer`, `second`), and the chunk shape `[min(n, 2^17)]`. Its
  values are derived from the source's header (the source holds no such
  bytes), so its chunks are bytes entries, in one of two forms:
  - **regular steps** `o + i × s`, the common case, are stored as the
    **index**: a `uint32` array of the values `0 … n − 1` with the CF
    attributes `scale_factor` (`s`) and `add_offset` (`o`), both binary64,
    which a CF-decoding reader (xarray) turns into `i × s + o` in binary64
    (the product first), and the codecs `[{"name": "numcodecs.delta",
    "configuration": {"dtype": "<u4"}}, {"name": "bytes", "configuration":
    {"endian": "little"}}, {"name": "gzip", "configuration": {"level": 6}}]`.
    A chunk's bytes are its indexes delta-coded (the first as it is, each
    other less the one before: `q × c, 1, 1, …`, the ramp going on past
    `n` into the last chunk's padding), as little-endian `uint32`, then one
    gzip member (RFC 1952, no name, time or extra field) of a deflate stream
    at level 6. A ramp of a million positions is then a few kilobytes.
  - **irregular positions** are stored as they are: `float64` values in
    little-endian byte order (`[{"name": "bytes", "configuration":
    {"endian": "little"}}]`), the last chunk padded with zero bytes.

  A dimension without a coordinate variable is allowed, and is how a
  dataset convention leaves out a position it cannot give (an axis with no
  valid voxel size, a stack's index).

Variables have no other attributes, and the group's attributes are
`zarr_conventions` and `refkv_zarrified` alone.

**Equivalence.** A dataset layout is compared entry for entry with the rest
of the hierarchy ([spec/zarrify.md §1.1](zarrify.md#11-output-and-equivalence)):
its coordinate values are the numbers its convention computes, in binary64,
from the header's values, with the operations it gives in their order, so
that every producer writes the same values. A deflated coordinate chunk is
the one place a producer compresses bytes it computed, and deflate streams
differ between encoders, so such chunks are compared by the indexes they
decode to, not byte for byte.

## 10. Coded streams

Audio and video formats code each frame (an MP3 or AAC frame, an H.264
picture) into a run of bytes that a decoder can only decode after the
frames it depends on: an MP3 frame's bit reservoir reaches into the frames
before it, an AAC frame overlaps the one before it, and an H.264 picture
is predicted from other pictures, back to the last keyframe. A chunk of
such a stream's samples therefore holds the frames it decodes to and the
frames before them that a decoder must decode first, its **lead-in**. The
array-to-bytes codec `refkv.stream` decodes such a chunk.

**The codec.** `{"name": "refkv.stream", "configuration": C}`, where `C` has:

- `format`: the coding of the frames: `"mp3"` (MPEG-1, 2 or 2.5 Layer III
  frames), `"aac"` (raw AAC frames, without ADTS headers), `"flac"` (FLAC
  frames, [RFC 9639 §9](https://www.rfc-editor.org/rfc/rfc9639#section-9)) or
  `"h264"` (H.264 access units as an MP4 file holds them: each NAL unit
  preceded by its length, in the length's size the setup gives);
- `setup`, when the format has one: the decoder's configuration, base64
  ([RFC 4648 §4](https://www.rfc-editor.org/rfc/rfc4648#section-4)): for
  `"aac"` the AudioSpecificConfig (ISO/IEC 14496-3 §1.6.2.1), for `"flac"`
  `fLaC` and the stream's STREAMINFO block, header included (42 bytes: the
  form ffmpeg's decoder and WebCodecs' `description` take), for `"h264"`
  the AVCDecoderConfigurationRecord (an MP4 file's avcC box, ISO/IEC
  14496-15 §5.3.3.1), which holds the parameter sets;
- `plane`, for `"h264"`: the plane of a decoded picture the array holds,
  `"y"`, `"u"` or `"v"`.

There are no other members.

**A chunk's bytes** are a header, then the frames:

1. `n`, the frames' count, as a varint;
2. for each frame, in the order of the frames, its `size` as a varint,
   then its `position` less the previous frame's (the first frame's less
   −1) as a zigzag varint;
3. the `n` frames, each `size` bytes, one after the other, in decode
   order.

A **varint** is an unsigned integer as protobuf encodes it, 7 bits a byte,
the least significant first, the high bit set on every byte but the last;
a **zigzag varint** is the varint of `2d` for `d ≥ 0` and of `−2d − 1` for
`d < 0`. So a lead-in frame (−1 after −1) costs one byte for its position,
and a frame whose output follows the one before it (`s` samples on, or one
picture on) one or two. A header that ends inside a varint, or whose sizes
do not add up to the bytes after it, is an error.

**Decoding a chunk.** A decoder of the format, set up with `setup` and in
its initial state, is given the frames in order, one frame each time. Each
frame decodes to some outputs: for audio, `s` samples of each channel; for
video, one picture. The output of frame `k` goes to the chunk's row
`position[k]` on, along the array's first axis: for audio, the `s` rows
from `position[k]`, `[s, channels]`; for video, the one row `position[k]`,
`[1, height, width]` of the plane. A frame of position −1 is decoded and
its output dropped: it is the chunk's lead-in. Every row no frame fills is
the fill value: the rows past the array's end in its last chunk, and the
rows of a frame the decoder gives no output for. An output that does not
fit its rows, or whose shape is not the array's, is an error. The outputs
are the decoder's own samples, with no conversion: `float32` for MP3 and
AAC, the stream's integer samples for FLAC (of the bits per sample the
setup gives, in the array's signed integer type: a decoder that gives them
in the high bits of a wider integer is shifted back), and the 8-bit planes
of a 4:2:0 picture for H.264 (`u` and `v` half the size of `y` along each
axis, rounded up).

The outputs are placed by the frame each comes from, whatever order the
decoder gives them in: an H.264 decoder outputs pictures in presentation
order, after a delay, and may emit nothing for a frame it cannot decode.
A decoder that cannot say which frame an output comes from does not decode
this codec. Nor does one whose output for a frame depends on more than a
bounded run of the frames before it, such as ffmpeg's fixed-point MP3
decoder, which carries its rounding error from frame to frame, so a chunk
it decodes from the lead-in differs from the stream by a unit here and
there. ffmpeg's float MP3 decoder, its AAC decoder, its FLAC decoder and its
H.264 decoder (the implementations' decoders, through PyAV) decode it.

**Determinism.** A chunk decodes to the same values every time, and, for
MP3, FLAC and H.264, to the values a decoder of the whole stream gives
(FLAC and H.264 decoding is exact by their standards; a FLAC frame depends
on no other, so it needs no lead-in). AAC's perceptual noise substitution
fills a band with noise from a pseudo-random generator whose state runs on
from frame to frame, so a chunk's noise bands differ from those of a
decoder of the whole stream; every other value is the same.

**Producing a chunk.** A convention says which frames are a chunk's own,
where each one's output goes, and which frames before them are its
lead-in ([zarrify/mp3.md §8.2](zarrify/mp3.md#82-chunks),
[zarrify/mp4.md §8.3](zarrify/mp4.md#83-chunks),
[zarrify/flac.md §8.2](zarrify/flac.md#82-shards)). A zarrifier
writes the header as a literal range, then the frames as ranges of the
source, a frame that starts where the one before it ends extending that
one's range. The bytes between two frames of the source are in no range,
so a decoder never sees them: one that looked for a run of frames to lock
onto would drop the frames before them, and one that reset its state would
decode the frame after them from nothing. The header holds the frames'
sizes and positions, which are the source's structure, not its samples
([spec/zarrify.md §1.2](zarrify.md#12-input)).

## 11. Format documents

What every format's document, `zarrify/<p>.md`, would otherwise say in
the same words is stated here once, and each document cites it.

### 11.1 Parts

A format's document has two parts. Part 1 is the format's convention: the Zarr layout, which Part 2 cites as "the convention §n".
Part 2 is the format's profile ([spec/zarrify.md, Profiles](zarrify.md#profiles)):
how refkv reads the source, which inputs it rejects, and how each chunk
references the source. The document is numbered on its own, Part 2 after
Part 1, and other documents cite it by name. A dataset layout's document
([§9](#9-dataset-layouts)) is a convention alone.

### 11.2 Declaration

A format's convention starts with its declaration ([§2](#2-attributes)), its
section 1, which shows its CMO ([§1](#1-conventions)) at the version its
header states. Unless the declaration says otherwise:

- the root declares the convention with `"profile": "<p>"`, the convention's
  version as `version`, and the input's URL as `source.url` (a file's URL,
  or a store's, ending in `/`);
- `refkv_source` declares it too, with the IR mirror's description
  ([§8.2](#82-the-table); a store's, [§8.9](#89-stores)), and so does each
  group of the mirror's view that has a member ([§8.7](#87-the-view)); the
  mirror's arrays have no attributes.

The declaration names what is the format's own: the root's source metadata,
the member `"<p>"`, when the root has any, the other nodes that declare the
convention, and the other arrays that have no attributes. A dataset layout's
declaration is [§9](#9-dataset-layouts)'s.

### 11.3 The IR mirror

For a file's convention, `refkv_source` is the file's IR mirror
([§8](#8-the-ir-mirror)): its table, from which the file is rebuilt byte for
byte, and its view, `refkv_source/tree`; for a store's, it is the store's
([§8.9](#89-stores)). The format's convention lists the IR's elements, the
format's **source model**.

### 11.4 Equivalence

The elements are determined by the source (a file, or a store's listing and
the documents the profile reads), and the mirror by its elements
([§8.8](#88-canonical-form): one order, one folding, one encoding):
hierarchies of one source are compared entry for entry, `refkv_source`
included ([spec/zarrify.md §1.1](zarrify.md#11-output-and-equivalence)).

