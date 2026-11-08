# Proposal: a description of stored arrays in the IR, at the byte level

refkv's parsers describe the arrays a source holds in untyped JSON, in a different shape for each
format, and each projection works out from that JSON and from the units' bytes the grid and codecs
it writes. This note proposes one description of an array as the source stores it, at the byte
level:
- its shape;
- the size of its elements in bits, and how they pack;
- how the source cuts it into units;
- the codecs that decode a unit to the elements' bytes;
- its fill value;
- where the source declares what its elements are;
- a map of the units the source holds, kept as an Arrow table.

Parsers fill it. Profiles read it, together with the source's own declarations, and decide
everything about the output, starting with the data type.

It is a design note. It changes no output by itself: a format adopts the description as its
projection is reworked, and its outputs are compared before and after.

## What the IR describes, and what it does not

The IR describes the source. A projection decides the output.

- **In the IR:** what the source holds, as found.
- **Not in the IR:** what a projection chooses:
  - which arrays to present, and how to assemble them (a TIFF level from several IFDs, a CZI level
    from a lattice of subblocks);
  - output chunking (joining strips, rechunking), and shards and their index;
  - attributes, member order, padding, and repairs (an LZW strip's tail);
  - how the source's values are typed and spelled (a Zarr data type, a string's `vlen-utf8`), and
    whether a scale is applied;
  - default values, and any synthetic bytes.

  The IR holds no byte the source does not. It holds no concept tied to an output encoding, such as
  Zarr's `sharding_indexed` codec.

**Where the IR cannot or need not interpret something, it falls back to a more general
representation:**
- bytes, or where the source holds them;
- the source's own id and parameters;
- an array whose storage it does not model.

Interpreting it is left to the profile. A typed vocabulary in the IR (data type names, struct
layouts, enumerations) would be translation work, which belongs to profiles, and every name in it
would be a chance to bend the source toward an output.

