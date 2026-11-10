# Stored arrays in the IR

A **stored array** is one structure that a source itself declares as an array, described as the
source stores it, at the byte level: its shape, the size of its elements in bits and how they
pack, how the source cuts it into units, the codecs that decode a unit to the elements' bytes, its
fill value, where the source declares what its elements are, and a map of the units the source
holds. Parsers build the descriptions and the map into the IR
([conventions §8](conventions.md#8-the-ir-mirror)); projections and profiles read them, together
with the source's own declarations, and decide everything about the output, starting with the data
type. This document is not a convention: nothing it specifies is written in an output, and the IR
mirror does not record it.

Version: 0 (draft) · Schema: [stored-array.json](stored-array.json)

Added 2026-11-08. The design is design/proposals/ir-array-model.md (d-v-b/refkv#289).

The key words MUST, MUST NOT, SHOULD, SHOULD NOT and MAY are to be interpreted as described in
RFC 2119.

## 1. Stored arrays

The IR describes the source; a projection decides the output. A description holds what the
source holds, as found, and no concept tied to an output encoding: not which arrays are
presented or how they are assembled, not output chunking, shards or padding, not data types, byte
order names or default values, and no byte the source does not hold. Where the IR cannot or need
not interpret something, it falls back to a more general representation: bytes, or where the
source holds them ([§2.2](#22-places-and-parameters)); the source's own id and parameters (the
codec `source`, [§3.2](#32-the-list)); an array whose storage it does not model (a description
with no grid, [§2.3](#23-the-grid)).

Every array a parser reaches has a description, whether or not its profile presents it. Each
format's document lists the structures that are its stored arrays and what each of their
declarations is. Today the NumPy, HDF5, IMS, TIFF, QPTIFF and NDPI parsers describe their arrays,
and the MP4 parser its tracks' samples ([§7](#7-the-formats)).

## 2. The description

A description has these members (the Rust type `StoredArray` of `rust/refkv-ir/src/array.rs`):

| Member | Contents |
| --- | --- |
| `element` | the IR element that declares the array |
| `index` | the array's index among those `element` declares, for a kind of element that may declare several; else absent |
| `declarations` | the places where the source declares what the array's elements are, as the format's document lists them |
| `shape` | the axes in storage order, the slowest-varying first; absent where the source declares no extent (an HDF5 null dataspace), which holds no element, unlike a scalar's empty shape (the JSON form writes `"shape": null`, the member present) |
| `axes` | the source's own order of the axes, where it differs from storage order: `axes[i]` is storage axis i's position in the source's list (a Fortran-order NumPy array of shape (3, 4) has `shape` [4, 3] and `axes` [1, 0]); else absent, never the identity |
| `item_bits` | the bits of one element, decoded; absent for an array with no grid and no given fill, and, of an array with a grid, where its elements are those `source` makes ([§3.2](#32-the-list)) and the IR does not know their size |
| `pad_from` | for elements that are not whole bytes: the first axis from which the elements of a decoded unit pack unbroken, each run over this axis and those after it ending on a byte boundary; absent: the whole unit packs unbroken |
| `grid` | how the source cuts the array into units ([§2.3](#23-the-grid)); absent where the IR does not model the array's storage |
| `chains` | the codec chains a unit may be encoded with, by position in the IR's chain table ([§3.1](#31-chains)) |
| `fill` | the source's fill value ([§2.4](#24-the-fill-value)) |
| `unit_parameters` | the codec parameters whose values differ from unit to unit, each a column of the chunk map ([§3.1](#31-chains)) |

A description stops at the elements' bytes as they come out of a chain. Which data type those
bytes are, in which byte order, how a structure's members lie, how a string or reference is
presented, and what the values mean (a scale, a blank, dimension names) are the profile's: it
reads them from the declarations, or, where a codec makes the elements, from the codec's entry
([§3.2](#32-the-list)).

### 2.1 Keys

A description is keyed by the IR element that declares the array. For a kind of element that may
declare several arrays, the key always adds the array's index among them, in the source's order,
however many it declares; the format's document names those kinds. So `(element, index)` is one
array's alone, and on each element either no description has an index or every one has, as 0 to
n − 1. Within a run, the id is what is computed on; the JSON form also gives the element's path
([§2.5](#25-the-json-form)).

Each format's document, and [§7](#7-the-formats) for the formats this document covers, says how a
profile finds, from the facts its parser gives, the key of each array's description: the
declaring element's id, and the index where the format's kind of element carries one. A profile so
matches what its facts say of an array to the array's description, presented or not. A graft does
not touch the facts: a format whose files a store grafts renumbers the ids its facts give in its
store code, as the HDF5 store's `placed()` renumbers its `store.element` ids. The declaring element
is in no run's subtree ([§5.1](#51-each-description)), so its id names one element, which an unfold
leaves where it is.

### 2.2 Places and parameters

A **place** is bytes of the source:

- an IR element (`element`), with `member`, for an element in a run's subtree, the position j of
  the run's member it is in, and `range`, an (offset, length) within the element's extent (its
  bytes in its own space); or
- where no element holds them, a range of an object by its key (`object`; none for the parsed
  source itself), the `offset` counted from the object's start, and its `length`. In a store IR,
  an object is one of the store's objects, and an external file of a file IR is named by the
  sibling key the run resolves for it, its key in the store.

An element's bytes, for a place, are its extent: as many as its extent's length, in its space. A
place lies within what it names: a range within its element's extent, and an object's range
within the object where its size is known (the parsed source's in a file IR, any object's in a
store IR).

A **parameter** of a codec, or a given fill value, is a value (`{"value": …}`) or a place whose
bytes the codec makes it from (`{"source": place}`). It is tagged, so that a value that is an
object cannot be read as a place.

### 2.3 The grid

A grid has an entry for each axis, `Each(n)` or a `List`, and an `edge`:

- `Each(n)`: the axis's units are all `n` elements long, but for a last one the axis's end cuts.
- `List`: each unit's length in order, a run of `c` equal lengths `n` kept as `[n, c]`.
- `edge`: how a unit past an `Each` axis's end is stored, as the source declares its layout:
  `full`, at the full unit length, the part past the end filled (HDF5's chunks, TIFF's tiles), or
  `short`, cut to the axis (a TIFF last strip).

A grid is in canonical form:

- an axis is `Each(n)` when all its units are `n` long, and, under `short`, also when all but a
  shorter last one are, which the axis's end cuts; otherwise it is a `List` (under `full`, units of
  one length but a shorter last one stay a `List`: `Each` would say the last is stored full);
- under `short`, `n` is the declared length clamped to the axis's length (a TIFF's RowsPerStrip
  above ImageLength, often 2^32 − 1, is one strip of the image's rows); under `full`, `n` is the
  declared length, which may exceed it;
- a `List` sums to the axis's length, and its adjacent equal lengths form one run: `[n, c]` with
  `c` at least 2, or `n` alone;
- lengths are at least 1. An axis of length 0 has no units: it is `Each` of the declared length,
  or an empty `List` where the source declares none.

An array whose storage the IR does not model (an HDF5 virtual dataset, a NumPy object array) has
no grid, no chains and no chunk map rows: its element, shape, declarations and whatever else its
parser knows. The profile reads the declaring structure for the rest, or leaves the array out.

### 2.4 The fill value

`fill` is one of:

- `undefined`: the source defines none (HDF5's "undefined", and a format that has no fill value),
  or defines one the IR cannot express, whose place is then among the description's
  `declarations` (the general fallback); a profile tells the two apart through the format's list of
  declaration places;
- `format_default`: the source says its format's default (HDF5's "default", which the HDF5
  specification defines as zero bytes);
- `given`: one decoded element's bytes, ceil(`item_bits` / 8) of them, as a parameter
  ([§2.2](#22-places-and-parameters)): a value, the bytes as a base64 string (RFC 4648 §4, with
  padding), or the place that holds them (a range, an extent, or an element's whole extent). An
  element of fewer bits than whole bytes takes the first bits of its bytes, packed as the array
  packs them.

### 2.5 The JSON form

The descriptions cross the bindings as one JSON document,
`{"chains": [...], "arrays": [...], "undescribed": …}`: the chain table, each chain a list of codecs
`{"name": …, "parameters": {name: parameter}}`; the descriptions in the IR's order; and the mark of
a source that kept no descriptions at all at the descriptions' limit ([§6](#6-in-the-ir)), in a file
IR `true` or `false`, in a store IR a list of the keys of the objects whose arrays kept none, empty
where none is marked. A description has every member of [§2](#2-the-description),
`null` where it is absent, and `path`, its element's path in the IR. A place is
`{"element": e, "member": j, "range": [offset, length]}` (`member` and `range` may be null) or
`{"object": key, "offset": o, "length": n}` (`object` null for the parsed source). A grid is
`{"axes": [...], "edge": "full" | "short"}`, each axis `{"each": n}` or `{"list": [...]}`, each
run of a list `n` or `[n, c]`. A fill is `"undefined"`, `"format_default"` or
`{"given": parameter}`. A unit parameter is `{"chain": c, "codec": k, "name": …}`. The JSON
Schema [stored-array.json](stored-array.json) gives the document, and, under `$defs`, the
description (`stored_array`), which each format's description schema refers to for its arrays.
Integers are written exactly, at any size.

## 3. Codecs

### 3.1 Chains

A chain lists every step the source encodes a unit with, in encoding order. Decoding runs it from
last to first, on the unit's bytes as its element or ranges give them ([§4](#4-the-chunk-map)),
and ends with the elements' bytes:

- **as the source stores them**, in its byte order, packed as `item_bits` and `pad_from` say, with
  bits most significant first within a byte; or
- **as a codec makes them**: a codec that makes the elements rather than restoring them (JPEG's
  decoding, GRIB's unpacking) states in its entry the bytes it makes.

Every codec takes bytes to bytes. An array stored as is has one empty chain.

The IR holds one table of distinct chains, and a description names its chains by their positions
in it, each once. A unit's chain is the description's first, `chains[0]`, unless its row gives
another position in the description's `chains` ([§4](#4-the-chunk-map)); every chain of an array
ends in the same element bytes.

A unit parameter names parameter `name` of the codec at position `codec` of the description's
chain `chain` (a position in its `chains`), and a description lists each once; each unit of that
chain that does not skip the codec gives its value in the chunk map's column
`p.<codec name>.<name>`. A parameter is given either in
its codec or as a column, never both; a codec whose parameter is a column appears once in its
chain; and a codec sees a unit's bytes, its parameters, and the unit's decoded shape (its grid
unit, cut at the array's end under `short` and whole under `full`) with the array's `item_bits`
and `pad_from`, never its coordinates, so a parameter that depends on the unit's position other
than through its shape is a column its parser computes. (TIFF's LZW decodes up to the unit's size,
and its predictor sums along the unit's rows.)

**Streams.** A codec whose entry says its units are a stream does not decode a unit apart: a
video picture is predicted from pictures decoded before it. Its units decode in the order of the
array's first axis, which is the source's decode order, each with the state of a decoder that has
decoded the units before it. A unit still decodes to its own elements, one picture: a decoder
gives its pictures in another order than it takes their units, and keeps which is which. The
codec's unit parameter `sync` says where a decoder may start with no state: at a unit whose
`sync` is 1. Started there, it makes that unit's elements, and those of the units after it, as a
decoder of the whole stream does, but for a unit after it that the source presents before it (a
leading picture of an open group), which may be predicted from units before the sync unit: a
decoder started at the sync unit before that one makes it. When a unit is presented is the
profile's, which reads the source's tables; the IR does not model it.

Configuration the source defines once, apart from the units, is a parameter given as a place,
so that the description says where it is rather than copying it. Parameters only an encoder needs
(a zlib level, a block size) are not recorded.

### 3.2 The list

A codec's name is one of this list. Each entry gives:

- the exact byte format its decoding takes, its framing included: a header, a trailer, a block
  layout or a checksum the source writes around the encoded bytes (HDF5's LZ4 filter writes its own
  block layout, not an LZ4 frame; LZF has no header; `zlib` is RFC 1950, with its header and
  Adler-32, not raw deflate). A name never covers two framings, unless a parameter of its entry
  chooses between them (HDF5's bitshuffle filter, whose compression parameter frames its blocks
  with LZ4 or leaves them bare, would be one entry), or the encoding's own first bytes tell them
  apart (a JPEG 2000 codestream and a JP2 file that holds one); otherwise an encoding the sources
  frame two ways is two entries. An encoding HDF5 frames its own way is named with the prefix `h5_`
  (`h5_lz4`, `h5_szip`, `h5_bitshuffle`);
- its parameters, which of them a unit may give as a column and the column's Arrow type;
- what its decoding makes;
- for a parameter given as a place, how the parameter is made from those bytes.

A chain's codec has only its entry's parameters.

| Codec | Takes | Parameters | Columns | Decoding makes |
| --- | --- | --- | --- | --- |
| `source` | the bytes the source's encoding wrote, framed as the source frames them | `format`: the profile's name; `id`: the source's id for the encoding, as the source gives it; `name`: the source's own name for the encoding, where it gives one (an HDF5 filter's optional name); `parameters`: its parameters, as the source gives them (a value, or a place that holds them) | none | what the source's encoding decodes to, elements of `item_bits` bits where the description gives it, and of the size that encoding gives them otherwise; the profile maps it or leaves the array out |
| `zlib` | a zlib stream ([RFC 1950](https://www.rfc-editor.org/rfc/rfc1950)) | none | none | the bytes it inflates to |
| `gzip` | a gzip stream ([RFC 1952](https://www.rfc-editor.org/rfc/rfc1952)): one member, or several one after another | none | none | the bytes its members inflate to, joined |
| `zstd` | a Zstandard frame ([RFC 8878](https://www.rfc-editor.org/rfc/rfc8878)) | none | none | the frame's content |
| `bzip2` | a bzip2 stream | none | none | the bytes it decompresses to |
| `lzf` | one LZF block with no header; its decoded length is not stored | none | none | the block's bytes |
| `skip` | `length` bytes that are none of the unit's elements (a header the source stores in one stream with them), then the elements' bytes | `length`: a value | none | the bytes after the first `length` |
| `blosc` | a Blosc 1 chunk, with its 16-byte header | none | none | the chunk's bytes |
| `skip` | `length` bytes that a reader passes over, then the bytes its decoding makes | `length`: a place of the count as the source writes it, in ASCII: decimal digits, a `+` before them allowed (the descriptor of NRRD's `byte skip`) | none | the bytes after the first `length` |
| `shuffle` | the bytes of elements of `element_size` bytes each: every element's first byte, then every element's second, and so on, then the bytes past the last whole element as they are | `element_size`: a place of one value | none | the elements' bytes, each element's together |
| `h5_fletcher32` | bytes, then their Fletcher-32 checksum as HDF5 computes it (4 bytes) | none | none | the bytes before the checksum |
| `h5_lz4` | the layout of HDF5's LZ4 filter (32004): the decoded length (8 bytes, big-endian), a block size (4 bytes, big-endian), then each block as its stored length (4 bytes, big-endian) and an [LZ4 block](https://github.com/lz4/lz4/blob/dev/doc/lz4_Block_format.md), or the block's bytes as they are where the stored length is the block's own | none | none | the blocks' bytes, joined |
| `h5_szip` | what HDF5's SZIP filter (4) writes | `options`, `pixels_per_block`, `bits_per_pixel`, `pixels_per_scanline`: each a place of one value | none | what the filter decodes it to |
| `h5_nbit` | what HDF5's N-bit filter (5) writes | `parameters`: a place of every value the filter has, which give the datatype it packs | none | what the filter decodes it to |
| `h5_scaleoffset` | what HDF5's scale-offset filter (6) writes | `parameters`: a place of every value the filter has | none | what the filter decodes it to |
| `h5_bitshuffle` | what HDF5's bitshuffle filter (32008) writes ([zarrify/hdf5.md §3.4](zarrify/hdf5.md#34-the-codec-refkvh5_bitshuffle)) | `element_size`, and, where the filter gives them, `block_size` (0: the filter's default) and `compression` (0: the blocks bare; 2: framed with LZ4): each a place of one value | none | the elements' bytes |
| `xz` | an .xz stream | none | none | the bytes it decompresses to |
| `tiff_lzw` | one TIFF LZW stream (TIFF 6.0 §13: codes packed most significant bit first, with the early change every TIFF writer uses now); or, where its first byte is 0 and its second odd, one of the older form libtiff calls old-style and tells by those bytes (codes packed least significant bit first, the code's width growing one code later) | none | none | the stream's bytes, up to the unit's: decoding stops there, and reads no code after ([zarrify/tiff.md §4.3](zarrify/tiff.md#43-data-types-and-codecs)) |
| `packbits` | PackBits runs (TIFF 6.0 §9) | none | none | the runs' bytes |
| `tiff_predictor` | a unit's rows, each differenced along its columns as TIFF's Predictor says: 2, each sample less the same sample of the pixel before it, as unsigned integers of `item_bits` bits in the byte order `byte_order` gives, modulo 2^`item_bits` (TIFF 6.0 §14); 3, the row's samples' most significant bytes, then their next and so on to their least, each byte less an earlier byte of that row of bytes, modulo 256 (Adobe's [TIFF Technical Note 3](http://chriscox.org/TIFFTN3d1.pdf)); that byte is as many places before it as a pixel has samples. A pixel's samples are the unit's last axis where `planar_configuration` is absent or 1 and the unit has three axes, else one | `predictor`, and `planar_configuration` where the image has more than one sample and the field: each a place of a field's one value; `byte_order`: a place of the file's byte order mark | none | the rows' elements, summed back, in the file's byte order |
| `ccitt_t6` | one ITU-T T.6 (Group 4) stream of the unit's rows | none | none | elements of 1 bit, 1 for a coded black run and 0 for a white one, most significant bit first, each row padded to a whole byte |
| `jpeg` | one JPEG stream (ISO/IEC 10918-1) of 8-bit samples; with `tables`, an abbreviated stream whose tables `tables` holds | `tables`: a place of an abbreviated stream of tables alone (TIFF's JPEGTables) | none | the frame's samples, one byte each, a pixel's components together in the frame's order, every component at the frame's size. Converting their color space is the profile's, which reads how the source declares it |
| `jpeg_restart_interval` | the entropy-coded data of one restart interval of a baseline JPEG stream's scan (ISO/IEC 10918-1), without the restart marker or EOI after it | `header`: a place of the stream's header | none | the samples of the interval's MCUs, one byte each, a pixel's components together in the frame's order, every component at the unit's size: the frame header gives the whole stream's size, and a unit's is its grid's. Converting their color space is the profile's, as for `jpeg` |
| `jpeg2k` | one JPEG 2000 codestream (ISO/IEC 15444-1), bare or in a JP2 file | none | none | the image's samples, of `item_bits` bits, little-endian, a pixel's components together in the codestream's order |
| `jpegxl` | one JPEG XL codestream (ISO/IEC 18181-1), bare or in its container (18181-2) | none | none | the image's samples, of `item_bits` bits, little-endian, a pixel's channels together |
| `jpegxr` | one JPEG XR file (ITU-T T.832, in its container) | none | none | the image's samples, of `item_bits` bits, little-endian, a pixel's channels together, in red, green, blue order whichever order its pixel format stores ([zarrify/czi.md §3.1](zarrify/czi.md#31-pixel-types-and-codecs)) |
| `webp` | one WebP file | none | none | the image's samples, one byte each, a pixel's red, green and blue together, with a fourth, alpha, where the unit's last axis has four: 255 where the stream holds none |
| `lerc` | one LERC blob | none | none | the blob's values in its own data type, little-endian, a pixel's bands together; a pixel its mask leaves out is as the LERC library decodes it |
| `eer` | one frame of Electron Event Representation: run lengths of `skip_bits` bits, each with a sub-pixel position of `horizontal_bits` and `vertical_bits` bits | `skip_bits`, `horizontal_bits`, `vertical_bits`: each a value, or a place of a field's one value; absent, 7, 2 and 2 | none | elements of 1 bit, 1 where an event lands on the pixel, most significant bit first, each row padded to a whole byte; the sub-pixel positions are not made |
| `grib_scale` | the integers `X` of a GRIB edition 2 field (FM 92 regulation 92.9.4), as the codec after it in its chain makes them: each of `item_bits` bits, 64, two's complement, little-endian | `reference`: a place of `R`, 4 octets, an IEEE binary32, big-endian (section 5's octets 12–15); `binary_scale` and `decimal_scale`: each a place of `E` or of `D`, 2 octets, an integer whose first bit is its sign and whose other 15, the most significant first, are its magnitude (octets 16–17 and 18–19) | none | floats of `item_bits` bits, binary64, little-endian: `(X × 2^E + R) × 10^−D`, computed in binary64, each operation rounded, in that order, `X` and `R` converted first; `2^E` is 1 multiplied by 2 as many times as `E`'s magnitude (divided, for a negative `E`), and `10^−D` is 1 divided by 10 as many times as `D`'s magnitude (multiplied, for a negative `D`), as ecCodes computes them. The integer −2^63, a missing value of `grib_complex`, makes NaN |
| `grib_simple` | the data of FM 92's data template 7.0: the unit's integers, each of `bits` bits, the most significant first, one after another, then the bits that fill the last octet | `bits`: a place of one octet, an unsigned integer, from 1 to 63 (section 5's octet 20) | none | the integers, unsigned, as `grib_scale` takes them |
| `grib_complex` | the data of FM 92's data template 7.2, or, with `differencing`, 7.3, every number the most significant bit first. With `differencing`: as many first values as its order, each an unsigned integer of its descriptors' octets, then the overall minimum of the differences, of as many octets, its first bit its sign and the others its magnitude. Then each group's reference, of `bits` bits; from the next whole octet, each group's coded width; from the next whole octet, each group's scaled length; from the next whole octet, the groups' coded values, each of its group's width, one after another | `bits`: a place of one octet, an unsigned integer, at most 64 (section 5's octet 20); `groups`: a place of section 5's octets 22–47, of data representation template 5.2, of which the codec reads the missing value management (octet 23), the number of groups (32–35), the reference of the groups' widths (36) and the bits of a coded width (37), the reference of the groups' lengths (38–41), their increment (42), the last group's length (43–46) and the bits of a scaled length (47), each an unsigned integer, big-endian; `differencing`: a place of section 5's octets 48–49, of template 5.3: the order of the spatial differencing, 1 or 2, and the octets of each of its descriptors, at most 8 | none | the unit's integers, as `grib_scale` takes them. A group's width is its coded width plus the widths' reference; its length is its scaled length times the increment plus the lengths' reference, and the last group's is the one given. A value is its group's reference plus its coded value. Under missing value management 1 or 2, a coded value of all ones (in a group of width 0, a group's reference of all ones) is a missing value, and under 2 so is one of all ones less 1: each makes −2^63. With `differencing`, the values that are not missing are differences, summed back in order: the first ones, as many as the order, are the first values, and each after them is itself plus the overall minimum plus the value before it (order 1), or plus twice the value before it less the one before that (order 2), as ecCodes reads them |
| `grib_png` | a PNG file ([W3C PNG](https://www.w3.org/TR/png-3/)), as FM 92's data template 7.41 holds one: not interlaced, with no palette, of gray samples of 1, 2, 4, 8 or 16 bits, or of red, green and blue samples of 8 bits, with or without alpha | none | none | the unit's integers, as `grib_scale` takes them: the image's pixels, row after row, each pixel's bits as one unsigned integer, the most significant first (a gray sample; red, green and blue, 24 bits; with alpha after them, 32) |
| `grib_ccsds` | one stream of CCSDS 121.0-B adaptive entropy coding, as [libaec](https://gitlab.dkrz.de/k202009/libaec) writes it and FM 92's data template 7.42 holds it: unsigned samples of `bits` bits, in blocks of `block_size` samples, `reference_sample_interval` blocks to an interval, with libaec's options `flags` | `bits`: a place of one octet, from 1 to 32 (section 5's octet 20); `flags`: a place of one octet, libaec's flags (octet 22: 8, the samples are preprocessed; 16, the restricted set of code options; 32, each interval ends on a whole octet; 2 and 4 say how a decoder lays its samples out, and nothing of the stream); `block_size`: a place of one octet (octet 23); `reference_sample_interval`: a place of 2 octets (octets 24–25); each an unsigned integer, big-endian | none | the unit's integers, the stream's first samples, as `grib_scale` takes them |
| `h264` | one access unit of an H.264 stream (ITU-T H.264), as ISO/IEC 14496-15 stores a sample: its NAL units, each after its length, of as many bytes as `setup` says | `setup`: a place of an AVCDecoderConfigurationRecord (an `avcC` box's payload), which holds the parameter sets; `plane`: a value, 0, 1 or 2, the picture's luma plane or one of its two chroma planes (Cb, then Cr); `sync` | `sync`: `UInt64`, 1 where a decoder may start at the unit, else 0 | plane `plane` of the access unit's picture, at the plane's own size: samples of 1 byte, or, of a bit depth above 8, of 2 bytes, little-endian. Its units are a stream ([§3.1](#31-chains)) |
| `hevc` | one access unit of an HEVC stream (ITU-T H.265), stored as `h264`'s | as `h264`'s, `setup` a place of an HEVCDecoderConfigurationRecord (an `hvcC` box's payload) | as `h264`'s | as `h264`'s |
| `av1` | one temporal unit of an AV1 stream, as [AV1-ISOBMFF](https://aomediacodec.github.io/av1-isobmff/) stores a sample: its OBUs, each with its size | as `h264`'s, `setup` a place of an AV1CodecConfigurationRecord (an `av1C` box's payload) | as `h264`'s | as `h264`'s |
| `vp9` | one frame or superframe of a VP9 stream | as `h264`'s, `setup` a place of a VPCodecConfigurationRecord (a `vpcC` box's payload) | as `h264`'s | as `h264`'s |

A place parameter of an HDF5 filter's codec holds client data values of the filter, as its filter
pipeline message stores them: each 4 bytes, an unsigned integer, little-endian. A place of a TIFF
field's one value holds the field's first value as the file stores it: an unsigned integer of the
field's type (which its `value` element's type gives), in the file's byte order. A place of the
byte order mark holds the file's first two bytes, `II` (little-endian) or `MM` (big-endian). A
place of a JPEG stream's header holds the stream's bytes before its scan's data: from its SOI
through its scan header, with its tables, its frame header and its restart interval between.

`source` is the general fallback: a source encoding refkv has no name for (an unknown HDF5
filter's id and client data). Where it makes the elements, first in every chain of an array, the
IR may not know what one element is: the description then has no `item_bits`, and still says
where each unit is (an MP4 audio track's samples). refkv's names describe the encoding, not the
output: which Zarr or
numcodecs codec stands for one is the profile's. Each format adds its codecs to the list as it
describes its arrays.

## 4. The chunk map

An IR's chunk map is one table with a row for each unit the source holds, of every array with a
grid. A unit the source does not hold has no row, and no row is for an array with no grid.

### 4.1 The Arrow schema

The chunk map crosses the Python binding as one Arrow record batch, by the Arrow PyCapsule
interface (`__arrow_c_array__` and `__arrow_c_stream__`), of this schema:

| Column | Arrow type | Null | Contents |
| --- | --- | --- | --- |
| `array` | `UInt32` | no | the array, by its position in the IR's descriptions |
| `coords` | `LargeList<UInt64>` (items not null) | no | the unit's coordinates in the array's grid (empty for rank 0) |
| `element` | `UInt32` | yes | the IR element holding the unit's bytes: its recipe gives them, or, with `range`, the range of its extent (taken before any recipe) |
| `member` | `UInt64` | yes | where `element` is in a run's subtree, the position j of the run's member it is in |
| `range` | `Struct<offset: UInt64, length: UInt64>` | yes | where the unit is part of `element`: its offset and length within the element's bytes, the same form as a place's `range` ([§2.2](#22-places-and-parameters)) |
| `ranges` | `LargeList<Struct<object: Dictionary<Int32, Utf8>, offset: UInt64, length: UInt64>>` (items not null; `object` null for the parsed source) | yes | where no element holds the unit's bytes: its bytes, in order (a unit in part of an element is `element` and `range`); `offset` counts from the object's start |
| `chain` | `UInt32` | yes | the unit's chain, a position in its array's `chains`; null for the first |
| `skips` | `UInt64` | yes | the codecs of its chain that the unit skips, a mask by codec position (bit i, codec i); null for none |
| `p.<codec name>.<name>` | the codec's entry's | yes | one for every unit parameter a description with a grid lists, whether or not a row gives it a value, after the columns above in order of their names |

The list columns are `LargeList`s, of 64-bit offsets, since a map the budget admits may hold
more than 2^31 coordinates or ranges. The schema's metadata gives `refkv.chunk_map`, the schema's
version, `0`.

### 4.2 Rows

- Rows are grouped by `array`, in order, and within an array are in row-major order of `coords`;
  a parser adds them in any order, and the IR sorts them.
- Exactly one of `element` and `ranges` is set in each row. `range` is set only with `element`,
  where the unit is part of it (a FITS tile within its heap element), is at least one byte long,
  lies within the element's bytes, and, where the element's recipe is the identity
  ([§5.2](#52-the-chunk-map)), is not all of them: such a whole element names the element alone,
  so that a unit has one spelling. With any other recipe the element alone gives the recipe's
  bytes, and a `range` of its whole extent is the one way to name the raw bytes (a TIFF JPEG tile
  whose recipe splices in its tables). A unit inside a derived space is an element or a range of
  one, since `ranges` address only objects. A row's ranges are at least one, each at
  least one byte long, and lie within their objects as a place's do
  ([§2.2](#22-places-and-parameters)).
- Null is the only way to write the first chain and "skips nothing".
- A parameter column is set exactly on the rows whose array lists it in `unit_parameters` for the
  row's chain and that do not skip its codec; null elsewhere.
- A rank-0 array has at most one row.

## 5. Checks

Both checks run when a parser adds its descriptions and chunk map to its finished IR, and in the
`check::finish` of a store after its grafts and unfolds (a file IR's own `check::finish` runs
before its descriptions are built, so has none to check). A description or a row they refuse is a
bug in its parser: an internal error naming the array's element.

They also check the `undescribed` mark ([§6](#6-in-the-ir)): a file IR lists no keys, and one
marked `true` has no descriptions, chains or rows; a store IR is not marked `true` itself, and its
list names its objects, once each, none of which holds a description's key or a row's `element`
(another object's rows may read its bytes).

### 5.1 Each description

- **Its element and places.** Its element, and every element its places, its fill's and its
  chains' parameters name, is an element of the IR; its own element is in no run's subtree (an
  unfold would leave the key behind); a place's `member` is set exactly when its element is in a
  run's subtree, and is below the run's count; an object key is any key or none in a file IR (an
  external file's key is checked when the file is grafted), and in a store IR is never none and
  names one of the store's objects; a place lies within what it names
  ([§2.2](#22-places-and-parameters)), its end below 2^64.
- **No extent.** A description without a shape has no grid, no `axes` and no `pad_from`.
- **The grid.** Its rank is the array's rank, and it is in canonical form
  ([§2.3](#23-the-grid)).
- **Chains.** At least one when `grid` is set, none when it is not; each a position in the chain
  table, and none twice. The chain table holds no chain twice, and every codec of it is one of the
  list ([§3.2](#32-the-list)), with only its entry's parameters.
- **`axes`.** When given, a permutation of the rank other than the identity.
- **`item_bits`, `pad_from` and the fill.** `item_bits` is set when the fill is given, and when
  `grid` is, unless every chain of the array begins with `source`; it is at least 1. `pad_from` is below the rank, given only when `item_bits` is set and
  is not a multiple of 8. A given fill value is ceil(`item_bits` / 8) bytes: a base64 string of
  that many, or a place of that length (a range's or an extent's length, else its element's).
- **`unit_parameters`.** None is listed twice. Each names a chain of the description, a codec of
  that chain that appears once there, a parameter the codec's entry allows as a column, and no
  parameter the codec gives itself.
- **Keys.** `(element, index)` is unique among the IR's descriptions, and on each element `index`
  is either absent from every description or set on every one, as 0 to n − 1.

### 5.2 The chunk map

- its columns match the schema: each of the IR's length, the list offsets their values', and each
  parameter column one whose name is `p.<codec name>.<name>` of an entry of the list and a
  parameter the entry allows as a column, of the entry's type;
- rows are only for arrays the IR describes that have a grid;
- coordinates are of the array's rank, inside its grid, grouped and ordered as
  [§4.2](#42-rows) says, with no repeats;
- exactly one of `element` and `ranges` is set in each row, and a row's ranges are at least one,
  each at least one byte long;
- `member` is set exactly when `element` is set and is in a run's subtree, and is below the
  run's count;
- `range` is set only with `element`, at least one byte long, its end within the element's bytes
  and below 2^64, and not the element's whole extent where the element's recipe is the identity:
  the element has no form, or its form no recipe (`"recipe": null` is none), or a recipe, by the
  grammar of [conventions §8.5](conventions.md#85-forms-and-recipes), of only `["src", o, n]`
  parts (`n` null or left out: to the element's end) and `["rows", o, s, n, c]` parts that, read
  as ranges of the element's extent, cover all of it in order with no gap or overlap
  (`[["src", 0]]`, `[["src", 0, null]]`, `src` parts end to end, `rows` runs of `n` bytes each `n`
  apart, or a single run of any stride, that make up its length). A part of no bytes covers none,
  and is skipped before its offset and stride are looked at; a part with fields the grammar does
  not give it, or a `src` part of null length whose offset is past the element's end, is not the
  identity;
- the first chain and no skips are written as null;
- `chain` is a position in the array's `chains`, and `skips` names only codecs of the unit's
  chain;
- each parameter column is set exactly on the rows whose array lists it for the row's chain and
  that do not skip its codec;
- every `element` is an element of the IR;
- every range is checked as a place's is ([§5.1](#51-each-description)): its key, and its bounds.

## 6. In the IR

**Built at finish.** A parser keeps what it learns about its arrays to itself while it reads.
When its IR is finished (after its last fold and rollback, and after `check::finish`), it builds a
description for each array it reaches and the chunk map's rows from the finished IR, and adds
them to the IR, which, in order: fits the map to its limit (below); drops from the chain table the
chains no description names, keeping the others' order, and renumbers the descriptions' positions
in it; fits the descriptions to their limit (below), which falls back the same way and drops the
chains it leaves unnamed; sorts the rows; and checks them ([§5](#5-checks)). They are
not in the facts a projection reads, which keep their arrays until each format's projection reads
the descriptions.

**Only two things change ids afterwards.**

- **A graft** carries a file IR's descriptions and rows into the store it joins, in order:
  - it fits the file's arrays, in the file's order, to the store's chunk map limit as it stands
    then (below), which counts every object the store has listed, grafted or not; a graft does not
    grow it. A store has the 2^22 once, where each file had it, so an array that kept its rows in
    its file may fall back in the store, depending on what was grafted before it;
  - it carries into the store's chain table only the chains the file's remaining descriptions
    name, each with its places' element ids renumbered and then found in the table by its value:
    a chain the table has keeps its position, and a new one is appended, in the order the file's
    descriptions first name them. The table's existing chains and positions are untouched;
  - it fits the file's descriptions, last, to what remains of the store's descriptions' limit, as
    describe does (below): where even their fallen-back descriptions do not fit, the file joins the
    store with none, its key added to the store's `undescribed` list, as is the key of a file
    marked `undescribed` at its own describe. A graft that fails on the IR's room adds nothing to
    the store.

  `element`, every place's element and the `element` column take their new ids; `array` takes the
  description's new position, after the store's; parameter columns of one name join; and the
  parsed source's null key, in every place and in `ranges`, becomes the file's key in the store.
- **An unfold** of a run (a store's own `check::finish` may unfold one, as may the mirror's
  canonical form) gives its members after the first new ids: each row's and place's element in
  member j's subtree moves to member j's copy, and `member` is cleared on every element that was
  in the run.

The mirror's canonical form ([conventions §8.8](conventions.md#88-canonical-form)) leaves the
descriptions and the chunk map out.

**Limits.** The IR's budget counts the descriptions' entries and the chunk map's in fields of its
own, each against a limit of its own, never against the IR's room (its records and elements). The
descriptions' entries are each description, each axis of its shape, each run of a listed axis (an
`Each` axis has none), each of its places (its declarations, and a fill given at a place), each of
its chains and each unit parameter; and of the chain table, each chain, each codec and each place
a codec's parameter names. The chunk map's are each row, each of its coordinates, each of its
`ranges` items and each unit-parameter value it sets (a row's `range` is part of it, and is none).
The descriptions' limit is 2^22 once, plus one per 4 bytes of the source, growing with each object
of a store as the store's other budgets do (4 plus one per 4 of its bytes). The chunk map's is 2^22
once, plus four per byte of the source; a store's grows by 4 plus four per byte with each object
the store lists, as its records do, and not when a file is grafted. Every unit in space 0 has at
least one byte of its own (an HDF5 chunk of an implicit index may be a single byte), or an index
entry of several, so where no two units share bytes or index entries, a map fits where each row's
rank, plus the unit-parameter values it sets, is at most 3 for a unit held by an element of space 0
(with or without a `range`), or at most 2 for a unit in `ranges` into the parsed source. Nothing
else has that guarantee: a unit in a derived space can cost far less than a byte of the source; a
unit in `ranges` into another object (an HDF5 external file) costs entries only the parsed source's
size pays for, so its array can fall back at any rank; and a source whose structures share bytes or
index entries (two HDF5 object headers on one chunk index, TIFF IFDs sharing a StripOffsets array)
can pass the limit.

The chunk map's limit never rejects a run, and never cuts an array's rows short, which a profile
would read as units the source does not hold. Arrays are tried in the order of the IR's
descriptions: a file IR's, and a store parser's own, are in `(element, index)` order, which describe
sorts them into; in a store, each graft's arrays
keep their relative order and follow those already there, so a store's arrays are in graft order
(not in key order: a description keyed by a file's root is keyed, in the store, by the file's
object element, whose id can be below the earlier grafts' ids). One whose rows do not fit in what
remains of the limit keeps none of them, and falls back: its description has no grid, no chains
and no unit parameters, and the map neither a row of it, nor a parameter column only its
description listed, nor an object key only its rows named. Later arrays are still tried, and the
run goes on. In a file IR, which arrays fall back follows from the source alone; in a store,
from its files in graft order.

The descriptions' limit never rejects a run either. Where the descriptions and the chain table pass
what remains of it, two passes follow the map's fit. First every array is charged its fallen-back
description's cost; where that total passes the limit, the source keeps no descriptions, chains or
rows at all and is marked `undescribed` ([§2.5](#25-the-json-form)), which a profile reads as a
source to take as its bytes, never as one with no arrays. Otherwise, in the same order, each array
keeps its full description (its grid, chains and unit parameters, and the chains it is the first
kept description to name) while what remains allows, and falls back as at the map's limit
otherwise; an array that fell back at the map's limit stays fallen back. A fallen-back array's rows
and the columns and chains only it named are dropped, so a later array that names a chain it shared
pays for that chain. A store parser that describes its own objects' arrays itself, not by graft,
keys each of them in one of its objects, and where it keeps none lists the key of every object whose
arrays it described.

As the map is built, a row holds about 93 bytes of memory, a coordinate 8 and a `ranges` item 24;
the worst case, rank-1 rows of elements over a shared index at four entries per byte, is about 200
bytes per byte of the source. A parameter column holds about 16 bytes a row, null or not, for every
row of the map; the list allows few columns (a video codec's `sync`). Descriptions a check refuses, and those a
fallback drops, spend none of either limit.

**Bindings.** Python's `refkv._core.Ir` gives the descriptions as the JSON document of
[§2.5](#25-the-json-form) (`stored_arrays()`, read by `refkv.ir.stored`) and the chunk map as an
object of the Arrow PyCapsule interface (`chunk_map()`); pyarrow, polars or nanoarrow read it
without a copy, and refkv itself depends on none of them. The WebAssembly module carries
neither, and compiles no Arrow code.

## 7. The formats

**NumPy** ([spec/zarrify/npy.md](zarrify/npy.md)). A `.npy` file's array, and each stored
(method 0) `.npz` member's whose header was read, is a stored array, keyed by its `header` element,
with no index; that element is its one declaration (its `descr`, `fortran_order` and `shape`). The
facts give its key: `declared_by` is the id of a `.npy` file's `header` element (a list of one),
or, for a `.npz` archive, a list of one id per member of the facts' `own.members` in their order
(the archive's central directory's), presented or not: its `header` element's
(`entries/<i>/data/header`), or null for a member with no description (a deflated one, or one
whose header was not read). A presented member's entry of the facts' `arrays` is the member of
`own.members` with its `path`.
`shape` is the header's, reversed for a Fortran-order array of two or more axes, whose `axes` then
give the header's order; `item_bits` is eight times the size of an element of its `descr` as NumPy
lays it out, whether or not the profile presents the array: a type string (with its one-character
codes, such as `<h` or `?`; a subarray, `(2,3)<f4`; fields joined by commas, `<f8,<i4`) or a
structure (with its padding fields, fields of shape, fields whose type is a (type, shape) tuple, and
fields of no bytes, such as `|S0`, which add none). It is absent for a `descr` of no size: an object
array's; one of 0 bytes; one whose size is the machine's (`l`, `L`, `g`, `G`, `p`, `P`), which a
header does not give; and a structure with two fields of one name, which NumPy refuses. A presented
array is one unit, its `data` element, in an empty chain: each axis is `Each` of its length, or an
empty `List` for an axis of length 0 (which has no unit, and no row), under `short`. NumPy defines
no fill value: `undefined`. An array whose data is not presented (its `descr` has no data type, or
its data does not fit) has no grid, nor, by the general fallback ([§1](#1-stored-arrays)), has one
whose element's bits would pass 2^64 (an array of no elements may be of such a type), whose
`item_bits` is then absent too. A deflated member's array is not
described yet: its header and data lie in its stream, which no element holds.

**HDF5** ([spec/zarrify/hdf5.md](zarrify/hdf5.md)). Every object the parser reads whose header has
a data layout message, and neither a symbol table nor a link info message, is a stored array,
whether or not a path reaches it or the profile presents it: a dataset. It is keyed by its
object's element (`objects/<i>`), with no index. The facts give the key: `array.declared_by` of
each node that is an array is the id of its dataset's object, which the store's `placed()`
renumbers with `store.element` ([§2.1](#21-keys)). An attribute's value is not described yet.

- **Declarations.** The first is the data of the dataset's datatype message, or, where that
  message is shared, of the committed datatype's datatype message it names; a header with no one
  datatype message gives none. Of a dataset the parser did not read, it is the shared message's
  own data: the description reads no byte the parser did not, so it is the same however the
  file's bytes were fetched. After it come the fill value messages whose value the description
  cannot give (below). A message is the element the IR made of its data (`messages/<k>/data`),
  or, where the IR had no room for one, a range of the source.
- **`shape`** is the dataspace's current dimensions, absent for a null dataspace and for one that
  does not read. `axes` is absent: HDF5 stores an array in its declared order.
- **`item_bits`** is eight times the datatype's size, the bytes of one stored element (for a
  variable-length type, of its heap reference); absent where the datatype does not read or its
  size is 0, and for a dataset the parser did not read whose datatype message is shared.
- **A chunked dataset** whose chunk index and filter pipeline the parser read has the grid of its
  chunk shape, `Each` on every axis, under `full`, and one chain: its filter pipeline's codecs
  ([§3.2](#32-the-list)), in the pipeline's order. A filter the list has no name for, or whose
  client data are fewer than its entry's parameters, is `source`, with `format` `hdf5`, its `id`,
  its `name` where the message gives one, and `parameters`, a place of all its client data,
  where it has any. Each chunk the index holds is a row: its element; or, where the chunks of
  an implicit index are one `data` element, its range of that element (no range where the chunk
  is all of the element); or, where the IR had no room for its element, its range of the source.
  `skips` is the chunk's filter mask, less the bits past the pipeline; an edge chunk the dataset
  stores unfiltered skips every codec. A chunk the index does not hold has no row.
- **A contiguous or compact dataset** is one unit in an empty chain: each axis `Each` of its
  length, or an empty `List` for an axis of length 0, under `short`. The row's bytes are the
  dataset's `data` element, or the range of its layout message's data that holds a compact
  dataset's (a range of the source where the IR had no room for the element); a dataset of no
  bytes, and a contiguous one with no address, has no row. A dataset whose stored bytes are not
  as many as its elements', and a contiguous one whose data runs past the end of the file, has no
  grid.
- **A dataset in external files** is one unit likewise, its row's `ranges` the files' ranges in
  order, each by the sibling key the run resolved for the file. It has a grid only where a store
  run found every file it reads, each as long as its part; in a file run, which looks for no
  sibling, it has none.
- **No grid** also for a virtual dataset, a null dataspace, a chunked dataset whose index the
  parser did not read, and a dataset of which it did not read the dataspace, the datatype, the
  layout, the filter pipeline message or the fill value message.
- **The fill value** has HDF5's three states, as the library's `H5Pfill_value_defined` gives them
  for the dataset: `undefined` (the message says so, or the header has no fill value message of
  either type), `format_default` (a fill value is defined and the message holds none: zeros), or
  `given`, the place of the value's bytes in the message, which are an element as the datatype
  stores one. The new message (type 5) is read where a header has both. A shared message,
  several messages of the type read, a message that does not read, or a value whose length is
  not the datatype's size, leaves `undefined`, with those messages among the declarations.

**NRRD** ([spec/zarrify/nrrd.md](zarrify/nrrd.md)). A NRRD input is one stored array, the one its
header declares, in the store IR the profile's run builds of the header file and, for a detached
header, its data file. It is keyed by the header file's object (the header has no element of its
own), with no index. The facts give the key: `array.declared_by` is the id of that object. Below,
the NRRD definition is Teem's, which
[zarrify/nrrd.md](zarrify/nrrd.md) cites.

- **Declarations.** The line of the `type` field, then the line of the `endian` field where the
  header has one: each the element `lines/<i>`, which holds the field's identifier, its descriptor
  and its line end.
- **`shape`** is the values of the `sizes` field, reversed: the data holds the header's last axis
  outermost. `axes` gives the header's order, the storage axes reversed (`[2, 1, 0]` for three
  axes).
- **`item_bits`** is eight times the bytes of one element of the `type` field's type
  ([zarrify/nrrd.md §3](zarrify/nrrd.md#3-data-types-and-encodings)).
- **The grid** is one unit of the whole array: each axis `Each` of its size, under `short`. Its
  row is the `data` element of the data's file, whole: the header file for an attached header, and
  for a detached one the data file, another object of the store. For the raw encoding the element
  is the array's bytes, after the lines and bytes that `line skip` and `byte skip` pass over, or,
  with a `byte skip` of −1, those that end the file; bytes of the file after them are no part of
  the unit, as the NRRD definition says (pynrrd 1.1.3, which reads a file it is given by its path
  to its end, refuses one that has any). For the gzip encoding the element is the stream: from
  after the lines `line skip` passes over to the end of the file.
- **The chain** is one: empty for the raw encoding; `gzip` for the gzip encoding; and `skip`, then
  `gzip`, for the gzip encoding with a `byte skip` above 0, which the NRRD definition counts within
  the inflated bytes. `skip`'s `length` is the descriptor of the `byte skip` field, a range of the
  field's line. (pynrrd 1.1.3 skips those inflated bytes too, but first seeks as many bytes into
  the file, so it reads no such file that follows the definition.)
- **The stream is not inflated**, as the profile reads none of the data: the description is the
  same whatever the stream holds, and one that inflates to other than the skipped bytes and the
  array's is not detected. The array's bytes are the first the chain makes: the NRRD definition
  leaves what follows the data unread. (pynrrd 1.1.3 refuses a stream that inflates to more, and
  inflates the first member alone of a stream of several, which the `gunzip` program the NRRD
  definition names reads whole.)
- **No grid** for no input: the profile rejects what it does not read (the text, hex and bzip2
  encodings, the type `block`, several data files), and builds no IR of it.
- **The fill value** is `undefined`: NRRD has none.

**TIFF** ([spec/zarrify/tiff.md](zarrify/tiff.md)). Every IFD the parser reads (of the main
chain, a SubIFD, or one another pointer tag leads to) whose ImageWidth (256) and ImageLength (257)
are unsigned values it holds declares an image, a stored array, whether or not the profile presents
it, keyed by the IFD's element, with no index. The planes of an OME-TIFF and the levels of a
pyramid are each an array: how they assemble is the profile's. The facts give the keys: each
level's `declared_by` lists, in the order of its `planes`, the id of each plane's IFD, null for a
plane no file of the run holds, and a set's store renumbers them as it does `planes`
([§2.1](#21-keys)). Below, a field's value is the first of its values, and a field the IFD lacks
has TIFF 6.0's default.

- **Declarations.** The header's `byte_order`, then the `value` element of each of these fields
  the IFD has with a value inside the file: BitsPerSample (258), PhotometricInterpretation (262),
  ColorMap (320), ExtraSamples (338), SampleFormat (339), YCbCrCoefficients (529),
  YCbCrSubSampling (530) and ReferenceBlackWhite (532).
- **`shape`** is ImageLength, then ImageWidth; with SamplesPerPixel (277) other than 1, the
  samples come last, or first where PlanarConfiguration (284) is 2. An ImageDepth (32997) other
  than 1 comes before ImageLength. `axes` is absent: TIFF names its dimensions by tag, and lists
  them in no order.
- **`item_bits`** is BitsPerSample, where every sample has the same; absent otherwise. Where it
  is not a multiple of 8, `pad_from` is the columns' axis (1, or 2 with the samples first): a
  unit's rows each end on a whole byte.
- **The grid.** An IFD with TileWidth (322) and TileLength (323) is cut into tiles, `Each` of
  TileLength and of TileWidth, under `full`, as libtiff and tifffile read it; its tables are
  TileOffsets (324) and TileByteCounts (325), or, where it has not both, StripOffsets (273) and
  StripByteCounts (279). Another is cut into strips, `Each` of RowsPerStrip (278), at most
  ImageLength, and of ImageWidth, under `short`, its tables the strips'. Interleaved samples are a
  last axis of one unit; with the samples first, each is a unit of its own, and the tables list
  the first sample's units, then the next's. Each entry of the tables that holds bytes is a row,
  its element the entry's `data` element (`tiles/<j>` or `strips/<j>`, of the IFD the tables are
  first met in); a JPEG tile or strip whose element's recipe is not the identity (it splices in a
  marker, or the tables) is that element with a `range` of all its extent ([§4.2](#42-rows)). An
  entry of no bytes at offset 0, as a sparse file's writer leaves a unit out, has no row.
- **The chain** is one, by Compression (259): empty for 1; `tiff_lzw` for 5, `zlib` for 8 and
  32946, `xz` for 34925 and `zstd` for 50000, each after `tiff_predictor` where Predictor (317) is
  2 or 3; `packbits` for 32773; `ccitt_t6` for 4 (one sample of 1 bit); `jpeg` for 7 (samples of 8
  bits), with `tables` where the IFD has JPEGTables (347); `jpeg2k` for 33003, 33004, 33005 and
  34712 (8 or 16 bits); `jpegxl` for 50002 and 52546 (8, 16 or 32 bits); `webp` for 50001 (8
  bits); `lerc` for 34887
  (8, 16, 32 or 64 bits), before `zlib` or `zstd` where the second value of LercParameters (50674)
  is 1 or 2; `eer` for 65000 and 65001 (one sample of 1 bit), with their fixed bit counts as
  values, and for 65002, with the places of those of the fields 65007, 65008 and 65009 the IFD has
  with unsigned values. The streams of `jpeg`, `jpeg2k`, `jpegxl`, `webp` and `eer` use no
  predictor, and libtiff and tifffile pass a Predictor over under them, as the chain does. Any
  other compression, one of these with samples of another size, and LERC whose LercParameters are
  not unsigned integers with a second value of 0, 1 or 2, is `source` with `format` `tiff` and
  `id` the Compression: the profile then reads the IFD for the rest.
- **No grid** for an image the IR does not model: BitsPerSample that differ; FillOrder (266)
  other than 1; a Predictor other than 1 under a compression that is neither LZW, Deflate, LZMA
  or zstd nor one of those five, such as none, PackBits, CCITT and LERC (libtiff applies a
  predictor only within those four coders, and tifffile applies one to whatever it does not
  decode as an image, so readers disagree); under those four, a Predictor other than 1, 2 and 3,
  Predictor 2 with samples that are not integers of 8, 16, 32 or 64 bits, and Predictor 3 with
  samples that are not floats of 32 or 64 bits (by SampleFormat, which every sample must share);
  PhotometricInterpretation 6
  with a YCbCrSubSampling other than (1, 1) under a compression that restores the stored bytes (1,
  5, 8, 32773, 32946, 34925, 50000), whose samples are then not an array of the image's shape; an
  ImageDepth other than 1; a PlanarConfiguration other than 1 and 2 with more than one sample; a
  width, a length, a sample count, a tile size or a RowsPerStrip of 0; an IFD without both of its
  units' tables, as unsigned values the parser holds; tables that do not list as many units as
  the grid has, or that differ in length; an entry of no bytes at an offset other than 0, which
  readers do not agree on (libtiff sizes a lone strip from the image and refuses one among
  others, and tifffile reads it as absent); and an entry whose bytes do not lie in the file, so
  that a file cut short does not read as an image with units its source does not hold.
- **More bytes than the unit's.** A tile or strip may hold, or decode to, more bytes than its
  unit's elements take (a tile stored at a larger sample size than its fields give): the unit's
  are the first, as libtiff and tifffile read them.
- **The fill value** is `undefined`: TIFF has none.

**GRIB** ([spec/zarrify/grib.md](zarrify/grib.md)). Every message of a GRIB edition 2 file has
descriptions, whether or not the profile presents its fields, keyed by its element
(`messages/<i>`), a kind of element that may declare several arrays: each bitmap one of its
sections 6 holds (indicator 0) and each of its fields (a section 7, with the sections 3, 5 and 6
in effect at it) is an array, indexed in the order of their sections, a bitmap before its field.
The file's store parser describes them itself ([§6](#6-in-the-ir)). A field's array is the values
its section 7 codes, in the file's order: how fields stack into a variable, where on its grid a
bitmap puts a field's values, and which way a grid's rows run are the profile's. The facts give
the keys: each entry of `fields` has `declared_by`, the id of its message's element, `array`, the
index of its values' array, and `mask`, the index of the bitmap that says which points its values
are (its own, or, under indicator 254, the message's last one before it), or null. Below, an octet
is numbered from 1 in its section, a section is the element `messages/<i>/sections/<j>`, and `n`
is section 5's number of values (octets 6–9).

- **Declarations.** A field's: its section 3 (the grid's number of points, its template, `Ni`,
  `Nj` and the scanning mode); its section 5 (`n`, the data representation template and its
  octets: the reference value and the scale factors, the type of the original values, complex
  packing's substitutes for its missing values); the first 6 octets of its section 6, which end
  with the bitmap indicator (the section's element, or the `header` element of a section that
  holds more); and, where a bitmap says which points its values are, that bitmap's bits (the
  `bitmap` element of its section 6). A bitmap's: the section 3 in effect, and the `header`
  element of its section 6.
- **A bitmap** is a bit for each point of the grid, in the file's order, 1 where the point has a
  value: `shape` is section 3's number of points (octets 7–10), one axis, and `item_bits` is 1. It
  is one unit in an empty chain, `Each` of the points (an empty `List` for none) under `short`,
  and its row is the section's `bitmap` element, the octets after the first 6, or the range of its
  first octets that hold the points' bits where it holds more. A bitmap whose section holds fewer
  bits than the grid has points has no grid.
- **A field's `shape`.** Where every point has a value (indicator 255, and `n` the grid's number
  of points) and the grid is one whose rows the parser reads (template 3.0, 3.1, 3.10, 3.20, 3.30
  or 3.40, defined by its template, with no list of the points of each row, and `Ni` × `Nj` its
  number of points), `shape` is `Nj`, then `Ni`, with `axes` `[1, 0]`, since section 3 gives `Ni`
  first; or, where the scanning mode's bit 3 (`0x20`) says that consecutive points run along a
  column, `Ni`, then `Nj`, with no `axes`. Rows that alternate in direction (bit 4, `0x10`) are as
  the file stores them. Every other field is one axis of its `n` coded values, in order: one on a
  reduced grid, on a 3.101 grid or on a grid the parser does not read; one whose `n` is not the
  grid's number of points; and one with a bitmap, of any indicator. A field with a bitmap is the
  values of the points the bitmap keeps, and no value is made for a point it leaves out: the
  bitmap's own array, or for a predefined bitmap (indicator 1–253) the indicator, says which they
  are.
- **Units.** A field is one unit: each axis `Each` of its length, or an empty `List` for an axis
  of length 0, under `short`. Its row is its section 7's element with the range of the octets
  that hold its values, which start after the section's first 5; a field of no values, and one
  whose values no octet codes, has no row.
- **IEEE floats** (template 5.4, of the precision 1 or 2 at octet 12) are stored as they are:
  `item_bits` is 32 or 64, the elements big-endian, the chain empty, and the row's range the
  `n` values' octets.
- **A constant field** codes no value: simple packing, JPEG 2000, PNG or CCSDS packing (5.0, 5.40,
  5.41, 5.42) of a bit width of 0 (octet 20), and complex packing (5.2, 5.3) of no groups (octets
  32–35). Every value is the reference value `R`, an IEEE binary32, big-endian: `item_bits` is
  32, the chain is empty, there is no row, and `fill` is given, the place of `R` (section 5's
  octets 12–15). ecCodes and NCEP's g2c read `R` itself whatever the decimal scale factor `D` is,
  as their encoders write it, where FM 92's regulation 92.9.4 would give `R × 10^−D`.
- **A packed field** is the integers its packing codes, which `grib_scale` makes its values of:
  `item_bits` is 64, and the chain is `grib_scale`, with the places of `R`, `E` and `D` in section
  5, then the codec of the integers, each parameter a range of section 5
  ([§3.2](#32-the-list)): `grib_simple` for 5.0 (a bit width of at most 63), the row's range the
  octets that hold `n` integers; `grib_complex` for 5.2 and for 5.3, with `differencing` (missing
  value management 0, 1 or 2; widths of at most 64 bits; an order of 1 or 2, and descriptors of at
  most 8 octets); `jpeg2k` for 5.40, whose codestream's samples, in the image's order whatever its
  rows, are the unit's integers; `grib_png` for 5.41; and `grib_ccsds` for 5.42 (unsigned samples
  of at most 32 bits). The row's range, but simple packing's, is all of section 7 after its first
  5 octets. The parser reads no octet of section 7 after them, so a stream that is not its
  codec's, or holds other than `n` integers, is not detected. Complex packing's missing values are
  NaN, whichever of its two kinds they are; their substitutes (section 5's octets 24–31) are among
  the declarations.
- **`source`**, with `format` `grib`, `id` the data representation template's number and
  `parameters` the place of section 5's octets after it (from octet 12), for any other template
  (the spectral 5.50 and 5.51, 5.61, 5.200), and for one of those above with parameters its codec
  does not take (IEEE floats of another precision, simple packing of 64 bits, complex packing of
  other options, signed CCSDS samples, a section 5 shorter than its template's octets). The field
  then has no `item_bits`, and its row's range is section 7 after its first 5 octets.
- **No grid** for a field whose section 7 holds fewer octets than its IEEE floats or its simple
  packing's integers take, and for one of another named packing with values to code and a section
  7 of 5 octets.
- **The fill value** is `undefined`, but a constant field's: GRIB has none.

**NIfTI** ([spec/zarrify/nifti.md](zarrify/nifti.md)). A NIfTI file's voxels are one stored
array, of a single file, of a file gzipped whole and of a header-and-image pair alike, as nibabel
reads them. The facts give its key: `declared_by` is the id of the element that declares it. Below,
`n` is the header's `dim[0]`, and `v` its `vox_offset`.

- **A single file's array** is keyed by its `header` element, with no index. That element is its
  one declaration: the header, whose `sizeof_hdr` gives the file's byte order, with its `dim`, its
  `datatype` and `bitpix`, and what else a profile reads of the voxels (`scl_slope`, `scl_inter`).
- **`shape`** is `dim[n]` to `dim[1]`: the header lists its dimensions the fastest-varying first,
  so `axes` is `n − 1` down to 0 for an `n` of 2 or more, and absent for 1. Each of the
  `n` dimensions is an axis, the sixth and the seventh too, which the parser accepts only at 1;
  the values of `dim` past `dim[n]` are not read.
- **`item_bits`** is the bits of a voxel of the header's `datatype`, which `bitpix` repeats (the
  parser refuses a header where the two differ). A voxel is one element: an RGB24 or RGBA32 voxel
  of 24 or 32 bits, a complex one of 64 or 128.
- **The grid** is one unit: every axis `Each` of its length, under `short`. Its row is the
  `voxels` element, the voxels' bytes from `v`, in an empty chain.
- **A pair's array** is the single file's, described by the pair's store parser itself
  ([§6](#6-in-the-ir)): its key and its declaration are the `header` element of the header file,
  and its row the `voxels` element of the image file, where `v` counts.
- **A gzipped file's array** has the single file's `shape`, `axes`, `item_bits` and grid, but its
  header lies in its stream, where no element holds it. It is keyed by the file's one element,
  `voxels`, the whole file, and has no declarations: a profile reads the header from the facts'
  `gzip.head`, the stream's first bytes as the parser inflated them. Its row is that element, in
  the chain `skip`, `gzip`: the stream inflates to the header, the extender and the extensions,
  then the voxels, and `skip`'s `length` is `v`, a value, since no place holds the field. The
  parser inflates the start of the stream alone, and accepts the file only where the size its
  trailer records is `v` plus the voxels' bytes, modulo 2^32.
- **No grid** for no file the parser accepts: it refuses one whose voxels it does not read as one
  array (a `datatype` it has no size for, a dimension below 1, a sixth or seventh dimension above
  1, voxels the file does not hold).
- **The fill value** is `undefined`: NIfTI has none.

**QPTIFF** ([spec/zarrify/qptiff.md](zarrify/qptiff.md)). A QPTIFF is read as a TIFF, and its
IFDs' images are described as a TIFF's, by the same code; its facts' levels give `declared_by`
likewise.

**NDPI** ([spec/zarrify/ndpi.md](zarrify/ndpi.md)). An NDPI is read by the TIFF parser in NDPI's
layout, and every IFD that declares a size is described as a TIFF's, keyed likewise, the macro
image and the slide map too, with the differences below. The facts give the keys: each level's
`declared_by` lists, in the order of its `planes`, the id of each plane's IFD.

- **A JPEG strip that McuStarts cuts.** An image of Compression 7 in one strip (interleaved
  samples of 8 bits, with neither JPEGTables nor a tile size), with McuStarts (65426), whose
  strip's header the parser read ([zarrify/ndpi.md
  §7](zarrify/ndpi.md#7-ndpi-profile) reads those of the IFDs of positive Magnification), is cut
  into the strip's restart intervals, as tifffile reads it. The grid is `Each` of the MCU's
  height, `Each` of an interval's width (its MCUs times the MCU's width) and `Each` of the
  samples, under `full`. Each interval is a row: the strip's element, with the `range` from the
  interval's McuStarts value (with McuStartsHighBytes) to 2 bytes before the next one's, or
  before the strip's end, which leaves out the restart marker or EOI after it. The chain is
  `jpeg_restart_interval`, its `header` the range of the strip's element before the first
  interval.
- **A JPEG strip read whole.** The image is one strip in a `jpeg` chain, as a TIFF's, without
  McuStarts, and also where the parser did not read the strip's header (an IFD of no positive
  Magnification), where the header is not one the profile reads, where its frame's components
  are not the image's samples, where McuStarts and McuStartsHighBytes do not fit one another,
  and where McuStarts are not the offsets of the intervals the image's size needs: as many,
  increasing, within the strip, each interval of at least one byte. A strip's frame may give
  another size than the image's fields ([zarrify/ndpi.md §4](zarrify/ndpi.md#4-the-image)): the
  description gives the fields', and the stream decodes to the frame's.
- **JPEG XR** (Compression 22610). The fields do not say what the streams hold: Hamamatsu
  writes 8-bit YCbCr of 3 samples whatever they are ([zarrify/ndpi.md
  §3](zarrify/ndpi.md#3-levels)). Where the parser read the header of the image's first stream
  (its first tile that holds bytes, or its strip), its pixel format is one the profile reads and
  its size is the unit's, the image's samples are the stream's: `shape` has the pixel format's
  samples last, or none for one sample; `item_bits` is the pixel format's, 8 or 16; and the
  chain is `jpegxr`. The declarations are the fields' all the same. Any other JPEG XR image
  (an IFD of no positive Magnification, whose stream the parser does not read) is as its fields
  say, in a `source` chain.
- **`source`** has `format` `ndpi` in an NDPI.

**IMS** ([spec/zarrify/ims.md](zarrify/ims.md)). An Imaris file's datasets are described as
HDF5's, by the same code, in a file run; a filter the list has no name for is `source` with
`format` `hdf5` there too. The facts give each image dataset's key as the sixth
member of its entry of `images`: the id of its object.

**MP4** ([spec/zarrify/mp4.md](zarrify/mp4.md)). Every track whose handler is `vide` or `soun` has
descriptions, whether or not the profile zarrifies it, keyed by its `trak` box, a kind of element
that may declare several arrays: each of a track's has an index. The facts give the key: each such
track's `declared_by` is the id of its `trak` box. A track's samples are its tables', then its
track fragments', in decode order ([zarrify/mp4.md §3](zarrify/mp4.md#3-tracks-and-samples)), each
the element `samples/<T>/<j>`.

- **Declarations.** The `fields` value of the track's first sample entry (its `data` value, of a
  sample entry the convention does not parse), then the `data` value of each of its child boxes,
  in order: a video codec's configuration is among them. A track with no sample entry has none.
- **A video track of H.264, HEVC, AV1 or VP9** (the sample entries `avc1`; `hvc1` and `hev1`;
  `av01`; `vp09`), of one sample description, whose configuration box (`avcC`, `hvcC`, `av1C`,
  `vpcC`) the parser reads as pictures of three planes of one bit depth, from 8 to 16 (4:2:0,
  4:2:2 or 4:4:4), has three arrays, the planes of its pictures: index 0 the luma plane, 1 and 2
  the chroma planes.
  - `shape` is the samples, then the plane's height and width: the sample entry's `height` and
    `width` for the luma plane, and for a chroma plane those halved, rounded up, as the format
    subsamples (both for 4:2:0, the width for 4:2:2, neither for 4:4:4). The first axis is the
    samples in decode order, not the pictures in presentation order: when a sample is presented
    is the profile's, from the track's tables. A stream whose pictures are of another size than
    the sample entry's decodes to the stream's.
  - `item_bits` is 8 for a bit depth of 8, else 16.
  - The grid is a sample a unit: `Each(1)`, then `Each` of the plane's height and of its width,
    under `short`.
  - The chain is the codec (`h264`, `hevc`, `av1` or `vp9`): its `setup` the configuration box's
    `data` value, its `plane` the array's index, and `sync` a unit parameter, 1 for a sync sample
    ([zarrify/mp4.md §3](zarrify/mp4.md#3-tracks-and-samples)) and 0 for any other.
  - Each sample is a row of each of the three arrays: its element.
- **A Motion JPEG track** (`jpeg`, `mjpa`, `dmb1`, or `mp4v` of object type indication 0x6C), of
  one sample description, whose first sample's frame header the parser read, of the sample entry's
  width and height, 8 bits and 1 or 3 components, has one array, index 0: `shape` the samples,
  the height, the width and, for 3 components, the components; `item_bits` 8; a sample a unit, in
  decode order, in a `jpeg` chain with no `tables`.
- **Any other track of one sample description** has one array, index 0, of its samples as the
  sample entry encodes them, by the general fallback ([§1](#1-stored-arrays)): an audio track,
  whose decoded samples are not described yet; a video track of another codec, of a sample entry
  of width or height 0, or whose configuration the parser does not read as above (pictures of one
  plane, or of planes of unlike bit depths); and a Motion JPEG track of another frame (a
  picture's fields, 12 bits, streams with no markers). `shape` is the track's sample elements,
  one axis; there is no `item_bits`, since the IR does not say what a sample decodes to; the grid
  is `Each(1)` under `short`; and the chain is `source`, with `format` `mp4` and `id` the sample
  entry's type, its four characters as a string. Each sample element is a row, in decode order:
  a sample, or, of a PCM track of one sample size, a chunk's samples together
  ([zarrify/mp4.md §5.3](zarrify/mp4.md#53-elements)).
- **No grid**, and neither `shape` nor `item_bits`, in one description of index 0, for a track
  whose samples are not one array: a track with no samples, with more than one sample
  description, or with its sample sizes in an `stz2` box; and a PCM track with a sample that its
  tables size as no whole number of frames, which readers do not agree on (ffmpeg reads its
  frames by the sample entry's size).
- **The fill value** is `undefined`: MP4 has none.
