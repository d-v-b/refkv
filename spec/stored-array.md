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
declarations is. Today only the NumPy profile describes its arrays
([§7](#7-the-formats)).

## 2. The description

A description has these members (the Rust type `StoredArray` of `rust/refkv-ir/src/array.rs`):

| Member | Contents |
| --- | --- |
| `element` | the IR element that declares the array |
| `index` | the array's index among those `element` declares, for a kind of element that may declare several; else absent |
| `declarations` | the places where the source declares what the array's elements are, as the format's document lists them |
| `shape` | the axes in storage order, the slowest-varying first; absent where the source declares no extent (an HDF5 null dataspace), which holds no element, unlike a scalar's empty shape (the JSON form writes `"shape": null`, the member present) |
| `axes` | the source's own order of the axes, where it differs from storage order: `axes[i]` is storage axis i's position in the source's list (a Fortran-order NumPy array of shape (3, 4) has `shape` [4, 3] and `axes` [1, 0]); else absent, never the identity |
| `item_bits` | the bits of one element, decoded; absent only for an array with no grid and no given fill |
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

Each format's document says how a profile finds, from the facts its parser gives, the key of each
array's description: the declaring element's id, and the index where the format's kind of element
carries one. A profile so matches what its facts say of an array to the array's description,
presented or not. A graft does not touch the facts: a format whose files a store grafts renumbers
the ids its facts give in its store code, as the HDF5 store's `placed()` renumbers its
`store.element` ids. The declaring element is in no run's subtree ([§5.1](#51-each-description)),
so its id names one element, which an unfold leaves where it is.

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

- `undefined`: the source defines none (HDF5's "undefined", and a format that has no fill value);
- `format_default`: the source says its format's default (HDF5's "default", which the HDF5
  specification defines as zero bytes);
- `given`: one decoded element's bytes, ceil(`item_bits` / 8) of them, as a parameter
  ([§2.2](#22-places-and-parameters)): a value, the bytes as a base64 string (RFC 4648 §4, with
  padding), or the place that holds them (a range, an extent, or an element's whole extent). An
  element of fewer bits than whole bytes takes the first bits of its bytes, packed as the array
  packs them.

### 2.5 The JSON form

The descriptions cross the bindings as one JSON document, `{"chains": [...], "arrays": [...]}`:
the chain table, each chain a list of codecs `{"name": …, "parameters": {name: parameter}}`, and
the descriptions in the IR's order. A description has every member of [§2](#2-the-description),
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
chain; and a codec sees only a unit's bytes and parameters, never its coordinates, so a parameter
that depends on the unit's position is a column its parser computes.

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
  with LZ4 or leaves them bare, would be one entry, `h5_bitshuffle`); otherwise an encoding the
  sources frame two ways is two entries, and one HDF5 frames its own way has a name of its own,
  prefixed `h5_` (`h5_lz4`, `h5_szip`);
- its parameters, which of them a unit may give as a column and the column's Arrow type;
- what its decoding makes;
- for a parameter given as a place, how the parameter is made from those bytes.

A chain's codec has only its entry's parameters.

| Codec | Takes | Parameters | Columns | Decoding makes |
| --- | --- | --- | --- | --- |
| `source` | the bytes the source's encoding wrote, framed as the source frames them | `format`: the profile's name; `id`: the source's id for the encoding, as the source gives it; `name`: the source's own name for the encoding, where it gives one (an HDF5 filter's optional name); `parameters`: its parameters, as the source gives them (a value, or a place that holds them) | none | what the source's encoding decodes to; the profile maps it or leaves the array out |

`source` is the general fallback: a source encoding refkv has no name for (an unknown HDF5
filter's id and client data). refkv's names describe the encoding, not the output: which Zarr or
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
  lies within the element's bytes, and is not all of them (a unit of the whole element names the
  element alone, so that a unit has one spelling). A unit inside a derived space is an element or
  a range of one, since `ranges` address only objects. A row's ranges are at least one, each at
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
- **`item_bits`, `pad_from` and the fill.** `item_bits` is set when `grid` is or the fill is
  given, and is at least 1. `pad_from` is below the rank, given only when `item_bits` is set and
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
  and below 2^64, and not the element's whole extent;
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
in it; checks the descriptions' limit; sorts the rows; and checks them ([§5](#5-checks)). They are
not in the facts a projection reads, which keep their arrays until each format's projection reads
the descriptions.

**Only two things change ids afterwards.**

- **A graft** carries a file IR's descriptions and rows into the store it joins, in order:
  - it fits the file's arrays, in the file's order, to the store's chunk map limit as the store's
    grafts so far have it, this file's object included (below). A store has the 2^22 once, where
    each file had it, so an array that kept its rows in its file may fall back in the store,
    depending on what was grafted before it;
  - it carries into the store's chain table only the chains the file's remaining descriptions
    name, each with its places' element ids renumbered and then found in the table by its value:
    a chain the table has keeps its position, and a new one is appended, in the order the file's
    descriptions first name them. The table's existing chains and positions are untouched;
  - it checks the descriptions' limit, last; a graft that fails on it, or on the IR's room, adds
    nothing to the store.

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
once, plus four per byte of the source; a store's, as its grafts fit to it, 2^22 once, plus 4 and
four per byte for each object grafted so far. Every unit in space 0 has at least one byte of its own
(an HDF5 chunk of an implicit index may be a single byte), or an index entry of several, so where
no two units share bytes or index entries, a map whose units are held by elements of space 0 (with
or without a `range`) fits at rank 3 or less, and one whose units are in `ranges` fits at rank 2 or
less. A unit in a derived space can cost far less than a byte of the source, and has no such
guarantee; nor has a source whose structures share bytes or index entries (two HDF5 object headers
on one chunk index, TIFF IFDs sharing a StripOffsets array).

The chunk map's limit never rejects a run, and never cuts an array's rows short, which a profile
would read as units the source does not hold. Arrays are tried in `Ir::arrays` order; one whose rows
do not fit in what remains of the limit keeps none of them, and falls back: its description has no
grid, no chains and no unit parameters, and the map no row of it. Later arrays are still tried, and
the run goes on. In a file IR, which arrays fall back follows from the source alone; in a store,
from its files in graft order. The descriptions' limit, checked after the fit, still rejects the
run.

As the map is built, a row holds about 93 bytes of memory, a coordinate 8 and a `ranges` item 24;
the worst case, rank-1 rows of elements over a shared index at four entries per byte, is about 200
bytes per byte of the source. A parameter column holds about 16 bytes a row, null or not, for every
row of the map; the list allows few columns (none yet). Descriptions a check refuses spend none of
either limit.

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
