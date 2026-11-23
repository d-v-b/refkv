# Stored arrays in the IR

A **stored array** is one structure that a source itself declares as an array, described as the
source stores it, at the byte level: its shape, the size of its elements in bits and how they
pack, how the source cuts it into units and whether it may leave a unit out, the codecs that
decode a unit to the elements' bytes, the fill value of a unit it leaves out, where the source
declares what its elements are, and a map of the units the source holds. Parsers build the descriptions and the map into the IR
([conventions §8](conventions.md#8-the-ir-mirror)); projections and profiles read them, together
with the source's own declarations, and decide everything about the output, starting with the data
type. This document is not a convention: nothing it specifies is written in an output, and the IR
mirror does not record it.

Version: 0 (draft) · Schema: [stored-array.json](stored-array.json)

Added 2026-11-08. The design is design/proposals/ir-array-model.md (d-v-b/refkv#289); where the
two differ, this document governs.
Changed 2026-11-22: every projection reads the descriptions (d-v-b/refkv#378).

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

The descriptions and the chunk map are given to every projection alike: refkv's own, which
reads them with the format's description (the format's known structures, specified in its
document under [zarrify/](zarrify/)) and decides the output through the plan its convention makes
from them, and a layout plugin's ([layout.md §2](layout.md#2-the-inputs)), which reads the same
parsed input and none of refkv's output or plan.

Every array a parser reaches has a description, whether or not its profile presents it. A format
whose parser describes its arrays has a paragraph in [§7](#7-the-formats), which lists the
structures that are its stored arrays and what each of their declarations is, and says what of
the format is described and what is not yet; the parser of a format with no paragraph there
describes nothing.

These rules are of the descriptions. An element's form
([conventions §8.5](conventions.md#85-forms-and-recipes)) is older, and still gives a format's own
geometry of a unit, a data type's name among it.

## 2. The description

A description has these members (the Rust type `StoredArray` of `rust/refkv-ir/src/array.rs`):

| Member | Contents |
| --- | --- |
| `element` | the IR element that declares the array |
| `index` | the array's index among those `element` declares, for a kind of element that may declare several; else absent |
| `declarations` | the places where the source declares what the array's elements are, as the format's paragraph of [§7](#7-the-formats) lists them |
| `shape` | the axes in storage order, the slowest-varying first; absent where the source declares no extent (an HDF5 null dataspace), which holds no element, unlike a scalar's empty shape (the JSON form writes `"shape": null`, the member present) |
| `axes` | the source's own order of the axes, where it differs from storage order: `axes[i]` is storage axis i's position in the source's list (a Fortran-order NumPy array of shape (3, 4) has `shape` [4, 3] and `axes` [1, 0]); else absent, never the identity |
| `item_bits` | the bits of one element, decoded; absent for an array with no grid, and, of an array whose grid gives no fill, where its elements are those `source` makes ([§3.2](#32-the-list)) and the IR does not know their size |
| `pad_from` | for elements that are not whole bytes: the first axis from which the elements of a decoded unit pack unbroken, each run over this axis and those after it ending on a byte boundary; absent: the whole unit packs unbroken |
| `grid` | how the source cuts the array into units ([§2.3](#23-the-grid)), and, where its layout may leave a unit out, the fill value ([§2.4](#24-sparse-grids-and-the-fill-value)); absent where the IR does not model the array's storage |
| `chains` | the codec chains a unit may be encoded with, by position in the IR's chain table ([§3.1](#31-chains)) |
| `unit_parameters` | the codec parameters whose values differ from unit to unit, each a column of the chunk map ([§3.1](#31-chains)) |

A description stops at the elements' bytes as they come out of a chain. Which data type those
bytes are, in which byte order, how a structure's members lie, how a string or reference is
presented, and what the values mean (a scale, a blank, dimension names) are the profile's: it
reads them from the declarations, or, where a codec makes the elements, from the codec's entry
([§3.2](#32-the-list)).

### 2.1 Keys

A description is keyed by the IR element that declares the array. For a kind of element that may
declare several arrays, the key always adds the array's index among them, in the source's order,
however many it declares; the format's paragraph of [§7](#7-the-formats) names those kinds. So `(element, index)` is one
array's alone, and on each element either no description has an index or every one has, as 0 to
n − 1. Within a run, the id is what is computed on; the JSON form also gives the element's path
([§2.5](#25-the-json-form)).

Each format's paragraph of [§7](#7-the-formats) says how a profile finds, from the facts its parser gives, the key of each array's description: the
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

A **parameter** of a codec, or a given fill value ([§2.4](#24-sparse-grids-and-the-fill-value)), is a value (`{"value": …}`) or a place whose
bytes the codec makes it from (`{"source": place}`). It is tagged, so that a value that is an
object cannot be read as a place.

### 2.3 The grid

A grid has an entry for each axis, `Each(n)` or a `List`, an `edge`, and `sparse`
([§2.4](#24-sparse-grids-and-the-fill-value)):

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

### 2.4 Sparse grids and the fill value

A grid is **dense** where the source's layout stores every unit (a NIfTI's voxels, a FITS image,
a FLAC's frames): the chunk map has a row for each of its units, and it has no fill value. A grid
is **sparse** where the layout may leave a unit out (an HDF5 chunk index, a TIFF entry of no
bytes): the map has a row for each unit the source holds, and a unit it leaves out holds the fill
value, where the source gives one. Sparse says what the layout allows, not what the file does: a
sparse grid whose source holds every unit stays sparse, its fill kept.

A sparse grid's `fill` is absent where the source defines none (HDF5's "undefined", and a format
that has no fill value), or defines one the IR cannot express, whose place is then among the
description's `declarations` (the general fallback); a profile tells the two apart through the
format's list of declaration places. Otherwise it is one of:

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
`null` where it is absent, and `path`, its element's path in the IR (null for an element the IR
does not hold, which no checked IR has); a description has no `fill`, which is its grid's. A place is
`{"element": e, "member": j, "range": [offset, length]}` (`member` and `range` may be null) or
`{"object": key, "offset": o, "length": n}` (`object` null for the parsed source). A grid is
`{"axes": [...], "edge": "full" | "short", "sparse": …}`, each axis `{"each": n}` or
`{"list": [...]}`, each run of a list `n` or `[n, c]`, and `sparse` null for a dense grid, else
`{"fill": …}`, the fill null where the source gives none, else `"format_default"` or
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
| `blosc` | a Blosc 1 chunk, with its 16-byte header | none | none | the chunk's bytes |
| `skip` | `length` bytes that a reader passes over, then the bytes its decoding makes | `length`: the count: a value, or a place that holds it as the source writes it in ASCII, decimal digits with a `+` before them allowed (the descriptor of NRRD's `byte skip`) | none | the bytes after the first `length` |
| `shuffle` | the bytes of elements of `element_size` bytes each: every element's first byte, then every element's second, and so on, then the bytes past the last whole element as they are | `element_size`: a place of one value, or a value where the source has no field for it | none | the elements' bytes, each element's together |
| `lsb_first` | elements of `bits` bits each, packed from each byte's least significant bit: bit k of the unit is bit k mod 8, counted from the least significant, of byte ⌊k / 8⌋, and an element's first bit is its least significant | `bits`: a value, from 1 to 7, the array's `item_bits` | none | the elements packed most significant bit first, as [§3.1](#31-chains) packs them |
| `h5_fletcher32` | bytes, then their Fletcher-32 checksum as HDF5 computes it (4 bytes) | none | none | the bytes before the checksum |
| `h5_lz4` | the layout of HDF5's LZ4 filter (32004): the decoded length (8 bytes, big-endian), a block size (4 bytes, big-endian), then each block as its stored length (4 bytes, big-endian) and an [LZ4 block](https://github.com/lz4/lz4/blob/dev/doc/lz4_Block_format.md), or the block's bytes as they are where the stored length is the block's own | none | none | the blocks' bytes, joined |
| `h5_szip` | what HDF5's SZIP filter (4) writes | `options`, `pixels_per_block`, `bits_per_pixel`, `pixels_per_scanline`: each a place of one value | none | what the filter decodes it to |
| `h5_nbit` | what HDF5's N-bit filter (5) writes | `parameters`: a place of every value the filter has, which give the datatype it packs | none | what the filter decodes it to |
| `h5_scaleoffset` | what HDF5's scale-offset filter (6) writes | `parameters`: a place of every value the filter has | none | what the filter decodes it to |
| `h5_bitshuffle` | what HDF5's bitshuffle filter (32008) writes ([zarrify/hdf5.md §3.4](zarrify/hdf5.md#34-the-codec-refkvh5_bitshuffle)) | `element_size`, and, where the filter gives them, `block_size` (0: the filter's default) and `compression` (0: the blocks bare; 2: framed with LZ4): each a place of one value | none | the elements' bytes |
| `czi_zstd1` | the data of a CZI subblock of Compression 6, Zstd1 ([zarrify/czi.md §3.1](zarrify/czi.md#31-pixel-types-and-codecs)): a header, which is the byte `01`, or the bytes `03 01` and a byte of flags; then a Zstandard frame ([RFC 8878](https://www.rfc-editor.org/rfc/rfc8878)) | none | none | the frame's content; or, where the header has flags and their bit 0 is set (hi-lo packing), that content with its two halves put together: it holds the low bytes of its 16-bit values, then their high bytes, and decoding makes the values' bytes, the low byte of each first |
| `xz` | an .xz stream | none | none | the bytes it decompresses to |
| `tiff_lzw` | one TIFF LZW stream (TIFF 6.0 §13: codes packed most significant bit first, with the early change every TIFF writer uses now); or, where its first byte is 0 and its second odd, one of the older form libtiff calls old-style and tells by those bytes (codes packed least significant bit first, the code's width growing one code later) | none | none | the stream's bytes, up to the unit's: decoding stops there, and reads no code after ([zarrify/tiff.md §4.3](zarrify/tiff.md#43-data-types-and-codecs)) |
| `packbits` | PackBits runs (TIFF 6.0 §9) | none | none | the runs' bytes |
| `tiff_predictor` | a unit's rows, each differenced along its columns as TIFF's Predictor says: 2, each sample less the same sample of the pixel before it, as unsigned integers of `item_bits` bits in the byte order `byte_order` gives, modulo 2^`item_bits` (TIFF 6.0 §14); 3, the row's samples' most significant bytes, then their next and so on to their least, each byte less an earlier byte of that row of bytes, modulo 256 (Adobe's [TIFF Technical Note 3](http://chriscox.org/TIFFTN3d1.pdf)); that byte is as many places before it as a pixel has samples. A pixel's samples are the unit's last axis where `planar_configuration` is absent or 1 and the unit has three axes, else one | `predictor`, and `planar_configuration` where the image has more than one sample and the field: each a place of a field's one value; `byte_order`: a place of the file's byte order mark | none | the rows' elements, summed back, in the file's byte order |
| `ccitt_t6` | one ITU-T T.6 (Group 4) stream of the unit's rows | none | none | elements of 1 bit, 1 for a coded black run and 0 for a white one, most significant bit first, each row padded to a whole byte |
| `jpegls` | one JPEG-LS stream (ISO/IEC 14495-1), whichever its interleave mode | none | none | the frame's samples, each an unsigned integer of `item_bits` bits, little-endian, a pixel's components together in the frame's order |
| `dicom_rle` | one frame of DICOM's RLE (PS3.5 [Annex G](https://dicom.nema.org/medical/dicom/current/output/chtml/part05/chapter_G.html)): a header of 16 unsigned 32-bit integers, little-endian, the number of segments and each one's offset in the frame, then the segments, each the PackBits runs of one byte of every sample of one plane, a sample's most significant byte first | none | none | the frame's samples, of `item_bits` bits, little-endian, a pixel's samples a plane each, one after another: the unit's first axis where it has three. The bytes a segment's runs give past its plane's are dropped |
| `jpeg` | one JPEG stream (ISO/IEC 10918-1) of the sequential DCT-based, progressive DCT-based or lossless process, whose sample precision is at most `item_bits`, which is 8 or 16; with `tables`, an abbreviated stream whose tables `tables` holds | `tables`: a place of an abbreviated stream of tables alone (TIFF's JPEGTables) | none | the frame's samples, each an unsigned integer of `item_bits` bits, little-endian, a pixel's components together in the frame's order, every component at the frame's size. Converting their color space is the profile's, which reads how the source declares it |
| `jpeg_restart_interval` | the entropy-coded data of one restart interval of a baseline JPEG stream's scan (ISO/IEC 10918-1), without the restart marker or EOI after it | `header`: a place of the stream's header | none | the samples of the interval's MCUs, one byte each, a pixel's components together in the frame's order, every component at the unit's size: the frame header gives the whole stream's size, and a unit's is its grid's. Converting their color space is the profile's, as for `jpeg` |
| `jpeg2k` | one JPEG 2000 codestream (ISO/IEC 15444-1), bare or in a JP2 file | none | none | the image's samples, of `item_bits` bits, little-endian, a pixel's components together in the codestream's order |
| `jpegxl` | one JPEG XL codestream (ISO/IEC 18181-1), bare or in its container (18181-2) | none | none | the image's samples, of `item_bits` bits, little-endian, a pixel's channels together |
| `jpegxr` | one JPEG XR file (ITU-T T.832, in its container) | none | none | the image's samples, of `item_bits` bits, little-endian, a pixel's channels together, in red, green, blue order whichever order its pixel format stores ([zarrify/czi.md §3.1](zarrify/czi.md#31-pixel-types-and-codecs)) |
| `webp` | one WebP file | none | none | the image's samples, one byte each, a pixel's red, green and blue together, with a fourth, alpha, where the unit's last axis has four: 255 where the stream holds none |
| `jpeg2k_tile` | the tile-parts of one tile of a JPEG 2000 codestream (ISO/IEC 15444-1), in the codestream's order, each from its SOT marker to its end, without the main header before them or the EOC after | `header`: a place of the codestream's main header, its bytes before its first tile-part: from its SOC through the last marker segment before the first SOT | none | the tile's samples, of `item_bits` bits, little-endian, a pixel's components together in the codestream's order: the tile is the one its SOT names, on the grid the header's SIZ gives, and of the unit's size |
| `lerc` | one LERC blob | none | none | the blob's values in its own data type, little-endian, a pixel's bands together; a pixel its mask leaves out is as the LERC library decodes it |
| `eer` | one frame of Electron Event Representation: run lengths of `skip_bits` bits, each with a sub-pixel position of `horizontal_bits` and `vertical_bits` bits | `skip_bits`, `horizontal_bits`, `vertical_bits`: each a value, or a place of a field's one value; absent, 7, 2 and 2 | none | elements of 1 bit, 1 where an event lands on the pixel, most significant bit first, each row padded to a whole byte; the sub-pixel positions are not made |
| `grib_complex` | the data of FM 92's data template 7.2, or, with `differencing`, 7.3, every number the most significant bit first. With `differencing`: as many first values as its order, each an unsigned integer of its descriptors' octets, then the overall minimum of the differences, of as many octets, its first bit its sign and the others its magnitude. Then each group's reference, of `bits` bits; from the next whole octet, each group's coded width; from the next whole octet, each group's scaled length; from the next whole octet, the groups' coded values, each of its group's width, one after another | `bits`: a place of one octet, an unsigned integer, at most 64 (section 5's octet 20); `groups`: a place of section 5's octets 22–47, of data representation template 5.2, of which the codec reads the missing value management (octet 23), the number of groups (32–35), the reference of the groups' widths (36) and the bits of a coded width (37), the reference of the groups' lengths (38–41), their increment (42), the last group's length (43–46) and the bits of a scaled length (47), each an unsigned integer, big-endian; the missing value management is 0, none of the values missing; `differencing`: a place of section 5's octets 48–49, of template 5.3: the order of the spatial differencing, 1 or 2, and the octets of each of its descriptors, at most 8 | none | the unit's integers, each of `item_bits` bits, 64, two's complement, the most significant bit first. A group's width is its coded width plus the widths' reference; its length is its scaled length times the increment plus the lengths' reference, and the last group's is the one given. A value is its group's reference plus its coded value. With `differencing`, the values are differences, summed back in order: the first ones, as many as the order, are the first values, and each after them is itself plus the overall minimum plus the value before it (order 1), or plus twice the value before it less the one before that (order 2), as ecCodes reads them |
| `grib_png` | a PNG file ([W3C PNG](https://www.w3.org/TR/png-3/)), as FM 92's data template 7.41 holds one: not interlaced, with no palette, of gray samples of 1, 2, 4, 8 or 16 bits, or of red, green and blue samples of 8 bits, with or without alpha, a pixel of `item_bits` bits | none | none | the unit's integers: the image's pixels, row after row, each pixel's bits as one unsigned integer of `item_bits` bits (a gray sample; red, green and blue, 24 bits; with alpha after them, 32), the most significant bit first, one after another, without the bits that end a row of the image on a whole octet, then the bits that fill the last octet |
| `grib_ccsds` | one stream of CCSDS 121.0-B adaptive entropy coding, as [libaec](https://gitlab.dkrz.de/k202009/libaec) writes it and FM 92's data template 7.42 holds it: unsigned samples of `bits` bits, in blocks of `block_size` samples, `reference_sample_interval` blocks to an interval, with libaec's options `flags` | `bits`: a place of one octet, from 1 to 32 (section 5's octet 20); `flags`: a place of one octet, libaec's flags (octet 22: 8, the samples are preprocessed; 16, the restricted set of code options; 32, each interval ends on a whole octet; 2 and 4 say how a decoder lays its samples out, and nothing of the stream); `block_size`: a place of one octet (octet 23); `reference_sample_interval`: a place of 2 octets (octets 24–25); each an unsigned integer, big-endian | none | the unit's integers, the stream's first samples, each an unsigned integer of `item_bits` bits, which is `bits`, the most significant bit first, one after another, then the bits that fill the last octet |
| `h264` | one access unit of an H.264 stream (ITU-T H.264), as ISO/IEC 14496-15 stores a sample: its NAL units, each after its length, of as many bytes as `setup` says | `setup`: a place of an AVCDecoderConfigurationRecord (an `avcC` box's payload), which holds the parameter sets; `plane`: a value, 0, 1 or 2, the picture's luma plane or one of its two chroma planes (Cb, then Cr); `sync` | `sync`: `UInt64`, 1 where a decoder may start at the unit, else 0 | plane `plane` of the access unit's picture, at the plane's own size: samples of 1 byte, or, of a bit depth above 8, of 2 bytes, little-endian. Its units are a stream ([§3.1](#31-chains)) |
| `hevc` | one access unit of an HEVC stream (ITU-T H.265), stored as `h264`'s | as `h264`'s, `setup` a place of an HEVCDecoderConfigurationRecord (an `hvcC` box's payload) | as `h264`'s | as `h264`'s |
| `av1` | one temporal unit of an AV1 stream, as [AV1-ISOBMFF](https://aomediacodec.github.io/av1-isobmff/) stores a sample: its OBUs, each with its size | as `h264`'s, `setup` a place of an AV1CodecConfigurationRecord (an `av1C` box's payload) | as `h264`'s | as `h264`'s |
| `vp9` | one frame or superframe of a VP9 stream | as `h264`'s, `setup` a place of a VPCodecConfigurationRecord (a `vpcC` box's payload) | as `h264`'s | as `h264`'s |
| `flac` | one frame of a FLAC stream ([RFC 9639 §9](https://www.rfc-editor.org/rfc/rfc9639#section-9)): its header, its subframes and its CRC-16 | `setup`: a place of the stream's STREAMINFO block's body, its 34 bytes ([RFC 9639 §8.2](https://www.rfc-editor.org/rfc/rfc9639#section-8.2)), which gives the sample rate and the bit depth to a frame whose header refers to it | none | the frame's samples, its inter-channel samples in order and each one's channels together, in the stream's order of channels (a side or mid channel restored to the left and right ones); each sample a two's-complement integer of `item_bits` bits, little-endian: the bytes of STREAMINFO's MD5 |
| `fits_rice` | one unit's stream of FITS's Rice coding (`RICE_1`, FITS Standard 4.0 section 10.4.1, as cfitsio's `fits_rcomp` writes it), its bits most significant first: a first value of `byte_pix` bytes; then the unit's pixels in blocks of `block_size`, each block a code of 3, 4 or 5 bits (for a `byte_pix` of 1, 2 or 4) and its pixels' differences, each pixel less the one before it (the first less the first value), a difference d mapped to 2d where it is not negative and to −2d − 1 where it is. A code of 0 has no more bits, every difference of its block 0; the highest code (7, 15 or 26) has each mapped difference in 8 × `byte_pix` bits; any other code c has each as its bits above its low c − 1 in unary (as many 0 bits, then a 1), then its low c − 1 bits | `block_size`, `byte_pix`: each a place of a card; absent, 32 and 4 | none | the unit's pixels, each an integer of `byte_pix` bytes, big-endian: the sums of the differences, modulo 2^(8 × `byte_pix`) |

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
grid. A unit the source does not hold has no row, which only a sparse grid's may lack
([§2.4](#24-sparse-grids-and-the-fill-value)), and no row is for an array with no grid.

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
  external file's key is checked in the store's IR, once the file is grafted), and in a store IR is never none and
  names one of the store's objects; a place lies within what it names
  ([§2.2](#22-places-and-parameters)), its end below 2^64.
- **No extent.** A description without a shape has no grid, no `axes` and no `pad_from`.
- **The grid.** Its rank is the array's rank, and it is in canonical form
  ([§2.3](#23-the-grid)).
- **Chains.** At least one when `grid` is set, none when it is not; each a position in the chain
  table, and none twice. The chain table holds no chain twice, and every codec of it is one of the
  list ([§3.2](#32-the-list)), with only its entry's parameters.
- **`axes`.** When given, a permutation of the rank other than the identity.
- **`item_bits`, `pad_from` and the fill.** `item_bits` is set when its grid's fill is given,
  and when `grid` is, unless every chain of the array begins with `source`; it is at least 1. `pad_from` is below the rank, given only when `item_bits` is set and
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
- an array whose grid is dense has a row for each of its units
  ([§2.4](#24-sparse-grids-and-the-fill-value));
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
them to the IR, which, in order: sorts the descriptions by `(element, index)`, so that which
arrays fall back follows from the source alone; fits the map to its limit (below); drops from the chain table the
chains no description names, keeping the others' order, and renumbers the descriptions' positions
in it; fits the descriptions to their limit (below), which falls back the same way and drops the
chains it leaves unnamed; sorts the rows; and checks them ([§5](#5-checks)). Each
format's projection reads them where its parser builds them, with the format's description and the plan refkv's convention
makes from it ([§1](#1-stored-arrays)), and so may any layout, from the parsed input.

**Only two things change ids afterwards.**

- **A graft** carries a file IR's descriptions and rows into the store it joins, in order:
  - it fits the file's arrays, in the file's order, to the store's chunk map limit as it stands
    then (below), which counts every object the store has listed, grafted or not; a graft does not
    grow it. A store has the 2^22 once, where each file had it, so an array that kept its rows in
    its file may fall back in the store, depending on what was grafted before it;
  - it carries into the store's chain table only the chains the file's remaining descriptions
    name, each with its places' element ids renumbered and then found in the table by its value:
    a chain the table has keeps its position, and a new one is appended, in the order the file's
    descriptions first name them. The table's existing chains and positions are untouched. Two
    chains of one array that the renumbering makes equal become one, which the array's rows and
    unit parameters then name; the two give the same unit parameters, or the graft fails;
  - it fits the file's descriptions, last, to what remains of the store's descriptions' limit, as
    describe does (below): where even their fallen-back descriptions do not fit, the file joins the
    store with none, its key added to the store's `undescribed` list, as is the key of a file
    marked `undescribed` at its own describe. A graft that fails on the IR's room adds nothing to
    the store.

  `element`, every place's element and the `element` column take their new ids; `array` takes the
  description's new position, after the store's; parameter columns of one name join; and the
  parsed source's null key, in every place and in `ranges`, becomes the file's key in the store.
- **An unfold** of a run (a store's own `check::finish` may unfold one) gives its members after the first new ids: each row's and place's element in
  member j's subtree moves to member j's copy, and `member` is cleared on every element that was
  in the run.

The mirror's canonical form ([conventions §8.8](conventions.md#88-canonical-form)) leaves the
descriptions and the chunk map out before it unfolds the runs, so its unfolds renumber none.

**Limits.** The IR's budget counts the descriptions' entries and the chunk map's in fields of its
own, each against a limit of its own, never against the IR's room (its records and elements). The
descriptions' entries are each description, each axis of its shape, each run of a listed axis (an
`Each` axis has none), each of its places (its declarations, and its grid's fill given at a place), each of
its chains and each unit parameter; and of the chain table, each chain, each codec and each place
a codec's parameter names. The chunk map's are each row, each of its coordinates, each of its
`ranges` items and each unit-parameter value it sets (a row's `range` is part of it, and is none).
The descriptions' limit is `2^22 + floor(Z / 4)` entries for a source of `Z` bytes, and the chunk
map's `2^22 + 4 × Z`. A store's are `2^22 + floor(Z / 4) + 4 × o` and `2^22 + 4 × Z + 4 × o`, for
its `o` objects of `Z` bytes in all: a file's of `Z` bytes, and 4 more for each object, as the limit
of its table's rows is ([conventions §8.9](conventions.md#89-stores)). The floor is of the total,
not of each object's size. A store's limits grow as it lists its objects, and not when a file is
grafted. Every unit in space 0 has at
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

Each profile but a layout specifies the stored arrays its parser describes in a file of its own,
`spec/zarrify/<profile>/stored-arrays.md`: which structures are stored arrays, their keys, their
declarations and shape, their units, codecs and fill value, and what is not described. A profile
whose parser describes none yet says so there. `spec/tools/stored_formats.py` checks that every
profile of the registry but a layout has that file, and no profile that is not in the registry.

The documentation site renders every profile's file here, in the registry's order, when it is built:

<!-- profiles:stored-arrays -->
Each profile's stored arrays are in its own directory of [zarrify/](zarrify/), and on
[the documentation site](https://d-v-b.github.io/refkv/spec/stored-array.html#7-the-formats) all of them are here.
<!-- /profiles -->
