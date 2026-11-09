# Proposal: the tools to build an output from the IR

refkv's profiles turn a file into an output in two steps: a parser reads the
file into an **IR** ([conventions §8](../../spec/conventions.md#8-the-ir-mirror))
and a set of **facts**, and a projection builds the convention's hierarchy
from them. A projection is a Rust function of what the parser read, built
from helpers that cut ranges, join strips, choose codecs, shard chunks and
write OME metadata. Someone who wants another output of a format refkv
already reads, such as an OME-Zarr 0.6 image of a TIFF or of an HDF5 dataset,
or one TIFF file made of a stack of them, has none of this. They can only
start from what a projection already wrote.

The first answer, now on main as layout plugins
([spec/layout.md](../../spec/layout.md), designed in earlier versions of this
note), hands a plugin the built-in output (the **base**) with bridges into
the IR, and checks what the plugin returns. That shows the gap without
closing it. refkv can build an output from the IR and an external caller
cannot, because the caller lacks the tools the projection uses. This note
proposes making those tools public, so that a built-in projection is one
caller of them among others. Plugins then become a thin way to name a
function from the command line.

It is a design note. It replaces this note's earlier versions;
[What changes on main](#what-changes-on-main) says what of the plugin
implementation stays.

**The design criterion: what a built-in projection can build, a caller can
build with public tools.** Two tests check it
([Testing that the tools suffice](#testing-that-the-tools-suffice)):

- **Rebuilding refkv's own output.** A Python projection written only with
  the public tools, without the built-in output, run over the fixtures,
  writes an output equivalent to the built-in one, and rejects the inputs it
  rejects ([zarrify §1.1](../../spec/zarrify.md#11-output-and-equivalence)).
- **Building outputs refkv does not write.** OME-Zarr 0.6 images of a TIFF
  and of an HDF5 dataset, and a TIFF stack, are built without the built-in
  output and without restating any rule of refkv's conventions.

Total control of the layout, the earlier criterion, follows: a caller writes
the entries it wants.

## Terms

- **Profile**: refkv's reading of one format, named in an output's root
  (`tiff`, `ndpi`, `hdf5`). Its **document** is spec/zarrify/<format>.md,
  which specifies both the source model and the **convention**, the output;
  its **projection** is the code that writes that output from what the
  parser read. A **layout** is one presentation within an output (the image
  layout, a dataset layout).
- **Facts**: the JSON object a parser returns beside the IR today, the
  projection's other input; unspecified.
- **Element**: a node of the IR, with a path, a kind and an extent in its
  source ([conventions §8.1](../../spec/conventions.md#81-elements)). An
  element's **form** may carry a **recipe**, the parts that make its bytes
  whole, such as a JPEG tile's tables spliced before its scan
  ([conventions §8.5](../../spec/conventions.md#85-forms-and-recipes)). An
  **alias** is an element whose bytes are another's (`ir.target(i)`), such as
  a TIFF IFD that shares an earlier one's tiles. A **derived** element is one
  whose bytes are computed rather than a source's; an element in a **derived
  space** has its extent in the inflated bytes of another element (a member
  of a gzip stream).
- **Rounds**: the batches of reads a parser or a plan asks for, each after the
  last's bytes have come; a format bounds them (HDF5's 4096).
- **Fill**: the run's read, after parsing, of the values the IR's view shows
  that the parser did not keep ([conventions §8.7](../../spec/conventions.md#87-the-view)).
- **Gap**: bytes of a source no element claims; the IR's leaves and gaps
  together cover each source ([conventions §8.1](../../spec/conventions.md#81-elements)).
  **Room** is the bound a source model puts on how many elements an IR holds,
  past which more of a source is gaps
  ([hdf5.md §7.3](../../spec/zarrify/hdf5.md#73-the-ir-mirror)); the **rows
  budget** bounds the IR mirror's table
  ([zarrify §1.1](../../spec/zarrify.md#11-output-and-equivalence)).
- **File run** and **store run**: refkv's reading of one file, and of a store
  (a directory or prefix of objects, [zarrify §1.4](../../spec/zarrify.md#14-store-inputs)).
  A source is read under the **reader policy**, which bounds what it may
  name and read, and its **pins**, its size and entity tag as first found
  ([spec/refkv.md §5.1](../../spec/refkv.md#51-pins),
  [§6.7](../../spec/refkv.md#67-reader-policy)).
- **Payload**: a reference's encoded size, at most 65519 bytes; a source
  index above 0 is a varint in it ([zarrify §1.2](../../spec/zarrify.md#12-input)).
  The **zero data source** is the data source of zero bytes padding draws
  on.
- **Structure only**: the rule of [zarrify §1.2](../../spec/zarrify.md#12-input)
  that an output does not depend on a file's pixel data, apart from what a
  convention specifies (its `omero` windows, sampled from decoded chunks).
- **Written**: an output's entries together with their placement (which
  bytes entries a vzip reader reads at open, and which are stored
  uncompressed, [spec/layout.md §3](../../spec/layout.md#3-the-output)).
- **Rejected**: the error that refuses an input, `refkv.zarrify.Rejected`.

## The gap

A built-in projection is given what the run that read the file has. A caller
has, today:

| what a projection uses | what a caller has today |
|---|---|
| **The IR**, with every element's path, kind, extent, form and value | `refkv.ir.parse(fmt, transport)` returns it with the facts and the planner's counts, unprojected, as the core's `Ir`, undocumented, for a file run; a store run has `StoreRun.finish` in the core and no such wrapper. The supported way, `layouts.base(path)`, runs the whole projection first and refuses store inputs |
| **The facts**: what the parser decoded (TIFF's sample format, compression, OME channels, pyramid levels; HDF5's nodes) | `parse` returns them, unspecified, changing with every projection change. [spec/layout.md §2](../../spec/layout.md#2-the-base) withholds them from plugins, which copy the decoded result from the base's documents instead |
| **The convention's decisions**: how planes map to IFDs and which IFDs are levels, which strips join and how a level shards, which HDF5 datasets are presented and why the others are not, their dimension names | Only as the base's documents |
| **Bytes read for the output**: edge chunks copied into it ([conventions §7](../../spec/conventions.md#7-source-values-as-arrays)), chunks sampled for `omero` windows ([tiff.md §4.4](../../spec/zarrify/tiff.md#44-arrays)), the Adler-32 of an HDF5 chunk a filter skipped ([hdf5.md §3.2](../../spec/zarrify/hdf5.md#32-chunks)), an LZW strip's tail ([tiff.md §4.5](../../spec/zarrify/tiff.md#45-strips)), HDF5 strings and records ([hdf5.md §4](../../spec/zarrify/hdf5.md#4-variable-length-strings)) | Nothing |
| **The source table**, each source opened under the reader policy and its pins, with its size; data sources numbered as [zarrify §1.2](../../spec/zarrify.md#12-input) says | `Base.sources`, the base's table, through a base only |
| **The helpers** of `rust/refkv-ir/src/out.rs`, `shard.rs` and `project/` | the codec routines of `refkv.zarrify.coding`; `cut`, `concat`, `payload_size`, `chunk_key`, `copy`, `array` and `group`. Virtual sharding, joining strips, framing HDF5's masked chunks, the OME and `omero` builders and the mirror are Rust's; the grid cut is Rust's and an internal Python port (`refkv.zarrify.common.grid_chunks`) |

Four gaps follow from it.

- **The IR does not describe what the facts do.** In the HDF5 IR, every object
  header message but attributes and links (dataspace, datatype, layout,
  filters, fill value) is an opaque `bytes[n]` leaf
  (`rust/refkv-ir/src/hdf5/walk.rs`); the dataset's shape, type and filters
  are only in `facts.nodes`. A TIFF's OME-XML is an element with an extent,
  but its value is not decoded in the IR beyond 1024 bytes
  ([tiff.md §5.3](../../spec/zarrify/tiff.md#53-values)), while the facts hold
  what [tiff.md §3](../../spec/zarrify/tiff.md#3-ome-xml) reads from it. The
  rules that make the facts are specified; their result is not.
- **The parsers make the convention's decisions, and read for them.** The
  TIFF parser maps planes to IFDs, scans for levels, decides which strips
  join into a chunk, when to shard and how to pad (`join_strips`,
  `shard_strips`, `pad_strips` in `rust/refkv-ir/src/tiff.rs`), reads an LZW
  strip's head to build its tail, and converts the image's position (OME's
  center, Aperio's corner, NDPI's offset from the slide's center) to the
  corner the convention uses; it reads every IFD's strip and tile tables
  for the mirror, and checks their values only for the IFDs those decisions
  use ([tiff.md §2](../../spec/zarrify/tiff.md#2-the-source),
  [§5.3](../../spec/zarrify/tiff.md#53-values),
  [§7.1](../../spec/zarrify/tiff.md#71-reading)). The HDF5 parser decides
  which datasets are presented (`decide()` in
  `rust/refkv-ir/src/hdf5/file.rs`), writes each one's Zarr data type,
  codecs, fill value and dimension names, reads a masked chunk's Adler-32
  (`read_skipped`) and the strings of the presented datasets, and describes
  their storage whatever the room, other objects only while room lasts
  ([hdf5.md §7.3](../../spec/zarrify/hdf5.md#73-the-ir-mirror)). So which
  elements are in the IR depends on the output.
- **Nothing vouches for a store a caller builds.** `ReferenceStore(mapping,
  sources)` checks source indexes and key types, not that a range lies
  within its source or that a payload fits. `prepare` and `store_of` check
  that, but only for a plugin's output against one base.
- **One input at a time.** A base is one file's. The TIFF stack of
  d-v-b/refkv#282 (docs/tiff-concat.md there) needs several, and so re-reads
  every IFD with tifffile, which refkv had already parsed.

The earlier versions of this note answered the first gap by handing over the
base as the description of the file. That keeps every external output
downstream of refkv's own: a caller can present what the projection
presented, differently, and reach past it only through the IR's raw
elements.

## Known and unknown

A profile's document specifies each structure's syntax: the IR element, its
type, and so its typed value, `ir.value(i)` (the **source model**,
[conventions §8](../../spec/conventions.md#8-the-ir-mirror)). For some
structures it also specifies their meaning. That gives each structure one of
two kinds:

- **Known**: a structure whose meaning refkv's model of the format
  specifies, in the profile's document. The known set is refkv's choice, not
  the format standard's. A TIFF's known structures are the tags its
  document's table lists ([tiff.md §2](../../spec/zarrify/tiff.md#2-the-source):
  byte order, dimensions, sample format and bits, samples, compression,
  predictor, planar configuration, photometric interpretation, fill order,
  YCbCr subsampling, JPEG tables, resolution, strip and tile tables,
  SubIFDs, EER's tags), the attributes of OME-XML that
  [tiff.md §3](../../spec/zarrify/tiff.md#3-ome-xml) reads (`Image`, `Pixels`,
  `Plane`, `Channel`, `TiffData`, `UUID`, today of the first `Image`), and its
  ImageJ, Aperio, NDPI, QPTIFF and Micro-Manager metadata as the document
  reads them. An HDF5 file's are a node's dataspace, datatype (compounds,
  [hdf5.md §3.3](../../spec/zarrify/hdf5.md#33-compounds), included), layout,
  chunk index with its filter masks, filters, fill value and external data
  files ([§8.6](../../spec/zarrify/hdf5.md#86-external-files)), its links
  ([§2.3](../../spec/zarrify/hdf5.md#23-links)) with the link info message's
  creation order, its attributes ([§6](../../spec/zarrify/hdf5.md#6-attributes),
  variable-length strings among them, and why one is left out), and the
  storage of strings and object references
  ([§4](../../spec/zarrify/hdf5.md#4-variable-length-strings),
  [§5](../../spec/zarrify/hdf5.md#5-object-references)). The interpreted
  result is the format's **description**.
- **Unknown**: everything else in the source: a TIFF tag the table does
  not list (a private tag, an UNDEFINED one), an HDF5 message type the document does not decode, a CZI attachment of a schema [czi.md](../../spec/zarrify/czi.md) does not decode,
  an MRC extended header. An unknown structure is never interpreted into an
  array or an attribute of an output; it is in the IR mirror like every
  element. A caller gets its typed value (`ir.value(i)`) where its type has
  one, its bytes (`parsed.bytes(i)`, [References](#2-references-and-values)),
  and a reference to its extent (`parsed.span(i)`). Past the room, an
  unknown structure is in a gap, which `parsed.gaps()` lists as ranges of
  their sources. A caller that needs an unknown's meaning decodes it. Part of
  a known structure that the model does not read, such as an OME-XML element
  [§3](../../spec/zarrify/tiff.md#3-ome-xml) skips, is not an element of its
  own: a caller parses it from the known structure's bytes
  (`parsed.bytes` of the OME-XML's element).

The built-in projections are built on the known because it is known: a
projection relies only on what the profile's document specifies, and an
external projection relies on exactly the same, through the same
description. refkv's model of a format grows only by a change to the
document and the description, tested against an independent reader of the
format (tifffile, h5py), which lands before any projection relies on the new
structure: TIFF's first change of this kind extends [§3](../../spec/zarrify/tiff.md#3-ome-xml) to read every
`Image`, as it reads the first. A projection never makes a structure known
by using it. A structure nobody has modeled stays unknown, and its bytes are
still there.

### The description

The description of a format is its known structures, interpreted: specified
in the profile's document beside the rules that interpret them, with a JSON
Schema in `spec/zarrify/<format>/description.json`, versioned with the
profile's source model (which today shares one version with its
convention). It names a structure by its IR path where it has one
([tiff.md §5.1](../../spec/zarrify/tiff.md#51-elements)), and gives its
extent where it may lie past the room. Where a known structure is large,
such as an HDF5 dataset's chunk index, the parser walks and checks it as
today, under the budgets of [hdf5.md §8.3](../../spec/zarrify/hdf5.md#83-checks),
and keeps it apart from the description's JSON and from the IR's room, in
the IR's chunk map ([spec/stored-array.md §4](../../spec/stored-array.md#4-the-chunk-map)):
a row for each unit the source holds, of every format, with its
coordinates, its element or ranges, and its chain and skips (an HDF5 chunk's
filter mask). The chunk map replaces `parsed.chunks(path)`. How one array is
stored (its shape, element size, grid, codecs, fill value and declarations)
is the part of a format's description that a `StoredArray` gives
([spec/stored-array.md §2](../../spec/stored-array.md#2-the-description)).

Today's facts are where each format's work starts, not what defines it.
Each profile's document lists its known structures first; then every item
of its facts is checked against the list, and is one of:

1. **Derived from a known structure**: it stays, in the description. TIFF's
   `facts.plan`, the planes that lie in other files, is one: it is read from
   the set's OME-XML or Micro-Manager metadata
   ([tiff.md §7.4](../../spec/zarrify/tiff.md#74-several-files)).
2. **A decision of the convention**: how planes map to IFDs and which IFDs
   are levels ([tiff.md §4.1](../../spec/zarrify/tiff.md#41-planes),
   [§4.2](../../spec/zarrify/tiff.md#42-pyramid-levels)); which strips join
   into a chunk, when to shard and how to pad (TIFF's `join`, `shard`, `pad`
   and `rectilinear` per level, and the LZW tails padding needs); a level's
   scale as a ratio, and the position converted to the image's corner; which
   HDF5 datasets are presented and why the others are not, and each one's
   Zarr data type, fill value presentation and dimension names (with HDF5's
   phony dimensions, counted across the file,
   [hdf5.md §3](../../spec/zarrify/hdf5.md#3-datasets)), its string and record
   presentation (the strings and records a presented dataset reads), and the
   Adler-32s of the chunks a filter skipped. A dataset's codecs are not among
   them: they move into its `StoredArray`
   ([spec/stored-array.md §3](../../spec/stored-array.md#3-codecs)), and the
   plan translates them to Zarr's.
   It moves into the convention's **plan**
   ([Planning](#3-planning-plan)), which the built-in projection and a caller
   both call; a decision that reads bytes reads them there.

An item that is neither waits for the document to model it, as for any
structure refkv decides to understand, and that change lands first. Every
decision a built-in output makes is a rule its convention must already
state ([zarrify §2](../../spec/zarrify.md#2-the-zarr-layout) requires the
exact layout the convention specifies); one it does not is stated in the
same change. Once a format's items are sorted, its facts are gone: its plan
is a Rust function of the description (with the chunk map, the part of it
kept apart), of the IR's elements the description names (their paths,
extents and forms, through a view of the IR limited to them), of the
sources' sizes, of the bytes it reads, and of the run's options
(`checksums`, the test-only setting below), and nothing else. It reaches the known only
through the description, so a plan cannot depend on what is not specified,
nor on an unknown element; a test of each format's plan checks that every
range it reads lies in an extent the description names or a unit of the
chunk map (an `omero` sample, a masked chunk's Adler-32), or was located
from bytes it already read of one (a heap collection an HDF5 string names).
For a zarr3 or SAFE store, the keys its mirror records as ignored, which
today sit in the facts, are part of the description. The facts'
test hook, `copy_threshold` (in the facts of every format whose projection
copies edge chunks, overriding `MAX_PAYLOAD` of `rust/refkv-ir/src/out.rs`),
becomes a test-only setting of the plan.

The parsers' checks sort the same way, each moving with what it checks. What
stays in the parser, and `read` raises, is a check of the file's own
structure: for TIFF, the header, the IFD chain and SubIFDs, entry counts and
field types, values within the file, and the source model's budgets
([tiff.md §7.1](../../spec/zarrify/tiff.md#71-reading)) for every IFD the
parser walks; for HDF5, the walk, the chunk indexes and their budgets
([hdf5.md §8.3](../../spec/zarrify/hdf5.md#83-checks)). What moves into the
plan is a check of a decision: TIFF's checks of the IFDs a plane or level
uses (their formats, sizes and tables, [tiff.md §2](../../spec/zarrify/tiff.md#2-the-source)),
its `translation` and padding checks, its LZW heads' total against the
file's size; the checks of [hdf5.md §8.3](../../spec/zarrify/hdf5.md#83-checks)
about presented datasets (their storage, their strings, the 2^26 bytes a
dataset of strings, references or records may take, the 2^20 strings a file
may present); and every
rejection a convention's layout makes ([tiff.md §7.2](../../spec/zarrify/tiff.md#72-rejection)).
Each format's change lists its checks by side, item by item.

With the decisions out of the parser, the IR's room stops depending on the
output: the HDF5 walk keeps objects and their storage in canonical order
while room lasts, and reads no dataset strings (it still reads the global
heap objects of variable-length string attributes, which are known
structure). An HDF5 mirror then holds fewer storage elements past its room
than today, and no global heap collections read only for a dataset's strings
or records. A
presented dataset's chunks come from the chunk map, whether or not they
are IR elements, so the references budget of
[zarrify §1.2](../../spec/zarrify.md#12-input) counts a repeat by its source
range, not by an element's extent, and `project`'s `elements(path)` gives the
chunk index's entry for a chunk past the room.

A node's **source metadata** ([conventions §2](../../spec/conventions.md#2-attributes)),
which its output declares today, is then a function of the description and
the plan (HDF5's `unpresented` reasons,
[hdf5.md §7.4](../../spec/zarrify/hdf5.md#74-what-is-not-presented), are the
plan's, and so are an attribute's reasons that depend on the output, its
JSON `budget` and `reserved`; the reasons that are the file's, `shared` and
`datatype`, are the description's), given by
`source_metadata(plan, path)`: one interpretation, written in two places,
not two.

The earlier version of this note rejected specifying the facts as "a second,
unversioned description of each format beside its convention", the base
output being that description already. Versioned with the convention, and
the projections' input, the description is not a second one: it is the one
the output and its source metadata are derived from.

## The tools

Five groups, each the public form of something a projection already has or
does. The names are proposals. The new module is `refkv.build`, whose
functions are bindings of the core's, so that the Rust CLI and the browser
build with the same ones; what exists stays where it is
(`refkv.zarrify.coding`, `refkv.zarrify.layouts.hierarchy`).

### 1. Reading: `read`

```python
with refkv.build.read("image.ome.tif") as parsed:     # or read_async
    parsed.profile, parsed.version                   # "tiff", 0 (an NDPI file is "ndpi")
    parsed.ir                                        # refkv.ir.Ir: at, index, children, value, target, raw
    parsed.description                               # the format's description
    parsed.sources                                   # tuple[refkv.Source], each url source with its size
```

`read(location, url=None, *, policy=None, registry=None, checksums=False,
max_bytes=64 << 20)` reads a file, or a store, as `zarrify` does (the same
sniffing, siblings, pins, reader policy and object stores), with the run's
fill of the values the IR's view shows that the parser did not keep, and
stops before the plan. It returns a `Parsed`, which keeps the run's
transport open for the reads below until it is closed. `parsed.sources`
lists every source the run opened, the siblings it found among them with
their sizes. A file read as a store (VSI, NRRD, an HDF5 file with external
storage) gives the multi-source IR of
[conventions §8.9](../../spec/conventions.md#89-stores), and so does a store
input, through `StoreRun.finish`. `read` raises `Rejected` for the parser's
own checks ([The description](#the-description)), and, once a format's plan
has landed and its IR no longer depends on the output, for the rows budget;
until then the rows budget is checked where it is today. `checksums` is the run's,
fixed here because a shard's ranges join differently with them
([zarrify §2.1](../../spec/zarrify.md#21-virtual-shards)), and every value
built from a `Parsed` follows it. `max_bytes` bounds the total
`parsed.bytes` reads over the `Parsed`'s life; the plan's reads have their
convention's bounds instead. The rounds of the parser's reads count toward
the rounds a format bounds for a run (HDF5's 4096,
[hdf5.md §8.3](../../spec/zarrify/hdf5.md#83-checks)), which its plan
continues. `parsed.gaps()` lists the gaps, as ranges of their sources, and
the IR's chunk map every array's units
([The description](#the-description)).

### 2. References and values

The IR accessors of `Base` move to `Parsed`: `parsed.reference(i,
member=None)` (an element's bytes as the profile reads them, its recipe
applied), `span(i, member=None)` (its own bytes, no recipe), `members`,
`form`, `element_at` and `source_of`. An alias is read by its own extent,
which lies within its target's. `reference` and `span` refuse a node and a derived element, and
`reference` a chunk whose form skips a filter, which the plan frames. An
element in a derived space (its extent in the inflated bytes of another
element) has no `span`, which is a range of a source.

`parsed.bytes(i)` is an element's own bytes: from the IR where the parser
kept them (`ir.raw`), otherwise read through the input's transport, and for
an element in a derived space, inflated from the element that holds it. It
takes any element with bytes, known or not, pixels included, within
`max_bytes` (bytes the IR already holds do not count against it);
`parsed.bytes(*indexes)` reads several in one round, and
`parsed.bytes((source, offset, length))` a range, such as a gap's. An alias's
bytes are its own extent's; its `ir.value` is its target's. Decoding an unknown
structure needs its bytes, and whether an
output may depend on what it reads is the output's rule, not the tool's
([Building and writing](#4-building-and-writing)).

They return **values**, whose parts name what they read, so that numbering
the source table is left to `seal`:

- `Ref(parsed, source, offset, length)`: bytes of a url source of an input,
  `source` an index of `parsed.sources`;
- `Shared(data)`: a byte string the convention names as shared (a JPEG
  tile's tables), which `seal` makes a data source read whole, one per
  distinct string; routines make them, and a caller may too;
- `Zeros(n)`: zero bytes, of the zero data source;
- `Data(data)`: a data source of the caller's own, of which a value may take
  any range: `cut(Data(blob), a, b)` is bytes `a` to `b` of that one source;
- `Literal(data)`: bytes in the reference itself, counted in its payload;
- `Copy(ref)`: the bytes of a `Ref` that must be copied into the output, as an
  edge chunk too large to pad is ([conventions §7](../../spec/conventions.md#7-source-values-as-arrays)):
  a caller fills it with `parsed.bytes`, and `seal` refuses one left
  unfilled (code `copy`).

A value is one part or a sequence of them, in order. `cut`, `concat`
(`merge=True` joins ranges that continue one another) and `fits` take
values. Where a decision or a check needs a payload before `seal` has
numbered the sources, it measures every url source index, 0 included (with
several inputs, `seal` may number a file's source above 0), and every data
source index as 5 bytes: its field tag and the varint of 2^22 − 1, an output's
table holding at most 2^22 sources, a constant
[zarrify §1.2](../../spec/zarrify.md#12-input) gains (it is the default
`max_sources` of [spec/refkv.md §6.7](../../spec/refkv.md#67-reader-policy),
and `seal` refuses a larger table whatever the policy); and the checksums at their
largest when the input was read with them: `fits` does, and so does every decision of a plan sized by
a payload (an edge chunk's copy, an HDF5 strings chunk's items, an LZF
chunk's framing, a shared JPEG table's reference). It may refuse a value
`seal` would write, never the other way.

`Array(shape, data_type, chunk_shape, codecs, chunks, fill_value=None,
dimension_names=None)` is an array: chunks keyed by coordinate tuples, each a
value or bytes (a copied edge chunk, an HDF5 record), `dimension_names` in the
array's axis order. Values carry no rule of their own: how many reads or bytes
one value may take is its reader's limit
([spec/refkv.md §6.7](../../spec/refkv.md#67-reader-policy) rule 5, `max_reads`
and `max_get_bytes`), not the writer's, since another reader may allow more.
Several conventions read more than 1024 ranges for one chunk (an HDF5 strings
chunk, an MP4 stream's chunk of samples, a VSI image of many strips); `seal`
writes them, and a reader under the default policy refuses that one `get`.

`hierarchy.array`'s `dimension_names`, when given, replace the `Array`'s.
A caller that presents an array's axes in another order uses Zarr's
`transpose` codec: it puts one in front of the array's codecs (merged with one
already first into a single codec, as `copy` merges them), and writes the
shape, the chunk grid and the chunk keys in the new order, as Zarr v3 defines
the codec (its `order` names, for each stored axis, the presented axis it
holds, so it is the inverse of the permutation that takes the stored axes to
the presented ones). That is Zarr's rule, not refkv's, and the chunks keep
their values.

`Written` is a mutable mapping of keys to bytes or values, with the
placements of what it holds: `written[key] = value` adds an entry, and
`written | other` and `written |= other` (another `Written` or a plain
mapping) keep every placement. Checks travel with the values, so a plain
`dict` loses only placements; an output that sets any starts as a
`Written()`.

### 3. Planning: `plan`

A projection today makes a convention's decisions for a whole input at once,
with one batch of reads, and some of them span the input (HDF5's phony
dimensions are counted across groups; a group's source metadata names why
each child is not presented; the 2^20 strings and the 4096 rounds are a run's;
TIFF's LZW heads are bounded together). `plan(parsed)` is that step, made
public: the convention's decisions for the whole input, as data, not as Zarr
entries. It is computed once per `Parsed`: a second call returns the same
plan, so its reads, rounds and findings do not depend on how often, or after
what, it is called. (`refkv.zarrify.layouts.plan`, a plugin's checked
options today, becomes internal, and the read planner of
`rust/refkv-ir/src/plan.rs` is the core's own.)

```python
plan = refkv.build.plan(parsed)
plan.arrays                                          # source path -> Array
plan.images                                          # TIFF, NDPI, QPTIFF: every image, below
plan.groups                                          # HDF5: canonical path -> its links and members
plan.unpresented                                     # source path -> reason (hdf5.md §7.4)
plan.refused                                         # None, or why the input is refused, with its kind
```

`plan.arrays` holds every array the convention presents but an image's, by
its path in the source: an HDF5 dataset's canonical path, a safetensors
tensor's name, `""` for a NumPy file's one array and each member's name
without `.npy` for a `.npz`. The plan is a function of
the description, the IR's elements it names, the bytes it reads and the
run's options.
It reads them as a step machine (`poll` for ranges, `complete` with their
bytes, in rounds, as a parser does), since a Rust projection is sans-I/O
today (the run reads one batch it declares, `reads` in
`rust/refkv-ir/src/project/mod.rs`): edge chunks copied, `omero` samples,
masked chunks' Adler-32, LZW heads, HDF5 strings, each under its
convention's bounds (one `omero` sample per channel of at most 2^26 bytes; at
most 2^26 bytes of a dataset's masked chunks; an LZW strip's head of at most
3 × rows × B + 64 bytes ([tiff.md §7.1](../../spec/zarrify/tiff.md#71-reading), B the bytes of one row of a strip, [§4.5](../../spec/zarrify/tiff.md#45-strips));
HDF5's 4096 rounds, counted from the parser's). Python, the Rust CLI and the
browser drive the same machine. A caller cannot widen those bounds, and
pays for all of them: a plan is of the whole input, so a caller that wants
one dataset still has every presented dataset's strings read and every
image's `omero` windows sampled.

Every check of a decision runs in `plan`, for the whole input, whatever the
caller then writes, so what the plan finds does not depend on what an output
includes ([zarrify §1.2](../../spec/zarrify.md#12-input)'s **Evaluation
order**). It finds two things, and raises neither. A dataset the convention
does not present is a reason in `plan.unpresented`, and an image one its own
`reason`. A rejection of the whole input is `plan.refused`, of one of two
kinds. A `budget` refusal, by a run-wide budget the plan breaks (the 4096
rounds, the 2^20 strings, the LZW heads' total, the references budget),
stops its reading, and it holds no arrays and no images. A `layout`
refusal, by the convention's layout (a multi-page TIFF in
strips without OME-XML, [tiff.md §4.2](../../spec/zarrify/tiff.md#42-pyramid-levels);
`SizeC` other than the samples per pixel, [§4.1](../../spec/zarrify/tiff.md#41-planes))
or a reason of the first image, whose failure rejects the input today),
leaves the plan holding all it built: every image, with its reason or its
levels, and every array. `project` raises `Rejected` for either, as
`zarrify` does; a
caller writing another output may still present what the plan holds, and
`tiff_array` ([Building and writing](#4-building-and-writing)) gives the
arrays the layout would not.

For a TIFF (and NDPI and QPTIFF), `plan.images` holds every OME `Image`
(one, for a TIFF without OME-XML), each as
[tiff.md §4.1](../../spec/zarrify/tiff.md#41-planes) to
[§4.5](../../spec/zarrify/tiff.md#45-strips) give it: its `reason`, `None`
or why the convention does not present it (then it has no levels); its
name; its axes, each a name with its type and unit, in its arrays' axis
order; its levels, each with its IFD paths, its physical scale per axis (the
image's scale times the level's ratio), its planes and its `Array` (strips
joined, padded and, where [§4.4](../../spec/zarrify/tiff.md#44-arrays) shards
a level, sharded); its `position`, twice: `position.given`, as the file
gives it, with its units and its anchor (`"center"` for OME's `Position`,
`"corner"` for Aperio's `Left` and `Top`, `"slide-center"` for NDPI's offset,
QPTIFF's as [qptiff.md](../../spec/zarrify/qptiff.md) says), and
`position.corner`, converted to the corner of the image refkv's convention
uses and checked, one number per axis in axis order and units, or `None`
where the file places nothing; and its `omero` windows. refkv's own output presents the first image, and a failure
of another is that image's reason, not the input's.

### 4. Building and writing

What exists stays: `cut`, `concat`, `payload_size`, `chunk_key`,
`chunk_coords`, `fill_value`, and the codec routines of
`refkv.zarrify.coding` (`tiff_tags`, `tiff_data_type`, `tiff_codecs`,
`tiff_byte_order`, `czi_codecs`, `vsi_codecs`, `dicom_codecs`,
`gzip_codecs`, `hdf5_value`, `hdf5_attributes`). The routines that walk the
IR (`tiff_tags`, `tiff_byte_order`) take a `Parsed`, and so does
`hdf5_attributes(parsed, plan, path, *, json=False)` once HDF5's description lands
(the plan for the reasons an attribute is left out that are the output's)
(until then it takes what `project(parsed)` returns): without `json` it gives
the attributes' decoded values (a NaN as a float), and with it as
[hdf5.md §6](../../spec/zarrify/hdf5.md#6-attributes) writes them for JSON.
The rest take plain values. New beside them, each the public binding of the
Rust function the built-in projection calls:

- **`grid(span, shape, item_size, *, limit)`**: a contiguous C-order array
  cut as [conventions §7](../../spec/conventions.md#7-source-values-as-arrays)
  cuts it (`out::grid_chunks`): the chunk shape it chooses, of whole
  trailing slabs, at most `limit` bytes, and its chunks, an edge chunk padded
  with `Zeros`, or, where the padded reference would not fit, a `Copy` of
  the edge chunk's range, which the caller fills with `parsed.bytes` (the
  plan fills its own). A caller that wants
  another chunk shape builds it with `cut` and `concat`, as the
  `hdf5-rechunk` example does, within `fits`.
- **`shard(array, shard_shape)`**: an `Array` as virtual shards of a shape
  the caller chooses, as [zarrify §2.1](../../spec/zarrify.md#21-virtual-shards)
  builds them (`shard.rs`), joined as its chunks' input was read
  (`checksums`): its codecs become `sharding_indexed`, and its chunks the
  shards.
- **`tiff_array(parsed, ifds)`**: the `Array` of a sequence of IFDs (their IR
  paths, `ifds/<n>`) of one
  format, as planes along a new leading axis, their strips joined and padded
  as [tiff.md §4.3](../../spec/zarrify/tiff.md#43-data-types-and-codecs) to
  [§4.5](../../spec/zarrify/tiff.md#45-strips) say, without the image
  layout's mapping or its rejections. It runs as a plan does, a step machine
  reading its IFDs' LZW heads under [§7.1](../../spec/zarrify/tiff.md#71-reading)'s
  bound and against `max_bytes`, without sharding (a caller shards it with
  `shard`), its rounds counted on its own, not the
  plan's, and raises
  `ValueError`, naming
  the check, when one of [§2](../../spec/zarrify/tiff.md#2-the-source)'s or [§4.5](../../spec/zarrify/tiff.md#45-strips)'s fails for its IFDs. A caller
  presenting what the layout refuses (a multi-page TIFF in strips without
  OME-XML, [§4.2](../../spec/zarrify/tiff.md#42-pyramid-levels))
  takes its arrays from here.
- **`hdf5_array(plan, path)`**: the `Array` of a dataset the plan leaves out
  for its one reason of the output, `filter_mask` because its masked chunks
  pass the 2^26 bytes a dataset's Adler-32 reads may take (the plan records
  which of `filter_mask`'s causes it was;
  [hdf5.md §3.2](../../spec/zarrify/hdf5.md#32-chunks)), built as
  [hdf5.md §3](../../spec/zarrify/hdf5.md#3-datasets) builds a presented
  one's, its Adler-32 reads against `max_bytes` instead. Its phony
  dimensions are the plan's, which numbers them for every dataset, presented
  or not, so they do not depend on what a caller asks for. Any other reason
  (`datatype`, a `fill_value` with no JSON form, `edge_chunks` stored
  unfiltered, a filter with no codec, and `filter_mask`'s other causes: a
  Fletcher32 chunk of at most 4 bytes, two mask bits set, a skipped shuffle,
  LZF past 2^16 bytes) leaves no array one chain of codecs
  reads, and raises `ValueError`, as does a refused plan. It runs as
  `tiff_array` does.
- **`ome_image(plan, image, paths)`** and **`omero(plan, image)`**: the
  OME-Zarr 0.5 `multiscales` and `omero` the image layouts write
  (`out::image_ome`, `out::omero_json`), for an image's levels written at
  `paths`.
- **`mirror(parsed)`**, **`declaration(parsed, plan, path, layouts=None)`**
  and **`source_metadata(plan, path)`**: the IR mirror's entries
  (`_core.mirror`); a node's declared attributes, its `zarr_conventions` and
  its `refkv_zarrified` (the declaration of refkv's convention,
  [conventions §2](../../spec/conventions.md#2-attributes)), with its source
  metadata and, at the root, the source and the layouts it holds
  (`out::declare` and `out::declare_array`); and a node's source metadata
  alone. A declaration is of one input, whose source it names.
- **`plan.groups`**: for HDF5, every group the convention presents, by its
  canonical path, with the links and the members
  ([hdf5.md §2.1](../../spec/zarrify/hdf5.md#21-declaration-and-attributes))
  its attributes gain (`_LINKS`; an array's `_DTYPE` and `_REFERENCE_FIELDS`
  are on its plan entry), from the description and the plan's decisions.

A routine returns a `Written` where it places its entries other than by
default: `mirror`'s documents are kept for later
([conventions §2](../../spec/conventions.md#2-attributes)), and so are the
lazily read nodes a convention places so (CZI's `TILES/`).
`hierarchy.array(a, *, dimension_names=None, attributes=None, to=None)`
takes an `Array` and returns a `Written`, and `hierarchy.check(written,
inputs)` takes the inputs.

```python
store = refkv.build.seal(written, inputs=[a, b])
store.save_vzip("out.vzip")
```

`seal(written, inputs, *, policy=None, registry=None)` is the check that
vouches for an output: the steps of
[spec/layout.md §4](../../spec/layout.md#4-writing-the-output), for any
number of inputs and without a base, as a function of the core. `seal`:

1. **Builds the source table** by first use over the references in key
   order (UTF-8), and within a reference in order of its parts, as a store
   run numbers its output today (`StoreIrOutput._numbered` in
   `python/src/refkv/ir/store.py`): the url sources first, then the data
   sources (each distinct `Shared` and `Data` byte string, and the zero data
   source, as long as the most `Zeros` any value holds). With one input that
   is a plain file (not a store, and with one entry in `parsed.sources`),
   the file is source 0 whether or not a value uses it, as
   [zarrify §1.2](../../spec/zarrify.md#12-input) says; with several inputs,
   or a store, there is no such source 0, and a source no value uses is not
   in the table. Equal url sources (URL and pins) are one; one URL with
   different pins (the file changed between two reads) is refused, and so is
   a `Ref` to a `Parsed` not among `inputs`. The orders some documents give
   their sources today (a TIFF set's walk of its planes,
   [tiff.md §7.4](../../spec/zarrify/tiff.md#74-several-files); a file read
   as a store, [zarrify §1.8](../../spec/zarrify.md#18-files-read-as-stores))
   and file runs' numbering of data sources change to this one rule, so a
   built-in table and a sealed one agree.
2. **Checks**, after numbering: every key, every `Ref` against its source's
   size as `read` found it, every payload, and the archive's limits. How many
   reads or bytes a value takes is its reader's limit
   ([spec/refkv.md §6.7](../../spec/refkv.md#67-reader-policy) rule 5), which
   `seal` does not check.
3. **Places** the entries: as the `Written` says, and otherwise every Zarr
   document is read at open.
4. **Checksums** the ranges, when the inputs were read with checksums
   (inputs read with and without them are refused together), reading them as
   a step machine, as the plan does, driven through the inputs' transports.
   Without checksums, `seal` reads nothing, and an input may already be
   closed: its sources' sizes and pins are known.

The inputs must have been read under the same policy and registry, unless
`policy` and `registry` are given, which the store then keeps, as
`refkv.compose` treats its operands. A failed check is a `ValueError`
naming the key and the rule, with a code (`range`, `payload`, `checksums`,
`key`, `archive`, and `inputs` for inputs that do not go together:
one URL with different pins, a `Ref` to a `Parsed` not given, inputs read
with and without checksums); the built-in projection, which seals its output
the same way, turns `range` and `payload` into the `Rejected` [zarrify
§1.2](../../spec/zarrify.md#12-input) asks for, and `checksums` (a payload
over the limit only because of its checksums) into the failure [§1.2](../../spec/zarrify.md#12-input) makes of
it, and the rebuild test does the same. The references budget of
[§1.2](../../spec/zarrify.md#12-input) is the plan's, for refkv's own
presentation; a caller's output is not held to it, as a plugin's is not
today.

`hierarchy.check(written, inputs)`, the problems a Zarr reader would trip on, is a
separate function for a caller's tests; for its `refkv` code (a declaration
its subtree does not bear out) it runs `project` on the input the
declaration names and compares the subtree with refkv's. An output keeping refkv's declarations claims to
follow refkv's convention, Structure only with it; `seal` does not check the
claim. Structure only binds refkv's conventions; a caller's output that does
not keep refkv's declarations claims nothing of them, whatever it read.

### 5. The built-in output: `project`

`refkv.build.project(parsed)` is the built-in projection: `plan(parsed)`,
`Rejected` if the plan is refused, then the convention's entries written
from the plan, with the mirror and the declarations, sealed. It returns what
`Base` gives today (entries, documents, arrays, attributes without refkv's
declarations, and `elements(path)`, which maps a built-in array's chunks to
IR elements, or to chunk index entries), as values, with `copy` working on it
as now. A caller that keeps refkv's arrays and changes only their metadata
copies them from it, as the `bare` example does.

## What a caller writes

An OME-Zarr 0.6 image of a TIFF, without `project`. The 0.6 metadata is the
caller's, and so is turning the file's position into 0.6's translation, which
places the center of a level's first pixel: from `position.corner`, the
corner refkv's rules give, plus half a pixel of each level's scale,
arithmetic of 0.6, not a rule of refkv's.

```python
from refkv.build import Written, plan, read, seal
from refkv.zarrify.layouts.hierarchy import array, group

with read("image.ome.tif") as p:
    pl = plan(p)
    if pl.refused:                                   # a layout refusal still holds images; this caller stops
        raise ValueError(pl.refused)
    image = pl.images[0]                             # name, axes, levels, position
    if image.reason:
        raise ValueError(image.reason)
    out, datasets = Written(), []
    for n, level in enumerate(image.levels):
        out |= array(level.array, dimension_names=[a.name for a in image.axes], to=str(n))
        datasets.append({"path": str(n), "coordinateTransformations": [{
            "type": "scale", "scale": level.scale}]})  # physical; and a translation: on the space axes,
            # where image.position.corner is not None, corner + level.scale / 2; 0 on the others
    ome = {"version": "0.6", "multiscales": [{"name": image.name, "coordinateSystems": [...],
                                              "datasets": datasets}]}
    out["zarr.json"] = group({"ome": ome})
    seal(out, inputs=[p]).save_vzip("image.vzip")
```

An OME-Zarr 0.6 image of an HDF5 dataset; its axes are the caller's options,
since HDF5 has none. A dataset the convention does not present is in
`plan.unpresented`, with its reason.

```python
from refkv.build import Array, plan, read, seal
from refkv.zarrify.coding import hdf5_attributes
from refkv.zarrify.layouts.hierarchy import array, group

with read("ophys.nwb") as p:
    path = "stimulus/templates/movie/data"           # NWB stores (t, x, y)
    pl = plan(p)
    a = pl.arrays[path]                              # data type, codecs, fill value, chunks, as hdf5.md §3
    perm = [0, 2, 1]                                 # presented axis j is stored axis perm[j]: (t, y, x)
    order = [perm.index(k) for k in range(len(perm))]  # the codec's order, perm's inverse
    a = Array([a.shape[i] for i in perm], a.data_type, [a.chunk_shape[i] for i in perm],
              [{"name": "transpose", "configuration": {"order": order}}, *a.codecs],
              {tuple(c[i] for i in perm): v for c, v in a.chunks.items()}, a.fill_value)
    out = array(a, dimension_names=["t", "y", "x"],
                attributes=hdf5_attributes(p, pl, path, json=True), to="movie/0")
    out["movie/zarr.json"] = group({"ome": {...}})   # the 0.6 image, axes from the caller's options
    out["zarr.json"] = group()
    seal(out, inputs=[p]).save_vzip("movie.vzip")
```

The **TIFF stack** of d-v-b/refkv#282 reads every file and seals one
`stack.tif` over all the inputs, instead of parsing them again with tifffile.
It needs no plan: writing a TIFF file is its own output's format, so encoding
IFDs is the caller's code, not a restated rule of refkv's. For each page it
walks the IFD's entries, a tag's repeats (`tags/<tag>~<k>`) included but for
the tags it rewrites or leaves out, of which only the first entry counts
([tiff.md §5.1](../../spec/zarrify/tiff.md#51-elements)), taking each
entry's tag, type and count from `ir.value` (a tag's first entry is the one
[tiff.md §2](../../spec/zarrify/tiff.md#2-the-source) reads). Most tags are of unknown meaning
to it and are copied uninterpreted, their values' bytes from `parsed.bytes`
into the header blob (each input's `max_bytes` set for one file's tags), so
that the value of `stack.tif` is one `Data` part and a `Ref` per contiguous
run of strips (`concat(..., merge=True)`), as the example's is. The strip and
tile tables (273, 279, 324, 325), which it rewrites, are known, so it takes
them from the description once TIFF's lands, and from `ir.value` of
`…/tags/273/value` and the others until then (tiff.md
[§5.3](../../spec/zarrify/tiff.md#53-values) keeps the tables whole). It
builds each strip's `Ref` from the tables, its offset and byte count checked
against the file's size (`parsed.sources[0].size`), not from the IR's
`strips/<j>` elements, which exist only for a strip of nonzero length within
the file. It leaves out what points elsewhere in the old file: SubIFDs,
Exif, GPS and Interoperability IFDs, free space, the GlobalParametersIFD
(330, 34665, 34853, 40965, 288, 289, 400), and every entry of field type IFD
or IFD8 (13, 18), that is, every tag with an `ifd_<tag>` child in the IR
([tiff.md §5.2](../../spec/zarrify/tiff.md#52-which-ifds)); and entries whose value's size cannot be told (a field type
the model does not know) or that lie outside the file. It refuses an
old-style JPEG's offset (513), a BigTIFF or mixed byte orders
(`header/magic`, `header/byte_order`), a page without strips or tiles, a
strip outside the file, and a stack of 4 GiB or more. The strips are ranges of the file, not `reference`s, since a JPEG strip's reference splices in tables the copied
JPEGTables tag already gives. Without checksums each input can be closed
once its pages are walked.

A file that names others (one of an OME-TIFF set, read with its siblings, so
that `len(parsed.sources) > 1`) is refused, since reading one file of a set alone is [not in this
proposal](#not-in-this-proposal). The tifffile example reads such a file's
own pages; until refkv can, the stack of a set is that example's, not this
one's. Until TIFF's decisions move out of its parser
([Order of work](#order-of-work)), `read` also still raises the image
layout's rejections, so the stack's inputs are, for that time, files the
layout accepts.

None of the three needs a plugin. A caller that wants the command line to run
one can still register it.

## Plugins

A plugin is what the command line and the entry-point registry can name:
`python -m refkv.zarrify --layout NAME`, and `zarrify(..., layout=Layout(name,
options))`, its face in Python, for an application that takes a layout's name
as configuration rather than importing its code. That is its whole reason to
remain. A plugin
becomes `write(inputs: Sequence[Parsed], options) -> Written`, given the
parsed inputs: `zarrify` takes a sequence of locations with `layout`
(`zarrify(["a.tif", "b.tif"], layout=...)`; a sequence without `layout`, and
`url` with several, are refused), the CLI several locations with `--layout`,
a store input among them, and `plan` and `project` are there for a plugin
that wants them. Its `formats` apply to each input. refkv seals what it
returns. A name an entry point of another distribution already takes is
refused when the registry is read, rather than one silently replacing the
other. The names, options and errors of [spec/layout.md](../../spec/layout.md)
stay; its determinism rule is restated over the inputs (their descriptions
and IRs, every byte read through them, by `bytes`, a plan, `tiff_array` or
`hdf5_array`, their run
options, the plugin's options in their canonical form
([spec/layout.md §1](../../spec/layout.md#1-plugins)) and the plugin's code), and its section on what a plugin is given
becomes a pointer to the tools.

## Testing that the tools suffice

The criterion is tested, not argued, by two kinds of test.

**Rebuilding refkv's own output.** For each of a few formats, the
implementation adds a Python projection written only with `read`, `plan`,
the values, the routines of group 4 and `hierarchy`'s `array` and `group`
(not `project`, and not `copy` of a built-in array), and a test that runs it
over every fixture of the format and checks that its output is equivalent to
`zarrify`'s as [zarrify §1.1](../../spec/zarrify.md#11-output-and-equivalence)
defines it, mirror, declarations and source table included, and that it
rejects the same inputs (`plan.refused`, `read`'s `Rejected`, and `seal`'s
`ValueError` as `project` turns it into `Rejected`). The first are NumPy,
TIFF's image layout and HDF5's hierarchy (its groups, datasets and links,
[hdf5.md §2](../../spec/zarrify/hdf5.md#2-the-hierarchy)). What a Python projection
writes is the convention's layout: keys, documents, attributes, from the
plan's decisions; so the test shows that the plan and the routines carry
every decision, and that the layout rules left are the convention's prose,
not hidden code.

**Building outputs refkv does not write.** The three outputs of [What a
caller writes](#what-a-caller-writes) become shipped examples, each built
without `project` and without code that restates a rule of refkv's
conventions (a mapping of OME planes, a strip join, a shard size, a codec
table, a position conversion); a caller's own output format (the TIFF the
stack writes, 0.6's metadata) is its own. The list is closed, and the
examples' review checks them against it. The HDF5 example takes the arrays
to present, their axes and their permutations as options, as the
`ome-zarr-06` example does today, so its test gives them per fixture. Each is tested over its formats'
fixtures by reading every array, or every page, back and comparing it with
tifffile's or h5py's values. A tool too coarse for one of them, so that a
caller must restate a rule to get past it, is split until it is not. The
other formats have the rebuild test only, which shows their plans carry
every decision; an output refkv does not write gains a test of the second
kind when someone writes one.

What either test cannot build is a missing tool, added before the test
passes. The Rust projections stay Rust: the browser and the Rust CLI run
them, and a Python projection is the test of the tools, not a replacement.

## What changes on main

- **Kept**: the codec routines, `cut`, `concat` and the other helpers
  (taking values), `copy` and `group`, the plugin entry points, names,
  errors and CLI, the `bare` example (over `project`), and the `zarrs`
  feature.
- **Changed**: `hierarchy.array` takes an `Array` and returns a `Written`,
  `hierarchy.check` takes the inputs; `Written` becomes a mutable mapping;
  `hdf5_attributes` gains `json`, and takes a `Parsed` once HDF5's
  description lands (what `project(parsed)` returns until then); `refkv.ir.Ir`'s
  navigation (`at`, `index`, `children`, `value`, `target`, `raw`) is
  documented as public; `tiff_tags` and `tiff_byte_order` take a `Parsed`; a plugin's `write`
  takes the inputs; `zarrify` takes several locations with `layout`; a
  duplicate plugin name is refused. `refkv.ir.parse` stays internal, behind
  `read`. Each format's facts are deleted once its items are sorted.
- **Extended**: [tiff.md §3](../../spec/zarrify/tiff.md#3-ome-xml) reads every OME `Image`, with the same
  attributes as the first, and the TIFF parser with it; refkv's own output
  still presents the first, and another image's failure is its own reason.
- **Moved**: `Base`'s IR accessors and `fits` to `Parsed` and `refkv.build`;
  its output accessors to what `project` returns; `prepare` and `store_of`
  into `seal`, a function of the core; the TIFF parser's plane mapping,
  level scan, joining, sharding, padding, LZW tails and position conversion,
  with its checks of the IFDs they use, and the HDF5 parser's presentation,
  Zarr types, translation of its codecs (which its datasets' `StoredArray`s
  give), dimension names, masked-chunk Adler-32 and dataset strings, into
  the plan, with the checks and reads that go with them; the
  Rust projections' one batch of reads into the plan's rounds; the IR
  mirror's rows budget into `read`; TIFF's `ome/planes/<p>` aliases
  ([tiff.md §5.1](../../spec/zarrify/tiff.md#51-elements)), which record the
  plane mapping, out of the IR into the plan.
- **Rewritten**: the `ome-zarr-06`, `tiff-pages`, `hdf5-rechunk` and
  `xarray-v2` examples, over the tools; `ome-zarr-06` moves off `project`
  format by format as each plan lands (IMS's with the other formats', after
  TIFF's and HDF5's).
- **New**: `refkv.build` (`read`, `Parsed`, the values, `Array`, `Written`,
  `plan`, `grid`, `shard`, `tiff_array`, `hdf5_array`, `ome_image`,
  `omero`, `mirror`,
  `declaration`, `source_metadata`, `seal`, `project`, and `MAX_PAYLOAD`, the
  65519-byte payload limit); each
  format's list of known structures, its description and schema; the tests
  of both kinds.
- **Changed outputs**: every output's source table is numbered by first use
  in key order, so some outputs number their sources differently (a TIFF
  set's, a file read as a store's, a file's JPEG tables' data sources);
  every decision sized by a payload measures source indexes at their
  largest, so an edge chunk, a strings chunk or an LZF chunk near the limit
  may be copied, split or framed where it was not; an HDF5 mirror past its
  room holds fewer storage elements, and no global heap collections read
  only for a dataset's strings or records; the references budget counts a repeat by its
  source range; every OME-TIFF's mirror loses its `ome/planes/<p>` aliases;
  an HDF5 run whose attributes' and datasets' collections were
  read together now reads them in more rounds, so an input near the 4096
  rounds may be newly rejected.
- **Specification**: spec/layout.md splits in two. The checks `seal` applies
  become a document of their own (what an output built from parsed inputs
  must satisfy, for any number of inputs); what is left is plugins, and its
  [§2](../../spec/layout.md#2-the-base) no longer binds a plugin to Structure
  only by withholding reads: a plugin's output claims refkv's conventions
  only where it keeps their declarations ([zarrify §1.2](../../spec/zarrify.md#12-input)
  is amended with it). Each profile's document gains its known structures
  and description, and its source metadata is restated over the plan.
  [§1.2](../../spec/zarrify.md#12-input)'s numbering, its references budget
  and its **Evaluation order** (now: every check of a decision runs in the
  plan), the documents' own source orders,
  [conventions §7](../../spec/conventions.md#7-source-values-as-arrays)'s
  copy rule, [tiff.md §2](../../spec/zarrify/tiff.md#2-the-source), [§3](../../spec/zarrify/tiff.md#3-ome-xml) and [§7.1](../../spec/zarrify/tiff.md#71-reading) (which checks are whose), and hdf5.md's
  room, [§4](../../spec/zarrify/hdf5.md#4-variable-length-strings),
  [§8.3](../../spec/zarrify/hdf5.md#83-checks) and
  [§8.4](../../spec/zarrify/hdf5.md#84-chunk-references) change as above.

refkv has no users yet, so nothing on main is kept for compatibility alone,
and changing an output is a cost only to the fixtures.

## Order of work

1. **The plumbing**, with no format's facts changed: `read` and `Parsed`
   (with `StoreRun.finish` wrapped), the values, `Array`, `Written`, `seal` in the core from `prepare` and `store_of` with the
   new numbering (and the built-in outputs changed to it, with the references
   budget's repeats by source range), the accessors
   moved from `Base`, `bytes`, `gaps`, and `project`. `parsed.description`
   is `None` for a format until its description lands, and `plan` is not yet
   public; the parsers still make the decisions, so `read` still raises their
   checks. The TIFF stack rewritten over this, its tags from the IR.
2. **The plan's machine**: the Rust projections' batch of reads becomes the
   rounds of a step machine, driven by the run in Python, the Rust CLI and
   the browser, with no output changed.
3. **One format at a time**, NumPy, then TIFF, then HDF5, each in two PRs.
   The first lists its known structures, specifies its description and
   tests it against tifffile, NumPy or h5py (TIFF's extending [§3](../../spec/zarrify/tiff.md#3-ome-xml) to every
   `Image`); each format's first PR may land before step 2
   ([ir-format-descriptions.md](ir-format-descriptions.md)). The second
   sorts its facts and checks against them, moves the
   parser's decisions and their reads into its plan, sizes its decisions by
   the largest source index (changing the outputs near the limit), deletes
   its facts, adds the routines it needs, and adds its tests, which pass
   before it merges.
   The 0.6 examples move onto TIFF's and HDF5's plans as they land.
4. **The other formats**, the same way, the store profiles and those whose
   IR is a store's (Zarr v2 and v3, N5, precomputed, DICOM's and SAFE's store
   forms), GRIB and ND2 among them; each has a description and a plan.

## Not in this proposal

- **Parser plugins**, for formats no built-in parser reads.
- **JavaScript.** The WebAssembly module exposes runs and their outputs,
  not IR accessors (`js/src/zarrify/ir/wasm.ts`). The tools are functions of
  the core, so a JS binding of them can follow.
- **Reading a file without the files it names**, such as one file of an
  OME-TIFF set alone: `read` of one file of a set reads the set.

## Prior art

Each idea here has precedents among file-format parsers and the tools that
reference data in place. What refkv takes from them, and where it differs:

**Known and unknown.** Parsers that expose a file as a tree of fields mostly
handle the bytes they do not understand the way this note does.
[Wireshark](https://github.com/wireshark/wireshark/blob/master/epan/dissectors/packet-data.c)
hands what no dissector claims to its generic `data` dissector as raw bytes;
[fq](https://github.com/wader/fq/blob/master/doc/dev.md) tells format authors
to "decode only ranges you know what they are", and adds "gap" fields for the
rest itself, as the IR's gaps do; [Kaitai Struct](https://doc.kaitai.io/user_guide.html)
yields a raw byte array for a field with a `size` and no type, or a switch
with no matching case; [ExifTool](https://exiftool.org/exiftool_pod.html)
prints unknown tags under generated names (`-u`) and copies maker notes as
one block when it writes. [Construct](https://construct.readthedocs.io/en/latest/misc.html)'s
`RawCopy` gives a parsed field's value, its raw bytes and its start and end
offsets together, the three ways a caller reaches an element here
(`ir.value`, `parsed.bytes`, `span`). For TIFF,
[tifffile](https://pypi.org/project/tifffile/), the independent reader this
note tests against, does the same for every tag, known or not: a `TiffTag`
has its value, its offset and its value's offset. In microscopy,
[Bio-Formats](https://javadoc.scijava.org/Bio-Formats/loci/formats/IFormatReader.html)
draws the line between known and unknown as this note does: a reader fills
the OME model, its known, through a `MetadataStore`, and keeps the rest as
"original metadata", tables of named values (`getGlobalMetadata`,
`getSeriesMetadata`). Its unknown has no bytes and no place in the file;
refkv's keeps both, across every format it reads, so a caller can decode what
refkv's model does not.

**A specified description of a format.** [DFDL](https://daffodil.apache.org/)
(an Open Grid Forum standard, which Apache Daffodil compiles to a parser it
runs) and Kaitai Struct (whose compiler writes a parser's source code)
describe a format's layout in a machine-readable language. refkv's source
models are partly machine-readable already: `rust/refkv-ir/schema/<format>.json`
gives each format's elements, limits and constraints as `require`
expressions, which the parsers' checks apply. The parsers themselves stay
hand-written, for their budgets and their reading order. The description
extends that model with what the known structures mean, as a JSON Schema
beside the source model's rules, and its tests check it against tifffile,
NumPy or h5py.

**Building outputs of references.** [GDAL's VRT](https://gdal.org/en/stable/drivers/raster/vrt.html)
is the oldest widely used format of this kind: XML datasets made of other
datasets, its `VRTRawRasterBand` a strided view of another file's bytes.
[kerchunk](https://fsspec.github.io/kerchunk/spec.html) references a key's
bytes as one `[url, offset, length]` or inline data, and builds them from
each library's model of a file: its TIFF reader from tifffile's (whose
`ZarrTiffStore.write_fsspec` writes the references,
[kerchunk/tiff.py](https://github.com/fsspec/kerchunk/blob/main/kerchunk/tiff.py)),
its HDF5 reader from h5py's, through HDF5's chunk queries
(`H5Dget_chunk_info`, `H5Dchunk_iter`), which give each chunk's offset, size
and filter mask as the chunk map does.
[VirtualiZarr](https://virtualizarr.readthedocs.io/en/stable/explanation/custom_parsers.html)'s
parsers are callables from a URL and an object-store registry to a
`ManifestStore` of chunk manifests. Each of these goes from a library's
model of one format to its manifests: there is no byte-complete IR beneath,
no description shared across formats, and no routines a parser and a caller
both call, which is the gap this note fills for refkv. Their manifests and
refkv's `Array` are close kin.

**Vouching for references.** [Icechunk](https://icechunk.io/en/stable/reference/migration/)
requires a repository's virtual chunk containers to be declared, and refuses
to resolve chunks under a URL prefix a reader has not authorized
(`authorize_virtual_chunk_access`), much as refkv's reader policy bounds what
a source may name; refkv's pins already follow Icechunk's checks of a
chunk's object ([spec/refkv.md §5.1](../../spec/refkv.md#51-pins)). GDAL
bounds what a VRT may read with `GDAL_VRT_RAWRASTERBAND_ALLOWED_SOURCE`, by
default a path relative to the VRT and without `..`, because a server that
accepts uploaded VRTs could otherwise be made to read local files; it warns
that its `ONLY_REMOTE` setting can still reach a local network. Both check
when a chunk is read. `seal` checks when an output is written: every range
against the size `read` found, every payload and the archive's limits.
VirtualiZarr's guide asks a parser to extract each chunk's byte ranges and
says nothing of checking them against the file's size, and kerchunk's
specification nothing either.

**Sans-I/O reading.** [h11](https://h11.readthedocs.io/en/latest/) is the
model of a protocol with no I/O of its own: a state machine fed bytes by its
host. Parsers of byte streams have the same shape (nom's streaming parsers
return `Incomplete`, with how many more bytes they need when known; Spicy
yields to its host when input runs out). The closest precedent for random
access is arrow-rs's [Parquet push decoder](https://arrow.apache.org/rust/parquet/arrow/push_decoder/struct.ParquetPushDecoder.html),
which returns `NeedsData` with the byte ranges it needs, and is given their
bytes, as refkv's parsers and plans ask for ranges in rounds; refkv adds
counted rounds and per-convention bounds, which let the host batch, merge
and bound every read. The I/O hooks of libtiff (`TIFFClientOpen`),
libavformat (`avio_alloc_context`) and HDF5 (its virtual file drivers, ROS3
among them) are pull-style callbacks the library drives, so a host cannot
see or shape the whole read plan; HDF5's vector reads (`read_vector`, since
1.14) batch some of them.

**Registries.** Python's entry points carry most plugin registries refkv's
users meet, and they treat a taken name differently: numcodecs lets a
registered codec replace another of the same id without a word; xarray warns
of a duplicate engine name and keeps the first; zarr-python keeps every
implementation of a name and chooses one by its configuration, warning when
the choice is ambiguous. refkv refuses a second plugin of a taken name, since
a layout's name is what an archive's maker chooses by. Format detection
(libmagic, Apache Tika's detectors, Bio-Formats' ordered reader list,
FFmpeg's probe scores) is what parser plugins, left out of this note, would
need.

## Alternatives considered

- **Plugins given the base** (this note's previous version, on main). It
  keeps every external output downstream of the built-in projection, reaches
  the IR only through bridges from the base's chunks, takes one input, and
  needs a plugin even for a script. The tools give the same, and more,
  without it.
- **The format standard as the known set** (every TIFF 6.0 baseline tag,
  every HDF5 message type). It gives the cleanest line between model and
  use, at the cost of specifying and decoding structures no refkv output
  uses, up front. The known set is refkv's model instead, grown when refkv
  decides to understand a structure, and the unknown is handed over
  uninterpreted, as a value, bytes or a range, meanwhile.
- **Routines per array, each reading for itself** (an earlier draft of this
  version). The decisions that span an input (phony dimensions, a group's
  reasons, a run's budgets and rounds) then depended on which routines a
  caller called, and in what order. The plan makes them once.
- **A plan of only what a caller asks for**, to save its reads. Whether an
  input is rejected, and the decisions that span it, would then depend on
  what was asked. The plan is of the whole input; its cost is in
  [Open questions](#open-questions).
- **Markers the caller resolves**, for the bytes a routine needs read (an
  earlier draft). Every routine's reads became the caller's to perform and
  to bound. The plan reads for itself.
- **Exposing `parse`'s facts as they are.** A caller would depend on a JSON
  object that changes with every projection change, and the projection
  decisions in it would look like file structure.
- **Rewriting the projections in Python.** It would make Python the only
  implementation of each convention; the browser and the Rust CLI need Rust.

## Open questions

1. **The cost of a whole plan.** A caller that wants one dataset of a large
   HDF5 file still has every presented dataset's strings read, and one that
   wants a TIFF's arrays still has its `omero` windows sampled. A plan could
   skip the reads whose results nothing asks for while still running every
   check that does not read; whether that keeps rejection the same for every
   format is the question.
2. **A plan with choices.** The plan makes refkv's decisions. A caller that
   wants the same rules with another parameter (shards of another size, a
   different join) uses `shard`, `tiff_array` and `cut` today; options of
   the plan for the parameters a convention states may serve better, once a
   caller asks.
3. **Local sources reached through symbolic links.** refkv makes a local
   path absolute without resolving symbolic links
   ([spec/refkv.md §5](../../spec/refkv.md#5-source-table)), so a source a
   reader policy's prefix admits may be a link to a file outside it, the
   case GDAL's VRT rule warns of. `seal` checks what `read` opened; whether
   the policy should resolve links first is a question for
   [spec/refkv.md §6.7](../../spec/refkv.md#67-reader-policy), which this note
   does not change.
4. **An OME-TIFF's other series in refkv's own output.** The plan holds every
   `Image`; refkv's convention presents the first. Presenting the others is a
   change to the convention, which a caller need not wait for.
