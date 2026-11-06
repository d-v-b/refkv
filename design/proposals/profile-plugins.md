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
  writes an output equivalent to the built-in one
  ([zarrify §1.1](../../spec/zarrify.md#11-output-and-equivalence)).
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
  ([conventions §8.5](../../spec/conventions.md#85-forms-and-recipes)).
- **Room**: the bound a source model puts on how many elements an IR holds;
  past it, bytes no element claims are a gap
  ([hdf5.md §7.3](../../spec/zarrify/hdf5.md#73-the-ir-mirror)).
- **Payload**: a reference's encoded size, at most 65519 bytes; a source
  index above 0 is a varint in it ([zarrify §1.2](../../spec/zarrify.md#12-input)).
  The **zero data source** is the data source of zero bytes padding draws
  on.
- **Structure only**: the rule that an output does not depend on a file's
  pixel data, apart from the reads its convention specifies
  ([zarrify §1.2](../../spec/zarrify.md#12-input)).
- **Written**: an output's entries together with their placement, which
  bytes entries a vzip reader reads at open and which are stored
  uncompressed ([spec/layout.md §3](../../spec/layout.md#3-the-output)).
- **Rejected**: the error that refuses an input, `refkv.zarrify.Rejected`.

## The gap

A built-in projection is given what the run that read the file has. A caller
has, today:

| what a projection uses | what a caller has today |
|---|---|
| **The IR**, with every element's path, kind, extent, form and value | `refkv.ir.parse(fmt, transport)` returns it with the facts, unprojected, as the core's `Ir`, undocumented, for a file run; a store run has `StoreRun.finish` in the core and no such wrapper. The supported way, `layouts.base(path)`, runs the whole projection first and refuses store inputs |
| **The facts**: what the parser decoded (TIFF's sample format, compression, OME channels, pyramid levels; HDF5's nodes) | `parse` returns them, unspecified, changing with every projection change. [spec/layout.md §2](../../spec/layout.md#2-the-base) withholds them from plugins, which copy the decoded result from the base's documents instead |
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
- **The parsers make presentation decisions, and read for them.** The TIFF
  parser decides which strips join into a chunk, when to shard and how to
  pad (`join_strips`, `shard_strips`, `pad_strips` in
  `rust/refkv-ir/src/tiff.rs`), reads an LZW strip's head to build its tail,
  and converts the image's position (OME's center, Aperio's corner) to the
  corner the convention uses. The HDF5 parser decides which datasets are
  presented (`decide()` in `rust/refkv-ir/src/hdf5/file.rs`), writes each
  one's Zarr data type, codecs and fill value (`facts.nodes[].array`), reads
  a masked chunk's Adler-32 (`read_skipped`) and the strings of the presented
  datasets, and describes their storage whatever the room, other objects only
  while room lasts ([hdf5.md §7.3](../../spec/zarrify/hdf5.md#73-the-ir-mirror)).
  So which elements are in the IR depends on the output.
- **Nothing vouches for a store a caller builds.** `ReferenceStore(mapping,
  sources)` checks source indexes and key types, not that a range lies
  within its source, that a payload fits, or how many reads a value takes.
  `prepare` and `store_of` check all of that, but only for a plugin's output
  against one base.
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
structures it also specifies their meaning, adopting the format standard's
definitions or giving its own. That gives each structure one of two kinds:

- **Known**: a structure whose meaning the profile's document specifies. The
  document lists its known structures and the rules that interpret them: a
  TIFF's byte order, image dimensions, sample format, compression, predictor,
  planar configuration, resolution and strip and tile tags, its OME-XML
  (every `Image`, with its pixels, planes and channels,
  [tiff.md §3](../../spec/zarrify/tiff.md#3-ome-xml)), its ImageJ, Aperio and
  Micro-Manager metadata; an HDF5 node's dataspace, datatype, layout, chunk
  index with its filter masks, filters and fill value
  ([hdf5.md §3](../../spec/zarrify/hdf5.md#3-datasets)), its links
  ([§2.3](../../spec/zarrify/hdf5.md#23-links)) and attributes
  ([§6](../../spec/zarrify/hdf5.md#6-attributes)). The interpreted result is
  the format's **description**.
- **Unknown**: everything else the parser keeps: a TIFF tag whose meaning the
  document does not give (a private tag, an UNDEFINED one), an HDF5 message
  type it does not decode, a CZI attachment, an MRC extended header. An
  unknown structure is never interpreted into an array or an attribute of an
  output; it is in the IR mirror like every element. A caller gets its typed
  value (`ir.value(i)`) where its type has one, its bytes (`parsed.bytes(i)`,
  [References](#2-references-and-values)), and a reference to its extent
  (`parsed.span(i)`). Past the room, an unknown structure is in a gap, whose
  bytes a caller still has as a range; that is the raw-bytes fallback at its
  plainest. A caller that needs its meaning decodes it.

The built-in projections are built on the known because it is known: a
projection relies only on what the profile's document specifies, and an
external projection relies on exactly the same, through the same
description. A structure becomes known by a change to the document and the
description alone, tested against an independent reader of the format
(tifffile, h5py), which lands before any projection relies on it; a
projection never makes a structure known by using it. A structure nobody has
modeled stays unknown, and its bytes are still there.

### The description

The description of a format is its known structures, interpreted: specified
in the profile's document beside the rules that interpret them, with a JSON
Schema in `spec/zarrify/<format>/description.json`, versioned with the
convention. It names IR elements by their paths, which the source model
specifies ([tiff.md §5.1](../../spec/zarrify/tiff.md#51-elements)). Where a
known structure can be large, the description gives where it is rather than
all of it: an HDF5 dataset's chunk index is given by its location and type,
and the routine that presents the dataset walks it
([Building](#3-building)), under the budgets of
[hdf5.md §8.3](../../spec/zarrify/hdf5.md#83-checks), so the description
stays small and does not depend on the IR's room.

Today's facts are where each format's work starts, not what defines it.
Each profile's document lists its known structures first; then every item
of its facts is checked against the list, and is one of:

1. **Derived from a known structure**: it stays, in the description. TIFF's
   `plan`, the planes that lie in other files, is one: it is read from the
   set's OME-XML or Micro-Manager metadata
   ([tiff.md §7.4](../../spec/zarrify/tiff.md#74-several-files)).
2. **A decision of the convention**: which strips join into a chunk, when to
   shard and how to pad (TIFF's `join`, `shard`, `pad` and `rectilinear` per
   level, and the LZW tails padding needs); a level's scale as a ratio, and
   the position converted to the image's corner; which HDF5 datasets are
   presented, and each one's Zarr data type, codecs and fill value
   (`facts.nodes[].array`), the Adler-32 of a chunk a filter skipped, the
   strings and records a presented dataset reads. It moves into the
   projection, as a routine ([Building](#3-building)) that the built-in
   projection and a caller both call; a decision that reads bytes reads them
   there.

An item that is neither waits: the document is amended, as for any
structure refkv decides to understand, and that change lands before the item
moves. Every decision a built-in output makes is a rule its convention must
already state ([zarrify §2](../../spec/zarrify.md#2-the-zarr-layout)
requires the exact layout the convention specifies); one it does not is
stated in the same change. The facts' test hook, `copy_threshold` (in the
facts of HDF5, NumPy, NRRD, FITS, NIfTI and MRC, overriding `MAX_PAYLOAD`
of `rust/refkv-ir/src/out.rs`), moves out of them into a test-only setting of
the routines.

The parsers' checks sort the same way. A check of the file's own structure
(the reading checks of [tiff.md §7.1](../../spec/zarrify/tiff.md#71-reading)
and those of [hdf5.md §8.3](../../spec/zarrify/hdf5.md#83-checks) about the
walk, the source model's budgets) stays in the parser, and `read` raises it.
A check of a decision moves with the decision into its routine, which raises
`Rejected`: TIFF's position and padding checks (in `tiff.rs` today), the
checks of [hdf5.md §8.3](../../spec/zarrify/hdf5.md#83-checks) about presented datasets (their storage, their
strings, the 2^26 bytes a dataset's reads may take, the 2^20 strings a file
may present), and every rejection a convention's layout makes
([tiff.md §7.2](../../spec/zarrify/tiff.md#72-rejection)). Each format's
change lists its checks by side, item by item. A budget over the whole
output (the references budget of [zarrify §1.2](../../spec/zarrify.md#12-input),
the 2^20 strings, a mirror's rows, [§1.1](../../spec/zarrify.md#11-output-and-equivalence))
is kept by the `Parsed`, which the routines add to as they present; a
dataset presented twice counts once.

With presentation out of the parser, the IR's room stops depending on the
output: the HDF5 walk keeps objects and their storage in canonical order
while room lasts, and reads no strings. An HDF5 mirror then holds fewer
storage elements past its room than today, and no global heap collections
read only for strings.

A node's **source metadata** ([conventions §2](../../spec/conventions.md#2-attributes)),
which its output declares today, is then a function of the description and
of the decisions the routines made for it (HDF5's `unpresented` reasons,
[hdf5.md §7.4](../../spec/zarrify/hdf5.md#74-what-is-not-presented)), and a
routine gives it from both ([Building](#3-building)): one interpretation,
written in two places, not two.

The earlier version of this note rejected specifying the facts as "a second,
unversioned description of each format beside its convention", the base
output being that description already. Versioned with the convention, and
the projections' input, the description is not a second one: it is the one
the output and its source metadata are derived from.

## The tools

Five groups, each the public form of something a projection already has or
does. The names are proposals. The new module is `refkv.build`; what exists
stays where it is (`refkv.zarrify.coding`, `refkv.zarrify.layouts.hierarchy`).

### 1. Reading: `read`

```python
with refkv.build.read("image.ome.tif") as parsed:     # or read_async
    parsed.profile, parsed.version                   # "tiff", 0 (an NDPI file is "ndpi")
    parsed.ir                                        # refkv.ir.Ir
    parsed.description                               # the format's description
    parsed.sources                                   # tuple[refkv.Source], each url source with its size
```

`read(location, url=None, *, policy=None, registry=None, checksums=False,
max_bytes=None)` reads a file, or a store, as `zarrify` does (the same
sniffing, siblings, pins, reader policy and object stores) and stops before
the projection. It returns a `Parsed`, which keeps the run's transport open
for the reads below until it is closed. A file read as a store (VSI, NRRD,
an HDF5 file with external storage) gives the multi-source IR of
[conventions §8.9](../../spec/conventions.md#89-stores), and so does a store
input, through `StoreRun.finish`. `read` raises `Rejected` for the parser's
own checks only ([The description](#the-description)). `checksums` is the
run's, fixed here because a shard's ranges join differently with them
([zarrify §2.1](../../spec/zarrify.md#21-virtual-shards)).

Everything read through a `Parsed` after `read`, by `bytes` or by a routine,
counts against one budget, `max_bytes`. Its default is what a `zarrify` run
of the input may read for its own output, the sum of the bounds its
convention gives those reads; a caller that reads more raises it.

### 2. References and values

The IR accessors of `Base` move to `Parsed`: `parsed.reference(i,
member=None)` (an element's bytes as the profile reads them, its recipe
applied), `span(i, member=None)` (its own bytes, no recipe), `members`,
`form`, `element_at` and `source_of`. They refuse a node, a derived element
and one in a derived space, which no range of a source reads, and
`reference` a chunk whose form skips a filter, which `hdf5_array` frames.
`parsed.bytes(i)` is an element's own bytes, from the IR where the parser
kept them (`ir.raw`) and otherwise read through the input's transport,
within the budget; it refuses what `span` refuses. It reads an element of
any kind, since decoding an unknown structure needs its bytes.

They return **values**, whose parts name what they read, so that numbering
the source table is left to `seal`:

- `Ref(parsed, source, offset, length)`: bytes of a url source of an input,
  `source` an index of `parsed.sources`;
- `Shared(data)`: a byte string many references share (a JPEG tile's
  tables), which `seal` makes a data source;
- `Zeros(n)`: zero bytes, of the zero data source;
- `Data(data)`: a data source of the caller's own; `cut(Data(blob), a, b)`
  is bytes `a` to `b` of that one source;
- `Literal(data)`: bytes in the reference itself, counted in its payload.

A value is one part or a sequence of them, in order. `cut`, `concat` and
`fits` (why `seal` would not write a value, its payload measured with the
largest source index a varint holds) take values.

A value a routine returns is **checked**: the routine built it under a rule
of its convention (an HDF5 strings chunk may read more than 1024 ranges; a
shard is held to [zarrify §2.1](../../spec/zarrify.md#21-virtual-shards)'s
rule and to no read rule). The check holds where the routine put the value:
the chunk key of the array the routine returned, written under that array's
document, by `hierarchy.array` or `project`. `seal` honors it there and
nowhere else; a checked value placed at another key, or under another
document, and any `cut` or `concat` of one, is unchecked.

### 3. Building

What exists stays: `cut`, `concat`, `payload_size`, `chunk_key`,
`chunk_coords`, `fill_value`, and the codec routines of
`refkv.zarrify.coding` (`tiff_tags`, `tiff_data_type`, `tiff_codecs`,
`tiff_byte_order`, `czi_codecs`, `vsi_codecs`, `dicom_codecs`,
`gzip_codecs`, `hdf5_value`, `hdf5_attributes`). The routines that walk the
IR (`tiff_tags`, `tiff_byte_order`) take a `Parsed`, and so does
`hdf5_attributes` once HDF5's description lands (until then it takes what
`project(parsed)` returns); the rest take plain values.

The helpers projections use become routines, each the public binding of the
Rust function the built-in projection calls. A Rust projection is sans-I/O
today: the run reads one batch of bytes it declares (`reads` in
`rust/refkv-ir/src/project/mod.rs`) and hands them over. A routine that
reads keeps that shape but in rounds, as a parser does: it is a step machine
that asks for ranges and is given their bytes (`poll` and `complete`), with
its rounds counted against the bounds of its convention (an HDF5 dataset's
strings take dependent rounds, elements, then collections, then heap objects,
at most 4096 by [hdf5.md §8.3](../../spec/zarrify/hdf5.md#83-checks)). The
Python binding drives it through its `Parsed`'s transport, within the
budget; the Rust CLI and the browser drive the built-in projections the same
way. The bounds of the reads are each convention's, which a caller cannot
widen: an edge chunk copied only when its payload, with its padding as a
literal, would pass `MAX_PAYLOAD`; one `omero` sample per channel of at most
2^26 bytes; at most 2^26 bytes of a dataset's masked chunks; an LZW strip's
head of at most 3 × rows × B + 64 bytes
([tiff.md §7.1](../../spec/zarrify/tiff.md#71-reading)).

The array routines return an `Array`: shape, data type, codecs, fill value,
chunk grid, and checked chunks keyed by coordinate tuples. A routine does not
check payloads, which depend on the source numbering `seal` does; `seal`
checks them.

- **`grid(parsed, span, shape, item_size, *, limit)`**: a contiguous C-order
  array cut as [conventions §7](../../spec/conventions.md#7-source-values-as-arrays)
  cuts it (`out::grid_chunks`): the chunk shape it chooses, of whole
  trailing slabs so that each chunk is one range, at most `limit` bytes
  (each convention's own: HDF5's is 2^20,
  [hdf5.md §3.2](../../spec/zarrify/hdf5.md#32-chunks)), and an edge chunk
  padded with `Zeros`, or copied as above. A caller that wants another chunk
  shape builds it with `cut` and `concat`, a chunk of many ranges, as the
  `hdf5-rechunk` example does, within `fits`.
- **`shard(parsed, array, shard_shape)`**: an `Array` as virtual shards of a
  shape the caller chooses, as [zarrify §2.1](../../spec/zarrify.md#21-virtual-shards)
  builds them (`shard.rs`): its codecs become `sharding_indexed`, with its
  inner codecs and index codecs, and its chunks the shards, joined as the
  input's `checksums` say.
- **`tiff_image(parsed, image=0)`**: an OME `Image` of a TIFF (the only one,
  for a TIFF without OME-XML) as [tiff.md §4.1](../../spec/zarrify/tiff.md#41-planes)
  and [§4.2](../../spec/zarrify/tiff.md#42-pyramid-levels) give it: its name;
  its axes, each a name with its type and unit; its physical scale per axis;
  its position in the axes' units, with its anchor (`"center"` for OME's
  `Position`, `"corner"` for Aperio's `Left` and `Top`); and its levels, each
  with its IFD paths, shape, scale and planes. A caller writing a 0.6 image of
  a TIFF takes its planes from here instead of restating the OME and ImageJ
  mappings.
- **`tiff_array(parsed, image, level)`**: a level's `Array` as
  [§4.3](../../spec/zarrify/tiff.md#43-data-types-and-codecs) to
  [§4.5](../../spec/zarrify/tiff.md#45-strips) give it: strips joined, padded
  (an LZW strip's tail built from its head, which it reads) and, where [§4.4](../../spec/zarrify/tiff.md#44-arrays)
  shards a level, sharded.
- **`hdf5_array(parsed, path)`**: an HDF5 dataset's `Array` as
  [hdf5.md §3](../../spec/zarrify/hdf5.md#3-datasets) to
  [§5](../../spec/zarrify/hdf5.md#5-object-references) give it, or `Rejected`
  with the reason [§7.4](../../spec/zarrify/hdf5.md#74-what-is-not-presented)
  records: its shape (with the trailing axis a fixed-length string takes),
  data type, codecs and fill value (`array_type` and the code behind
  `facts.nodes[].array` today), its chunks from walking the chunk index, a
  chunk a filter skipped framed with its Adler-32, a contiguous dataset cut
  by `grid`, and a dataset of strings, records or references from the bytes
  it reads.
- **`ome_image(...)`, `omero(parsed, image, level)`**: the OME-Zarr 0.5 group
  document the image layouts write (`out::image_ome`), and an image's
  `omero` with its windows, sampled as
  [tiff.md §4.4](../../spec/zarrify/tiff.md#44-arrays) says
  (`out::omero_json`, `rust/refkv-ir/src/project/window.rs`).
- **`mirror(parsed)`**, **`declaration(parsed, ...)`** and
  **`source_metadata(parsed, path)`**: the IR mirror's entries
  (`_core.mirror`), a root's `refkv_zarrified`, and a node's source
  metadata from the description and the decisions the routines recorded on
  the `Parsed` (`out::declare`), for an output that keeps refkv's
  conventions.

A routine returns a `Written` where it places its entries other than by
default: `mirror`'s documents are kept for later
([conventions §2](../../spec/conventions.md#2-attributes)), and so are the
lazily read nodes a convention places so (CZI's `TILES/`).

`hierarchy.array` changes to take an `Array` (`array(a, *,
dimension_names=None, attributes=None, to=None)`), and `hierarchy.check` to
take the inputs (`check(written, inputs)`).

### 4. Writing: `seal`

```python
store = refkv.build.seal(written, inputs=[a, b])     # written: entries, or a Written
store.save_vzip("out.vzip")
```

`seal(written, inputs, *, policy=None, registry=None)` is the check that
vouches for an output: the steps of
[spec/layout.md §4](../../spec/layout.md#4-writing-the-output), for any
number of inputs and without a base. `written` is a mapping of keys to bytes
or values, or a `Written` (several merge with `|`, their placements kept).
`seal`:

1. **Builds the source table** by first use over the references in key
   order (UTF-8), and within a reference in order of its parts, as a store
   run numbers its output today (`StoreIrOutput._numbered` in
   `python/src/refkv/ir/store.py`): the url sources first, then the data
   sources (each distinct `Shared` and `Data` byte string, and the zero data
   source, as long as the most `Zeros` any value holds). With one input that
   is a plain file, the file is source 0 whether or not a value uses it, as
   [zarrify §1.2](../../spec/zarrify.md#12-input) says; with several inputs,
   or a store, there is no such source 0, and a source no value uses is not
   in the table. Equal url sources (URL and pins) are one; one URL with
   different pins (the file changed between two reads) is refused, and so
   is a `Ref` to a `Parsed` not among `inputs`. The orders some documents
   give their sources today (a TIFF set's walk of its planes,
   [tiff.md §7.4](../../spec/zarrify/tiff.md#74-several-files); a file read
   as a store, [zarrify §1.8](../../spec/zarrify.md#18-files-read-as-stores))
   and file runs' numbering of data sources change to this one rule, so a
   built-in table and a sealed one agree.
2. **Checks**, after numbering: every key, every `Ref` against its source's
   size as `read` found it, every payload, and the archive's limits. An
   unchecked value reads at most 1024 url ranges, counted as readers merge
   them ([spec/refkv.md §6.7](../../spec/refkv.md#67-reader-policy) rule 5);
   a checked value is held to its own rule. The whole-output budgets are the
   inputs' ([The description](#the-description)), which `seal` checks were
   not passed.
3. **Places** the entries: as the `Written` says, and otherwise every Zarr
   document is read at open.
4. **Checksums** the ranges, when the inputs were read with checksums
   (inputs read with and without them are refused together), through the
   inputs' transports. Without checksums, `seal` reads nothing, and an input
   may already be closed: its sources' sizes and pins are known.

The inputs must have been read under the same policy and registry, unless
`policy` and `registry` are given, which the store then keeps, as
`refkv.compose` treats its operands. A failed check is a `ValueError`
naming the key and the rule. `check(written, inputs)`, the problems a Zarr
reader would trip on, is a separate function for a caller's tests; for its
`refkv` code (a declaration its subtree does not bear out) it runs `project`
on the inputs and compares the subtree with refkv's. An output keeping
refkv's declarations claims to follow refkv's convention, Structure only
with it; `seal` does not check the claim.

### 5. The built-in output: `project`

The built-in output is one more thing a caller may want: a caller that keeps
refkv's arrays and changes only their metadata copies them, as the `bare`
example does. `refkv.build.project(parsed)` runs the built-in projection
over a `Parsed` and returns what `Base` gives today (entries, documents,
arrays, attributes without refkv's declarations, and `elements(path)`, which
maps a built-in array's chunks to IR elements), as checked values, with
`copy` working on it as now. It is a tool beside the others, not the only way
in.

## What a caller writes

An OME-Zarr 0.6 image of a TIFF, without `project`. The 0.6 metadata is the
caller's, and so is turning the image's position into 0.6's translation,
which places the center of a level's first pixel: arithmetic of 0.6, not a
rule of refkv's.

```python
from refkv.build import read, seal, tiff_array, tiff_image
from refkv.zarrify.layouts.hierarchy import array, group

with read("image.ome.tif") as p:
    image = tiff_image(p)                            # name, axes, scale, position and its anchor, levels
    out, datasets = {}, []
    for n, level in enumerate(image.levels):
        out |= array(tiff_array(p, image, level), dimension_names=[a.name for a in image.axes], to=str(n))
        datasets.append({"path": str(n), "coordinateTransformations": [{
            "type": "scale", "scale": level.scale}]})  # and a translation from image.position
    ome = {"version": "0.6", "multiscales": [{"name": image.name, "coordinateSystems": [...],
                                              "datasets": datasets}]}
    out["zarr.json"] = group({"ome": ome})
    seal(out, inputs=[p]).save_vzip("image.vzip")
```

An OME-Zarr 0.6 image of an HDF5 dataset; its axes are the caller's options,
since HDF5 has none. Its attributes are written as
[hdf5.md §6](../../spec/zarrify/hdf5.md#6-attributes) writes them for JSON
(`hdf5_attributes(..., json=True)`), since a decoded NaN is not JSON.

```python
from refkv.build import hdf5_array, read, seal
from refkv.zarrify.coding import hdf5_attributes
from refkv.zarrify.layouts.hierarchy import array, group

with read("ophys.nwb") as p:
    path = "stimulus/templates/movie/data"
    out = array(hdf5_array(p, path), attributes=hdf5_attributes(p, path, json=True), to="movie/0")
    out["movie/zarr.json"] = group({"ome": {...}})   # the 0.6 image, axes from the caller's options
    out["zarr.json"] = group()
    seal(out, inputs=[p]).save_vzip("movie.vzip")
```

The **TIFF stack** of d-v-b/refkv#282 reads every file and seals one
`stack.tif` over all the inputs, instead of parsing them again with
tifffile. Writing a TIFF file is its own output's format, so encoding IFDs is
the caller's code, not a restated rule of refkv's. For each page it walks
the IFD's entries, a tag's repeats (`tags/<tag>~<k>`) included
([tiff.md §5.1](../../spec/zarrify/tiff.md#51-elements)), taking each
entry's tag, type and count from `ir.value`. Most tags are of unknown meaning
to it and are copied uninterpreted, their values' bytes from `parsed.bytes`
into the header blob, so the value stays one `Data` part and a `Ref` per
strip, as the example's is. It reads the strip and tile tables (273, 279,
324, 325), which it rewrites, resolving a page whose `tiles` is an alias of
an earlier IFD's through `ir.target`. It leaves out what points elsewhere in
the old file (SubIFDs, Exif, GPS and Interoperability IFDs, free space: 330,
34665, 34853, 40965, 288, 289) and entries whose value's size cannot be told
(a field type the model does not know) or that lie outside the file. It
refuses what the example refuses: an old-style JPEG's offset (513), a BigTIFF
or mixed byte orders (`header/magic`, `header/byte_order`), a strip outside
the file, a stack of 4 GiB or more, and more files than a reader reads for one
value. The strips are `span`s, not `reference`s, since a JPEG strip's
reference splices in tables the copied JPEGTables tag already gives. Without
checksums each input can be closed once its pages are walked.

A file that names others (one of an OME-TIFF set, whose IR has several
objects) is refused, since reading one file of a set alone is [not in this
proposal](#not-in-this-proposal). The tifffile example reads such a file's
own pages; until refkv can, the stack of a set is that example's, not this
one's.

None of the three needs a plugin. A caller that wants the command line to run
one can still register it.

## Plugins

A plugin is what the command line and the entry-point registry can name:
`python -m refkv.zarrify --layout NAME`, and `zarrify(..., layout=Layout(name,
options))`, its face in Python. That is its whole reason to remain. A plugin
becomes `write(inputs: Sequence[Parsed], options) -> Written`, given the
parsed inputs: `zarrify` takes a sequence of locations with `layout`
(`zarrify(["a.tif", "b.tif"], layout=...)`, `url` refused with several), the
CLI several locations with `--layout`, a store input among them, and
`project(parsed)` gives a plugin that wants it the base. Its `formats` apply
to each input. refkv seals what it returns. The names, options and errors of
[spec/layout.md](../../spec/layout.md) stay; its determinism rule is
restated over the inputs (their descriptions and IRs, every byte read through
them, by `bytes` or the routines, the canonical options and the plugin's
code), and its section on what a plugin is given becomes a pointer to the
tools.

## Testing that the tools suffice

The criterion is tested, not argued, by two kinds of test.

**Rebuilding refkv's own output.** For each of a few formats, the
implementation adds a Python projection written only with the tools of
groups 1 to 4 and `hierarchy`'s `array` and `group` (not `project`, and not
`copy` of a built-in array), and a test that runs it over every fixture of
the format and checks that its output is equivalent to `zarrify`'s as
[zarrify §1.1](../../spec/zarrify.md#11-output-and-equivalence) defines it,
mirror, declarations and source table included. The first are NumPy
(`grid` with a copied edge chunk, `gzip_codecs`; the smallest, for the
guide), TIFF's image layout (`tiff_image`, `tiff_array`, `ome_image`,
`omero`, `mirror`, `declaration`), and HDF5's dataset layout
(`hdf5_array`, `hdf5_attributes`, the description's nodes and links). These
bind the functions the built-in projections call, so they show that the
tools are complete and wired, not that they are the right size for a caller.

**Building outputs refkv does not write.** The three outputs of [What a
caller writes](#what-a-caller-writes) become shipped examples, each built
without `project` and without code that restates a rule of refkv's
conventions (a mapping of OME planes, a strip join, a shard size, a codec
table, a position conversion); a caller's own output format (the TIFF the
stack writes, 0.6's metadata) is its own. Each is tested over its formats'
fixtures by reading every array, or every page, back and comparing it with
tifffile's or h5py's values. A tool too coarse for one of them, so that a
caller must restate a rule to get past it, is split until it is not. NumPy
has no such test: it is the rebuild test's smallest case only.

What either test cannot build is a missing tool, added before the test
passes. The Rust projections stay Rust: the browser and the Rust CLI run
them, and a Python projection is the test of the tools, not a replacement.

## What changes on main

- **Kept**: the codec routines, `cut`, `concat` and the other helpers
  (taking values), `copy` and `group`, the plugin entry points, names,
  errors and CLI, the `bare` example (over `project`), and the `zarrs`
  feature.
- **Changed**: `hierarchy.array` takes an `Array`, `hierarchy.check` the
  inputs; a plugin's `write` takes the inputs; `zarrify` takes several
  locations with `layout`.
- **Extended**: the TIFF parser reads every OME `Image` of an OME-XML
  into the description (`read_ome` in `rust/refkv-ir/src/tiff.rs` reads the
  first today), since OME-XML is known whole; refkv's own output still
  presents the first.
- **Moved**: `Base`'s IR accessors and `fits` to `Parsed` and `refkv.build`;
  its output accessors to what `project` returns; `prepare` and `store_of`
  into `seal`; the TIFF parser's joining, sharding, padding, LZW tails and
  position conversion, and the HDF5 parser's presentation, Zarr types,
  codecs, masked-chunk Adler-32 and strings, into routines the projections
  call, with the checks and reads that go with them; the Rust projections'
  one batch of reads into rounds.
- **Rewritten**: the `ome-zarr-06`, `tiff-pages`, `hdf5-rechunk` and
  `xarray-v2` examples, over the tools; `ome-zarr-06` without `project`.
- **New**: `refkv.build` (`read`, `Parsed`, the values, `grid`, `shard`,
  `tiff_image`, `tiff_array`, `hdf5_array`, `ome_image`, `omero`, `mirror`,
  `declaration`, `source_metadata`, `seal`, `project`, `MAX_PAYLOAD`); each
  format's list of known structures, its description and schema; the tests
  of both kinds.
- **Changed outputs**: every output's source table is numbered by first use
  in key order, so some outputs number their sources differently (a TIFF
  set's, a file read as a store's, a file's JPEG tables' data sources); an
  HDF5 mirror past its room holds fewer storage elements, and no global heap
  collections read only for strings.
- **Specification**: spec/layout.md splits in two. The checks `seal` applies
  become a document of their own (what an output built from parsed inputs
  must satisfy, for any number of inputs); what is left is plugins. Each
  profile's document gains its known structures and description, and its
  source metadata is restated over them. [zarrify
  §1.2](../../spec/zarrify.md#12-input)'s numbering, the documents' own
  source orders and hdf5.md's room and [§8.3](../../spec/zarrify/hdf5.md#83-checks) change as above.

refkv has no users yet, so nothing on main is kept for compatibility alone,
and changing an output is a cost only to the fixtures.

## Order of work

1. **The plumbing**, with no format's facts changed: `read` and `Parsed`
   (with `StoreRun.finish` wrapped), the values and checked values, `seal`
   from `prepare` and `store_of` with the new numbering (and the built-in
   outputs changed to it), the accessors moved from `Base`, `bytes`, and
   `project`. `parsed.description` is `None` for a format until its
   description lands; the facts are not exposed. The TIFF stack rewritten
   over this, its tags from the IR.
2. **The read rounds**: the Rust projections' batch of reads becomes the
   rounds of a step machine, driven by the run in Python, the Rust CLI and
   the browser, with no output changed.
3. **One format at a time**, NumPy, then TIFF, then HDF5, each in two PRs.
   The first lists its known structures, specifies its description and
   tests it against tifffile or h5py. The second sorts its facts and checks
   against them, moves the parser's decisions and their reads into routines,
   adds the routines it needs from group 3, and adds its tests of both
   kinds, which pass before it merges. The 0.6 examples move onto TIFF's and
   HDF5's as they land.
4. **The other formats**, the same way.

## Not in this proposal

- **Parser plugins**, for formats no built-in parser reads.
- **JavaScript.** The WebAssembly module exposes runs and their outputs,
  not IR accessors (`js/src/zarrify/ir/wasm.ts`). The tools are Rust
  functions, so a JS binding of them can follow.
- **Reading a file without the files it names**, such as one file of an
  OME-TIFF set alone: `read` of one file of a set reads the set.

## Alternatives considered

- **Plugins given the base** (this note's previous version, on main). It
  keeps every external output downstream of the built-in projection, reaches
  the IR only through bridges from the base's chunks, takes one input, and
  needs a plugin even for a script. The tools give the same, and more,
  without it.
- **Decoding every structure** (every HDF5 message type, every TIFF tag's
  meaning), so that a caller never meets an unknown. It has no end, and the
  specification of each format would grow by every structure anyone might
  someday want. With the known and unknown line, refkv specifies a structure
  when it decides to understand it, and in the meantime the structure is
  handed over uninterpreted, as a value, bytes or a range.
- **Exposing `parse`'s facts as they are.** A caller would depend on a JSON
  object that changes with every projection change, and the projection
  decisions in it would look like file structure.
- **Markers the caller resolves**, for the bytes a routine needs read (an
  earlier draft of this version). Every routine's reads became the caller's
  to perform and to bound, and `seal` had to tell resolved bytes from a
  caller's own. Routines read for themselves instead.
- **Rewriting the projections in Python.** It would make Python the only
  implementation of each convention; the browser and the Rust CLI need Rust.

## Open questions

1. **Large inputs.** `seal` checks each value in Python; an output of
   millions of chunks needs the check in Rust, over the same rules.
2. **The default of `max_bytes`.** The sum of a convention's read bounds is
   large for a file with many datasets; a caller that reads only a little
   would want a smaller default, and a caller decoding a large unknown
   structure (a CZI attachment) a larger one.
3. **An OME-TIFF's other series in refkv's own output.** `tiff_image` gives
   every OME `Image`; refkv's convention presents the first. Presenting the
   others is a change to the convention, which a caller need not wait for.