**Two places hold output decisions today.** Each goes when its format adopts the description; the
mirror's forms may change with them:
- **TIFF and VSI-TIFF's JPEG prefix.** An Adobe marker, and the JPEGTables without their SOI and
  EOI, sit in the IR's shared data, and recipes splice them into units (`["shared", k]`,
  [conventions §8.5](../../spec/conventions.md#85-forms-and-recipes)). The marker is a synthetic
  byte. In place of the prefix, the `jpeg` codec takes the tables as a parameter naming the
  JPEGTables tag's bytes, and the color transform the marker conveys as a parameter. Building a
  self-contained JPEG for the output is the projection's.
- **FITS's `tile_cut`.** The parser cuts a tiled image's tiles into output chunks
  ([rust/refkv-ir/src/fits.rs](../../rust/refkv-ir/src/fits.rs)), so each `heap/<j>` element holds
  several tiles. The heap becomes one element, each tile a chunk map row naming its range within
  that element, and the projection makes the same cut, so neither the output nor the IR's element
  count grows.

## The gap

**Per array, in facts.** Each parser hands its projection facts as untyped JSON (`facts["` occurs
348 times in `rust/refkv-ir/src`), and each format describes its arrays in its own shape. Many
describe the output already:

- **HDF5's `"array"` facts** ([rust/refkv-ir/src/hdf5/file.rs](../../rust/refkv-ir/src/hdf5/file.rs)) give:
  - a variable-length string as `"string"` with `vlen-utf8`;
  - a fixed-length string as `uint8` with an added axis;
  - a compound with string or reference members as rebuilt records;
  - a fill value of zero where the file gives none;
  - `numcodecs.zlib` at level 1, whatever the file's level;
  - Fletcher32 as a trim of the stored chunk, rather than a codec.
- **NumPy's facts** give an `S` or `V` type as `uint8` with an added axis, and a default fill
  ([rust/refkv-ir/src/npy/header.rs](../../rust/refkv-ir/src/npy/header.rs)).
- **Others leave the array to the projection:** NIfTI's projection reads the data type, shape and
  axes from the header's bytes.

**Per unit, in forms.** A data element carries a form
([conventions §8.5](../../spec/conventions.md#85-forms-and-recipes)): its `recipe`, the unit's
bytes, and, as its format gives them, a `geometry` and a `codec`. Each format spells these
differently.

**Units, per format.** Each projection finds a unit's bytes its own way:
- **HDF5** through a `store` member that holds the element id of the dataset's chunks struct, the
  indexes of masked chunks, external ranges and compact data; the projection reads the chunks with
  `ir.members`;
- **TIFF** through plane element ids and a member order;
- **FITS** through a list of elements and coordinates;
- **GRIB** through message ranges: its parser emits section elements, but its projection reads byte
  ranges;
- **NumPy** through an offset and a length, and for a deflated `.npz` member the compressed range,
  framed as gzip, with the header skipped.

So the same facts about a stored array are spelled many ways. They are checked nowhere, often
already bent toward the output, and handled by each projection separately. The build tools' plan
([profile-plugins.md](profile-plugins.md)) asks each format for a typed description of what it
holds, but nothing in the core describes an array.

## What an array is

A **stored array** is one structure that the source itself declares as an array:
- an HDF5 dataset;
- one TIFF IFD's image, with a samples axis last (`[rows, columns, samples]`) where the IFD is
  chunky, first (`[samples, rows, columns]`) where it is planar, and none where it has one sample;
- a NumPy file or `.npz` member;
- a FITS image HDU, and, where its tiles carry a NULL_PIXEL_MASK, that mask as a second array the
  HDU declares (the parser rejects such files today; FITS's PR may lift that);
- a NIfTI image;
- a safetensors tensor;
- one CZI subblock;
- one GRIB field: sections 3 to 7 as they stand at a section 7, since sections 3 to 7 may repeat
  within a message. A regular grid's field has the rows and columns section 3 lists. A grid that
  lists a number of points per row (a reduced grid), or template 3.101, has one axis of points.

Anything assembled from several of them is the plan's: a TIFF level, an OME image, a CZI level, an
IMS level.

Every array the parser reaches has a description, whether or not it is presented; presenting it is
the plan's call. An HDF5 dataset whose object header the parser visits but whose messages lie past
the IR's room has one too, keyed on its `objects/<i>` struct, with `Place::Extent` declarations and
whatever the parser read of it; the plan, not the parser, will decide which datasets to present.

A description is keyed by the IR element that declares the array. For a kind of element that may
declare several arrays, the key always adds the array's index among them, in the source's order,
however many it declares:
- a safetensors `header` declares every tensor, counted in the header's order without
  `__metadata__`;
- a GRIB message holds its fields;
- a FITS image HDU declares the image (0) and, where its tiles carry one, the null pixel mask (1).

Its JSON form also gives that element's path, for the build tools' description schemas, which name
structures by path ([profile-plugins.md](profile-plugins.md#the-description)). Within a run, the id
is what is computed on.

A profile finds an array's description from the facts, so each format's document says how its facts
name the element (and index) that keys it. An HDF5 spike had to match datasets to descriptions by
object header address, since HDF5's facts give each node its address and its chunks but not its
`objects/<i>` element; HDF5's PR gives each dataset node that element.

## The description

A Rust type in a new module, `rust/refkv-ir/src/array.rs`:

```rust
/// An array as the source stores it, at the byte level.
pub struct StoredArray {
    /// the IR element that declares the array
    pub element: u32,
    /// the array's index among those `element` declares, for a kind of element that may declare
    /// several
    pub index: Option<u64>,
    /// where the source declares what the array's elements are, as the format's document lists
    /// them: an HDF5 datatype message (or the committed datatype a shared one points at), a NumPy
    /// header's `descr`, a TIFF IFD's BitsPerSample and SampleFormat entries and the file header
    /// that gives the byte order, a NIfTI header
    pub declarations: Vec<Place>,
    /// the axes in storage order, the slowest-varying first; `None` where the source declares no
    /// extent at all (HDF5's null dataspace), which holds no elements, unlike a scalar's `[]`
    pub shape: Option<Vec<u64>>,
    /// the source's own order of the axes, where it differs from storage order: `axes[i]` is storage
    /// axis i's position in the source's list (a Fortran-order NumPy array of shape (3, 4) has
    /// shape [4, 3] and axes [1, 0])
    pub axes: Option<Vec<u32>>,
    /// the bits of one element, decoded; `None` only for an array with no grid and no given fill
    pub item_bits: Option<u64>,
    /// for elements that are not whole bytes: the first axis from which the elements of a decoded
    /// unit pack unbroken, each run over this axis and those after it ending on a byte boundary
    /// (a chunky TIFF's columns axis, 1: each row of columns × samples ends on a byte); `None`:
    /// the whole unit packs unbroken
    pub pad_from: Option<u32>,
    /// how the source cuts the array into units; `None` where the IR does not model the array's
    /// storage (below)
    pub grid: Option<Grid>,
    /// the codec chains a unit may be encoded with, each by its position in the `Ir`'s chain
    /// table; an array stored as is has one empty chain. A chunk map row's `chain` and a
    /// `UnitParameter`'s `chain` are positions in this list, not in the table: a unit uses
    /// `chains[0]` unless its row gives another. Every chain of an array ends in the same element
    /// bytes
    pub chains: Vec<u32>,
    pub fill: Fill,
    /// the parameters whose values differ from unit to unit, each a column of the chunk map
    pub unit_parameters: Vec<UnitParameter>,
}

/// how the source cuts the array into units
pub struct Grid { pub axes: Vec<AxisUnits>, pub edge: Edge }
/// the units along one axis: one length for all, the last cut by `edge` where the length does not
/// divide the axis; or each unit's length in order, runs of equal ones kept as `[length, count]`
pub enum AxisUnits { Each(u64), List(Vec<Run>) }
pub enum Run { One(u64), Repeat(u64, u64) }
/// how a unit past an `Each` axis's end is stored, as the source declares its layout
pub enum Edge {
    /// at the full unit length, the part past the end filled (HDF5 chunks, TIFF tiles)
    Full,
    /// cut to the axis (a TIFF last strip)
    Short,
}

/// a codec that takes a unit's bytes toward its elements' bytes: by name, and its parameters
pub struct Codec { pub name: String, pub parameters: Map<String, Param> }

/// a parameter: a value, or bytes of the source that the codec makes it from
pub enum Param { Value(J), Source(Place) }

/// bytes of the source: an IR element (with `member`, for an element in a run's subtree, the
/// position j of the run's member it is in), or `range`, an (offset, length) within the
/// element's extent (its bytes in its own space); or, where no element holds them, a range of an
/// object by its key (`None`: the parsed source itself), the offset counted from the object's start
pub enum Place {
    Element { element: u32, member: Option<u64>, range: Option<(u64, u64)> },
    Extent { object: Option<String>, offset: u64, length: u64 },
}

/// the source's fill value
pub enum Fill {
    /// the source defines none (HDF5's "undefined")
    Undefined,
    /// the source says its format's default (HDF5's "default", which the HDF5 specification
    /// defines as zero bytes)
    FormatDefault,
    /// one decoded element's bytes, ceil(item_bits / 8) of them: a base64 string, or the place
    /// that holds them (HDF5's fill value message's value, or the old fill value message's). An
    /// element of fewer bits than whole bytes takes the first bits of its bytes, packed as the
    /// array packs them
    Given(Param),
}

/// a parameter named `name` of codec `codec` of chain `chain`, whose value each unit of that chain
/// gives in the chunk map's column `p.<codec name>.<name>`; its Arrow type is the one the codec's
/// entry in the list gives the parameter
pub struct UnitParameter { pub chain: u32, pub codec: u32, pub name: String }
```

**No data types.** The description stops at the elements' bytes as they come out of the chain:
- **Typing.** Which Zarr data type those bytes are, in which byte order, how a struct's members lie,
  and how a string or reference is presented are the profile's. It reads them from the source's
  declaration, which `declarations` locates, or, where a codec makes the elements (below), from the
  codec's entry. Each format's document lists what each of its places is.
- **Meaning.** What the values mean is the profile's too: a scale (FITS's BSCALE and BZERO, NIfTI's
  `scl_slope`), a blank, dimension names.

A function that turns a source's type declaration into a Zarr data type (a NumPy `descr`, used by
NumPy and `.npz`) lives in the profiles' shared tooling, not in the core.

A declaration must be reachable. Where a parser reads it but keeps it only in facts today, the
format's PR gives it a place:
- a gzipped NIfTI becomes one derived element over the file in space 0, the only claim on those
  bytes, with `header`, its extender and extensions, and `voxels` (from `vox_offset`) in its derived
  space. Validating the mirror's view of such a header re-derives the stream, and the parser reads
  only its start; NIfTI's PR keeps validation to the stream's start, or says that cost is taken;
- a deflated `.npz` member gets `header` and `data` children in its derived space.

**Arrays whose storage the IR does not model.** These have a description with `grid: None`, no
chains and no chunk rows: its element, shape, declaration and whatever else the parser knows. The
profile reads the declaring structure for the rest, or leaves the array out. Examples:
- an HDF5 virtual dataset;
- a NumPy `O` array;
- a TIFF image with YCbCr subsampling and no JPEG compression.

### The chunk map

An `Ir`'s chunk map is one Arrow record batch with a row for each unit the source holds, of every
array with a grid. A unit the source does not hold has no row.

| Column | Arrow type | Null | Contents |
| --- | --- | --- | --- |
| `array` | `UInt32` | no | the array, by its position in `Ir::arrays` |
| `coords` | `List<UInt64>` | no | the unit's coordinates in the array's grid (empty for rank 0) |
| `element` | `UInt32` | yes | the IR element holding the unit's bytes; its recipe gives them |
| `member` | `UInt64` | yes | where `element` is in a run's subtree, the position j of the run's member it is in |
| `range` | `Struct<offset: UInt64, length: UInt64>` | yes | where the unit is part of `element`: its offset and length within the element's extent, its bytes in its own space |
| `ranges` | `List<Struct<object: Dictionary<Int32, Utf8>, offset: UInt64, length: UInt64>>` | yes | where no element holds exactly the unit's bytes: its bytes, in order; `object` is the object's key, null for the parsed source; `offset` counts from the object's start |
| `chain` | `UInt32` | yes | the unit's chain, a position in its array's `chains`; null for position 0 |
| `skips` | `UInt64` | yes | the codecs of its chain that the unit skips, a mask by codec index; null for none |
| `p.<codec name>.<name>` | as the codec's entry gives | yes | one per parameter any array gives as a column: set exactly on the rows whose array lists it in `unit_parameters` for the row's chain and that do not skip its codec; null elsewhere |

Further rules for the map:
- The schema's metadata gives `refkv.chunk_map` and the schema's version.
- Rows are grouped by `array`, in order, and within an array are in row-major order of `coords`;
  the IR sorts them so when it builds the map, and a parser adds them in any order.
- Exactly one of `element` and `ranges` is set in each row.
- Null is the only way to write chain 0 and "skips nothing".
- A rank-0 array has at most one row.
- Rows have their own limit (below). A map that would pass it is an error that rejects the run,
  never a map cut short, which a profile would read as units the source does not hold.

One batch per `Ir`, not one per array, because CZI subblocks and GRIB fields are each an array, and
a file can hold hundreds of thousands of them.

**Where a unit's bytes are.**
- **An element**, wherever the parser makes one:
  - HDF5's chunks (`objects/<i>/chunks/<idx>`);
  - TIFF's strips and tiles;
  - GRIB's section 7;
  - FITS's tiles, once `tile_cut` moves out.
- **Derived spaces.** An element in a derived space gives the unit's inflated bytes: the `data`
  child of a deflated `.npz` member and the `voxels` of a gzipped NIfTI, once they have them. Its transform is the IR's
  ([conventions §8.5](../../spec/conventions.md#85-forms-and-recipes)), not a codec. A unit inside
  a derived space is an element or a range of one, since `ranges` address only objects; an array
  whose units lie in a derived space past the IR's room has no grid (the general fallback).
- **A range of an element**, where the unit is part of one. A range survives a graft without
  object keys, and the check holds it to its element's extent:
  - a FITS tile within its heap element, including a `.fits.gz` heap in its derived space;
  - an HDF5 chunk of a dataset with an implicit index the parser does not make chunk elements of
    (it decides by what the image needs and by room), within the dataset's one `data` element;
  - an HDF5 dataset's compact data, within its layout message's `data` element.
- **`ranges`**, where no element holds the unit's bytes:
  - a unit past the IR's room;
  - an HDF5 external dataset's unit, in one or several files.
- **Objects** are named by key: null for the parsed source itself, and an HDF5 external file by the
  sibling key the run resolves for it, which is already its key in the store.

**HDF5's cases.**
- A contiguous dataset with an undefined address holds no unit, and has no row.
- A chunk's `skips` is its stored filter mask, bit i the i-th filter of the pipeline, which is codec
  i of the chain. Fletcher32 is a filter of the pipeline like the others.
- A partial edge chunk under the "do not filter partial edge chunks" flag skips every filter:
  `skips` has each filter's bit, although HDF5 stores its mask as 0. The stored mask stays in the
  chunk index's bytes in the IR.
- Mask bits past the pipeline's length name no filter; the HDF5 library ignores them, and so does
  `skips`. HDF5's PR checks whether any file sets them.

**Why Arrow.** An HDF5 dataset can have millions of chunks. As JSON, the map would be text that
every binding copies and parses. As Arrow, it is a few typed columns:
- **Python** receives an object that implements the Arrow PyCapsule interface
  (`__arrow_c_array__` and `__arrow_c_stream__`). pyarrow, polars and nanoarrow read it without a
  copy, so refkv itself needs no pyarrow.
- **JavaScript**, when something there needs the map, reads Arrow IPC with `apache-arrow`.

A parameter that varies by unit is a typed column, such as a FITS tile's ZSCALE as a float64. The
chunk map replaces profile-plugins' `parsed.chunks(path)`
([profile-plugins.md](profile-plugins.md#the-description)), which it generalizes to every format.

### Codecs

A chain lists every step the source encodes a unit with, in encoding order. Decoding runs it from
last to first, on the unit's bytes as its element or ranges give them, and ends with the elements'
bytes:
- **As the source stores them,** in its byte order, packed as `item_bits` and `pad_from` say, with
  bits most significant first within a byte.
- **Or as a codec makes them.** Some codecs make the elements rather than restore them: GRIB's
  unpacking, FITS's dequantization, JPEG's decoding. Each such codec's entry states the bytes it
  makes, and the profile types them from that entry. GRIB's unpacking makes little-endian float64
  values, NaN at a point its bitmap masks, as `refkv.grib` decodes today.

Every codec takes bytes to bytes, so HDF5's filters, nbit and scaleoffset among them, keep their
pipeline order.

**A name is one byte format.** Each entry states the exact bytes its decoding takes, framing
included, and a name never covers two framings: `zlib` is RFC 1950, `deflate` is raw RFC 1951,
HDF5's LZ4 filter (`hdf5_lz4`) writes blocks behind its own header rather than an LZ4 frame, and
LZF has no header at all.

**The list.** A codec's name is one of `spec/stored-array.md`'s list of the IR's codecs. Each entry
gives:
- its parameters, and which of them may be unit columns;
- what its decoding makes;
- for a parameter made from a `Source`, how it is made from those bytes.

Step 1 lists the codecs NumPy needs (none, once a deflated member's data is an element in its
derived space). Each format's PR adds its own; the list as it will grow:
- **Framing and compression:** `zlib`, `gzip`, `deflate` (raw), `lzw`, `packbits`, `zstd`, `jpeg`;
- **Bytes added by the source:**
  - `fletcher32`: encoding appends the checksum, decoding checks it and drops it;
- **HDF5's filters:** `shuffle`, `nbit`, `scaleoffset`, `szip`, and the registered filters refkv
  decodes (`lzf`, `hdf5_lz4`, `bzip2`, `blosc`, `bitshuffle`);
- **Bit order:** `bit_reverse`, TIFF's FillOrder 2, the last step of encoding and the first of
  decoding, on the stored bytes, for the compressions libtiff reverses for; JPEG ignores FillOrder,
  and the fax codecs take it as a parameter (TIFF refuses FillOrder 2 for most compressions
  today);
- **TIFF's predictors:** `tiff_predictor`, with `predictor` (2 or 3), `sample_bits`, `samples`,
  `row_length` and `byte_order`;
- **Later formats:**
  - FITS: `fits_rice`, `fits_gzip`, `fits_hcompress`, and `fits_quantize` (ZQUANTIZ, ZSCALE, ZZERO,
    ZBLANK and the dither seed);
  - GRIB: `grib_unpack`, with sections 3, 5 and 6 and the scanning mode;
  - CZI: JPEG XR and zstd's hi/lo split;
  - TIFF: CCITT and JPEG 2000.

The IR uses refkv's own names because it describes the encoding, not the output. Which Zarr or
numcodecs codec stands for it is the profile's.

**The general fallback.** A source encoding refkv has no name for is the codec `source`, carrying
the format, the source's id for the encoding, its name where the source gives one, and its
parameters as the source gives them (an unknown HDF5 filter's id, optional name and client data). The profile maps it or leaves the array out. Its parameters are never unit columns: the list gives them no types, so a column could not be
typed, and two formats' `source` parameters of one name would collide in a store.

**What is not recorded:** parameters only an encoder needs, such as a zlib level or Blosc's block
size.

**GRIB's scanning mode** is a parameter of `grib_unpack`. The codec makes its rows run in one
direction (undoing alternate rows) and transposes where the mode says consecutive points run along
columns, as `refkv.grib` does today. The grid's axes are the field's rows and columns as section 3
lists them, or its points for a reduced grid. Which way they point on the earth is the profile's.

Units may differ from their array in three ways, each a column:
- **`chain`:** a unit encoded another way. A FITS image quantizes and compresses its tiles
  (chain 0: `fits_quantize`, then `fits_rice` or its declared compressor), but a tile stored in
  `GZIP_COMPRESSED_DATA` is the losslessly compressed values (chain 1: `fits_gzip` alone). Both end
  in the image's values as ZBITPIX declares them, big-endian, as `fits_quantize`'s entry states. One
  in `UNCOMPRESSED_DATA`, if FITS's PR lifts today's rejection of it, a third chain. A null pixel
  mask's chain is the compressor ZMASKCMP names. The parser picks the column per tile today but
  keeps only the tile's range; FITS's PR records the chain.
- **`skips`:** codecs the unit skips (HDF5's cases, above).
- **A parameter column:** a parameter whose value varies by unit, such as a FITS tile's ZSCALE. It
  is nullable, and set exactly on the rows whose array lists it for the row's chain and that do not
  skip its codec.
  - A parameter is given either in its codec or as a column, never both.
  - A codec whose parameter is a column appears once in its chain.
  - A codec sees only a unit's bytes and parameters, never its coordinates. A parameter that
    depends on the unit's position, such as FITS's dither seed (ZDITHER0 plus the tile's index), is
    a column the parser computes.

**Configuration elsewhere in the source.** A codec may need configuration that the source defines
once, apart from the units, for many units. Zarr's codecs cannot say this; refkv's can.
`Param::Source` names the place the parameter is made from, so the description says where the
configuration is rather than copying it. Examples:
- a TIFF IFD's JPEGTables, for `jpeg`;
- a GRIB field's sections 3, 5 and 6, for `grib_unpack`; with bitmap indicator 254, section 6 is an
  earlier field's;
- HDF5's fill value message, as `Fill::Given`.

The parser today reads only the fill value message (0x05), and folds an undefined fill, a default
one and an absent message into one "none". HDF5's PR tells the states apart, and reads the old fill
value message (0x04) where the new one is absent, as the HDF5 library does.

### Checked

`StoredArray::check` and the chunk map's check test what a projection computes on.

**Each description:**
- **The grid.** Its rank is the array's rank, and it is in canonical form:
  - An axis is `Each(n)` when all its units are `n` long, except, under `Short`, a last one cut by
    the axis's end. Otherwise it is a `List`: under `Full`, an axis whose last unit the source
    stores shorter than the others (10, 10, 5 over 25) stays a `List`, since `Each(10)` would say
    the last unit holds 10.
  - Under `Short`, `n` is the declared length clamped to the axis length (a TIFF's RowsPerStrip
    above ImageLength, often 2^32 − 1, is one strip of the image's rows). Under `Full`, `n` is the
    declared length, which may exceed it.
  - A `List` sums to the axis length. Its adjacent equal lengths form one run: `Repeat`, with a
    count of at least 2, or `One`.
  - Lengths are at least 1. An axis of length 0 has no units; it is `Each` of the declared length
    (an extendible HDF5 dataset's chunk length), or an empty `List` where the source declares
    none.
- **Chains.** At least one chain when `grid` is set, none when it is not; each a position in the
  chain table.
- **`axes`.** When given, a permutation of the rank.
- **No extent.** Where `shape` is `None`, `grid`, `axes` and `pad_from` are absent.
- **`item_bits`, `pad_from` and the fill.** `item_bits` is set when `grid` is or the fill is
  `Given`, and is at least 1.
  `pad_from` is below the rank, given only when `item_bits` is set and is not a multiple of 8. A
  `Fill::Given` value is ceil(item_bits / 8) bytes.
- **`unit_parameters`.** Each names a codec of its chain that appears once there, a parameter the
  codec's entry allows as a column, and no parameter the codec gives itself.
- **Keys.** `(element, index)` is unique among an `Ir`'s arrays, and on each element `index` is
  either absent from every description or set on every one, as 0 to n − 1. Which elements carry
  indexes is the format's (its document names them), not the core's check.

**The chunk map:**
- its schema matches;
- rows are only for arrays with a grid;
- coordinates are of the array's rank, inside its grid, grouped and ordered as above, with no
  repeats;
- exactly one of `element` and `ranges` is set in each row;
- `member` is set exactly when `element` is in a run's subtree, and is below the run's count;
- chain 0 and no skips are written as null;
- `chain` is in range, and `skips` names only codecs of the unit's chain;
- each parameter column is set exactly on the rows whose array lists it for the row's chain and that
  do not skip its codec;
- every `element` is an element of the IR;
- in a file IR, an `object` key may be null or any key (an external file's key is checked when the
  file is grafted); in a store IR, it is never null, and names one of the store's objects.

Both checks run when a parser adds its descriptions and map to its finished IR (a separate step
after `check::finish`, which built them from), and again in the `check::finish` of any store the
file is grafted into. A description or a row they refuse is a bug in its parser: an internal error
naming the array's element.

## Where it lives, and how it travels

**Built at finish.** A parser keeps what it learns about its arrays to itself while it reads, as
it keeps an HDF5 chunk index today. When its IR is finished (after its last fold and rollback, and
after `check::finish`), it builds a `StoredArray` for each array it reaches and the chunk map's rows
from the finished IR, and adds them to the `Ir`: `Ir::arrays: Vec<StoredArray>`, the chains they
share, and the map. They are not in the facts. So:
- **No signature changes.** `Parser::finish`, `run.rs` and the store parsers keep returning
  `(Ir, J)`, and every projection already takes the `Ir`.
- **Only two things change ids afterwards.** A graft renumbers them with the elements it carries
  (below). A store's own `check::finish` after grafting may unfold a run, giving its members after
  the first new ids; the unfold moves each row's and place's element in member j's subtree to its
  new id and clears `member`. Fold and rollback happen only while reading, before anything is built.
- **Plain columns, Arrow on export.** The IR holds the map as Rust column vectors, sorted by
  `(array, coords)` when built, and makes the Arrow batch only when Python asks for it. Arrow is
  therefore a dependency of the Python binding alone. `mirror::canonical`, which takes `&Ir` and
  clones it to unfold every run, clones it without the arrays and map (they sit behind a shared
  pointer the clone drops), so no caller changes.
- **Shared chains.** The `Ir` holds one table of distinct chains, and a description names its
  chains by their positions in it, so hundreds of thousands of CZI subblocks with the same encoding
  share one chain. `StoredArray.chains` is, in the type, `Vec<u32>` into that table. A chain with a
  `Param::Source` is shared only by arrays whose parameter is at the same place, which is usually
  none: HDF5's nbit and scaleoffset take client data the HDF5 library computes for each dataset
  (from its datatype, and the chunk's element count or the fill value), and each GRIB field has
  sections of its own. Such an array has a chain of its own, a few places long, which the
  descriptions' limit counts. Copying the bytes into a `Value` would share more chains at the cost
  of duplicating source bytes, which the description avoids (see the alternatives).
- **They count against limits.** The `Budget` counts descriptions beside records and elements, at
  2^22 entries plus one per 4 bytes of the source. Chunk map entries (each row, each coordinate and
  each range) count against their own limit, apart from the IR's room: 2^22 plus four per byte of
  the source, each growing with each object of a store as its other budgets do. A unit can cost a
  single byte of the source: the HDF5 spike's (below) 4 MB file of 2000 × 2000 one-byte chunks
  under an implicit index parses today, and HDF5 bounds its chunk count only by 2^53 − 1. Every
  unit the source holds has at least one byte of its own, or an index entry of several, so a map
  of rank 3 or less always fits. The cost is the memory: about 109 bytes per rank-2 row as built,
  about 36 per entry. `memory()` includes both. `digest()` covers the descriptions and each row's
  values, not the Arrow layout: the columns in a fixed order (the named ones, then the parameter
  columns by name) and a dictionary's keys as their strings.
- **The facts' `arrays` key stays** as NumPy, safetensors and IMS use it today, until each format's
  PR removes it.

The mirror does not write them; a later note decides whether it should.

They cross the bindings with the `Ir`:
- **Python.** `PyIr` gives the descriptions as one JSON document (the chain table and the list),
  which Python reads with narrowing helpers, as `refkv._narrow` reads facts, and the chunk map as a
  PyCapsule object.
  `project(format, ir, facts, url)` gets them back with the `Ir`, so nothing is imported back into
  Rust.
- **JavaScript** gets nothing new. The app parses and projects inside the WebAssembly module
  (`vz_run_*`, `vz_store_*`). Only the module's test harness reads facts (`vz_finish`). Exporting
  the map waits until something in JavaScript reads it.

**Its schemas.** `spec/stored-array.md` specifies:
- the description;
- its JSON Schema, `spec/stored-array.json`, which each format's description schema refers to for
  its arrays;
- the chunk map's Arrow schema, as above;
- the list of the IR's codecs.

A `Param` is tagged in JSON (`{"value": …}` or `{"source": …}`), so a value that is an object cannot
be read as a source.

**On graft.** `Ir::graft` carries the file's descriptions and chunk map rows into the store:
- `element`, every `Place::Element`, and the `element` column take their new ids;
- `array` takes the description's new position;
- parameter columns of the same name join, having the type the codec's entry gives;
- the parsed source's null key, in every `Place::Extent` and in `ranges`, becomes the file's key in
  the store.

`placed()` does the same for HDF5's store facts today.

**The forms.** A unit element's form keeps its per-format `geometry` and `codec` in the mirror,
which writes them for readers of the mirror. Projections compute from the description, not from
forms. A later note may write the forms from the description.

**In the build tools' terms.** A `StoredArray` is the part of a format's description that says how
one array is stored. This changes two things in profile-plugins:
- **Its list of HDF5's convention decisions** keeps the data type, fill value presentation,
  dimension names, phony dimensions, string and record presentation, and the Adler-32s of skipped
  filters. The codecs move into the description, and the plan translates them to Zarr's.
- **The chunk map replaces `parsed.chunks(path)`.**

## Order of work

1. **The type, its schemas and its plumbing.** Nothing in this step needs profile-plugins. It adds:
   - `array.rs`, with `StoredArray`, both checks and `renumber`, and their tests (one of valid
     descriptions and maps, one per refusal);
   - `spec/stored-array.md` and `spec/stored-array.json`, with the codec list's form and the codecs
     NumPy needs;
   - Arrow (`arrow-array` with its `ffi` feature, and `arrow-schema`) behind a new `refkv-ir`
     feature that only `refkv-python` enables, so the WebAssembly module compiles none of it;
   - `Ir::arrays`, the chain table and the chunk map, built at finish, kept current by graft and
     unfold, and counted by `Budget`, `memory()` and `digest()`;
   - `PyIr`'s access to the descriptions and map, with the PyCapsule export and its test;
   - NumPy's parser describing its arrays (a `.npy` file's and each stored `.npz` member's),
     checked against NumPy's own reading of each header, with no output changed, so the type meets
     a real format at once. A deflated member gets no description in this step, an exception to
     "every array the parser reaches" that ends when NumPy's PR gives it `header` and `data`
     children;
   - the two edits to profile-plugins.

   Only NumPy emits descriptions yet, and no output changes.
2. **Each format, inside the build tools' format PRs.** Profile-plugins' step 3 reworks NumPy, then
   TIFF, then HDF5 into descriptions and plans, and its step 4 the others
   ([profile-plugins.md](profile-plugins.md#order-of-work)). Each of those PRs:
   - describes its format's arrays, adds its codecs to the list, and has its projection read them;
   - moves the typing its facts did (a fixed string's added axis, a default fill) into the format's
     plan;
   - gives unreachable declarations a place (NumPy's `.npz` headers; NIfTI's gzipped header);
   - lists in its format's document what each of its `declarations` places is;
   - removes the facts the description replaces, while the rest (such as HDF5's string spans) stay
     until the format's plan replaces them;
   - compares its outputs before and after, byte for byte, and keeps them unchanged apart from
     what profile-plugins' own step changes (sizing decisions by the largest source index); the
     mirror's forms may change, and each PR names those changes.

   Some format PRs carry extra work:
   - HDF5's strings and references are resolved by the plan, which needs the plan's reads
     (profile-plugins' step 2, before its step 3).
   - HDF5's PR tells the fill value's states apart, resolves a shared fill value message as the
     HDF5 library does, and treats a fill value whose size is not the element's as the library
     does. It checks each description against h5py, but not with HDF5 2.0.0's `get_chunk_info`
     for an extensible array whose unlimited axis is not the first: that call returns wrong
     offsets there (`ims_latest_indexes.ims:/Indexes/ea_last`), where HDF5's own reads agree with
     refkv's. It also makes sure the parser never enumerates more units than the map's limit before
     it refuses: the spike's parser built 1.76 GB of chunks first.
   - TIFF's PR replaces the JPEG prefix.
   - FITS's PR moves `tile_cut` to the projection and records each tile's chain.
   - GRIB's PR keeps its NaN at masked points, so its output is unchanged.

Step 1 lands before profile-plugins' step 3, so that NumPy's description, the first, uses the shared
type rather than an array shape of its own.

**What an HDF5 spike showed.** Before step 1 merged, a throwaway branch described every HDF5, NWB
and IMS dataset on step 1's type and checked each against h5py: shapes, chunk shapes, element
sizes, every chunk's offset, size and mask, the filter pipeline, fill values, and contiguous,
compact and external storage. About 1,100 chunks were also decoded and compared. Outputs stayed
byte for byte the same.
- **What fit:** grids under `Full` (chunks larger than the axis, a zero-length extendible axis),
  pipelines as one chain with masks as `skips`, declarations including committed datatypes,
  virtual datasets with no grid, external files as `ranges`, datasets past the room, and real
  multi-file grafts through a store's `check::finish`. NWB's 52 arrays shared 2 chains.
- **What changed here:** the map's limit, `shape`'s `None`, implicit-index and compact units as
  ranges of an element, the wording of `skips`, framing in codec names, the `source` codec's name,
  and finding a description from the facts.
- **Cost:** describing 10^6 chunks took about 0.27 s and 109 MB, and the Arrow export about
  76 bytes per row.
- **Not tested by HDF5:** unit parameter columns, chains past the first, `axes`, `pad_from`,
  `index`, and runs. HDF5's parser never folds, so unfold is tested only on synthetic IRs until a
  format whose parser folds describes its arrays.

## Not in this proposal

- **Coded streams.** MP4's tracks, MP3 and FLAC hold frames that:
  - are not independently decodable;
  - decode in one order and present in another;
  - decode to several planes.

  `refkv.stream`, which decodes a chunk a projection builds from frames and their lead-in, is an
  output encoding. A coded stream needs a description of its own kind, in a later note: its format,
  its setup's place, its frames with their sync flags and presentation times, and its decoded
  geometry per plane.
- **Anything a projection decides,** as listed above. `shard.rs` stays projection tooling.
- **The output documents** and any checking of them.
- **The store profiles.** Zarr v3, Zarr v2 (with OME-Zarr), N5 and precomputed read stores whose
  array documents already describe their arrays, in the lossless JSON value `Jv`. They keep them.
- **Zarr validity.** A description is not checked against Zarr.

## Alternatives considered

- **Typed data types in the IR.** These would be Zarr's names, plus refkv's for fixed-length bytes,
  heap references, structs, enumerations, HDF5's arrays, opaque, bitfield and time types. The IR
  would need a registry, and struct offsets and padding, and string padding and charsets. Every name
  is already a translation, and the IR computes only on an element's size.
- **Typing each format's facts on its own.** Each format's description would be checked, but the
  same facts about an array would stay spelled many ways.
- **Copying HDF5's `"array"` facts into the type.** They already hold output decisions.
- **A model of the output documents.** It puts output concerns in the core's array type, and it
  leaves the source's arrays untyped.
- **Zarr's codec names and category order in the IR.** HDF5's nbit and scaleoffset depend on the
  data type and can follow shuffle, so they fit no category order. And naming a source encoding
  after its Zarr counterpart is the profile's translation.
- **The chunk map as JSON.** Millions of chunks would cross every binding as text.
- **One Arrow batch per array.** Each batch has its own schema and buffers, and a CZI file or a GRIB
  file can hold hundreds of thousands of arrays of one unit each.
- **refkv's own binary columns,** like the WebAssembly module's little-endian range pairs. They need
  no dependency, but every consumer would write its own reader, to a format refkv would have to
  specify.
- **IR paths instead of element ids.** A graft re-roots paths just as it renumbers ids. The IR maps
  ids to paths but not paths to ids, and a run's members have no paths of their own.
- **A unit's codecs as skips only.** Skips cannot say that a FITS tile is gzip rather than Rice.
- **Quantization inside each FITS compressor.** It would repeat ZSCALE and ZZERO in every
  compressor, and leave unclear that the gzip fallback has none.
- **A codec's configuration from elsewhere in the source, copied or left out.** Copying it would
  duplicate source bytes in the IR. Leaving it out would leave a unit's decoding incomplete.

## Open questions

1. **The name.** `StoredArray` says what it describes. If the build tools call this part of a
   description something else, it takes that name.
