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
  output and without restating any rule a format's document specifies.

Total control of the layout, the earlier criterion, follows: a caller writes
the entries it wants.

## Terms

- **Profile**: refkv's reading of one format, named in an output's root
  (`tiff`, `ndpi`, `hdf5`). Its **convention** is the output it specifies
  (spec/zarrify/<format>.md); its **projection** is the code that writes that
  output from what the parser read. A **layout** is one presentation within
  an output (the image layout, a dataset layout).
- **Facts**: the JSON object a parser returns beside the IR today, the
  projection's other input; unspecified.
- **Element**: a node of the IR, with a path, a kind and an extent in its
  source ([conventions §8.1](../../spec/conventions.md#81-elements)). An
  element's **form** may carry a **recipe**, the parts that make its bytes
  whole, such as a JPEG tile's tables spliced before its scan
  ([conventions §8.5](../../spec/conventions.md#85-forms-and-recipes)).
- **Room**: the bound a source model puts on how many elements an IR holds,
  past which it keeps fewer ([hdf5.md §7.3](../../spec/zarrify/hdf5.md#73-the-ir-mirror)).
- **Written** and **placement**: an output's entries with which bytes entries
  a vzip reader is given at open and which are stored uncompressed
  ([spec/layout.md §3](../../spec/layout.md#3-the-output)).
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
  and moves the translation from the image's center, where OME's `Position`
  puts it, to its corner. The HDF5 parser writes each node's Zarr data type,
  codecs and fill value (`facts.nodes[].array`), reads a masked chunk's
  Adler-32 (`read_skipped` in `rust/refkv-ir/src/hdf5/file.rs`) and the
  strings of presented datasets, and describes the storage of the datasets
  the output presents whatever the room, other objects only while room lasts
  ([hdf5.md §7.3](../../spec/zarrify/hdf5.md#73-the-ir-mirror)). So which
  elements are in the IR depends on the output.
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

refkv's model of a format, its **source model**, specifies each structure's
syntax: the IR element, its type, and so its typed value, `ir.value(i)`
([conventions §8](../../spec/conventions.md#8-the-ir-mirror)). This note
extends the term to meaning: for some structures, the format's document also
specifies what they mean. That gives each structure one of two kinds:

- **Known**: a structure whose meaning the format's document specifies. The
  document lists its known structures and the rules that interpret them: a
  TIFF's byte order, image dimensions, sample format, compression, predictor,
  planar configuration and resolution tags, its OME pixels, planes and
  channels ([tiff.md §3](../../spec/zarrify/tiff.md#3-ome-xml)); an HDF5
  node's dataspace, datatype, layout, chunk index, filters and fill value
  ([hdf5.md §3](../../spec/zarrify/hdf5.md#3-datasets)), its links
  ([§2.3](../../spec/zarrify/hdf5.md#23-links)) and attributes
  ([§6](../../spec/zarrify/hdf5.md#6-attributes)). The interpreted result is
  the format's **description**.
- **Unknown**: everything else the parser keeps as an IR element: a TIFF
  tag whose meaning the document does not give (a private tag, an UNDEFINED
  one), an HDF5 message type it does not decode, a CZI attachment, an MRC
  extended header. An unknown structure is never interpreted into an array
  or an attribute, as today; it is in the IR mirror like every element. A
  caller gets its typed value (`ir.value(i)`) where its type has one, its
  bytes (`parsed.bytes(i)`, [References](#2-references-and-values)), and a
  reference to its extent (`parsed.span(i)`). A caller that needs its
  meaning decodes it.

The built-in projections are built on the known because it is known: a
projection relies only on what the format's document specifies, and an
external projection relies on exactly the same, through the same
description. A projection's use never makes a structure known. The model
grows when refkv decides to understand more of a format, by specifying a
structure's meaning in the format's document; the description then carries
it, and every projection may use it. A structure nobody has modeled stays
unknown, and its bytes are still there.

### The description

The description of a format is its known structures, interpreted: specified
in the format's document beside the rules that interpret them, with a JSON
Schema in `spec/zarrify/<format>/description.json`, versioned with the
convention. It names IR elements by their paths, which the source model
specifies ([tiff.md §5.1](../../spec/zarrify/tiff.md#51-elements)), and
gives a byte range where a structure has no element of its own (an HDF5
dataset's chunks, from its chunk index), so that it does not depend on the
IR's room.

Today's facts are where each format's work starts, not what defines it.
Each format's document lists its known structures first; then every item of
its facts is checked against the list, and is one of:

1. **Derived from a known structure**: it stays, in the description. TIFF's
   `plan`, the planes that lie in other files, is one: it is read from the
   set's OME-XML or Micro-Manager metadata
   ([tiff.md §7.4](../../spec/zarrify/tiff.md#74-several-files)).
2. **A projection's decision**: which strips join into a chunk, when to
   shard and how to pad (TIFF's `join`, `shard`, `pad` and `rectilinear` per
   level, and the LZW tails padding needs); a level's scale as a ratio, and
   the translation moved to the image's corner; HDF5's Zarr data type,
   codecs and fill value (`facts.nodes[].array`), which chunks a filter
   skipped and their Adler-32, the strings and records a presented dataset
   reads. It moves into the projection, as a routine
   ([Building](#3-building)) that the built-in projection and a caller both
   call; a decision that reads bytes reads them there.

An item that is neither is a gap in the document, not a reason to make the
item known: the document is amended first, as for any structure refkv
decides to understand, and the item then sorts as above. Every decision a
built-in output makes is one its convention must already specify
([zarrify §2](../../spec/zarrify.md#2-the-zarr-layout) requires the exact
layout the convention specifies); one it does not is specified in the same
change. The facts' test hook (HDF5's `copy_threshold`, which overrides the
constant `MAX_PAYLOAD` of `rust/refkv-ir/src/out.rs`) moves out of them.

The parsers' rejections sort the same way. A check of the file's own
structure (the reading checks of [tiff.md §7.1](../../spec/zarrify/tiff.md#71-reading)
and [hdf5.md §8.3](../../spec/zarrify/hdf5.md#83-checks), the source model's
budgets) stays in the parser, and `read` raises it. A check of a decision
moves with the decision into its routine, which raises `Rejected`: TIFF's
translation and padding checks (in `tiff.rs` today), HDF5's limit of 2^20
strings presented, and every rejection a convention's layout makes
([tiff.md §7.2](../../spec/zarrify/tiff.md#72-rejection)). The output's own
budgets (references, mirror rows, [zarrify §1.1](../../spec/zarrify.md#11-output-and-equivalence)
and [§1.2](../../spec/zarrify.md#12-input)) are `seal`'s. Each format's
change lists its rejections by side.

With HDF5's chunk index in the description, the IR's room stops depending
on the output: the walk keeps objects and their storage in canonical order
while room lasts, with no exemption for presented datasets. An HDF5 mirror
past its room then holds fewer storage elements than today; the output does
not, since its chunks come from the description.

A format's **source metadata** ([conventions §2](../../spec/conventions.md#2-attributes)),
which its output declares today, is then defined as a function of the
description, and a routine gives it (`source_metadata(parsed, path)`): one
interpretation, written in two places, not two.

The earlier version of this note rejected specifying the facts as "a second,
unversioned description of each format beside its convention", the base
output being that description already. Versioned with the convention, and
the projections' input, the description is not a second one: it is the one
the output and its source metadata are derived from.

## The tools

Five groups, each the public form of something a projection already has or
does. The names are proposals; the Python module is `refkv.build`.

### 1. Reading: `read`

```python
with refkv.build.read("image.ome.tif") as parsed:     # or read_async; url=, policy=, registry=, checksums=
    parsed.profile, parsed.version                   # "tiff", 0 (an NDPI file is "ndpi")
    parsed.ir                                        # refkv.ir.Ir
    parsed.description                               # the format's description
    parsed.sources                                   # tuple[refkv.Source], each url source with its size
```

`read(location, url=None, *, policy=None, registry=None, checksums=False)`
reads a file, or a store, as `zarrify` does (the same sniffing, siblings,
pins, reader policy and object stores) and stops before the projection. It
returns a `Parsed`, which keeps the run's transport open for the reads below
until it is closed. A file read as a store (VSI, NRRD, an HDF5 file with
external storage) gives the multi-source IR of
[conventions §8.9](../../spec/conventions.md#89-stores), and so does a store
input, through `StoreRun.finish`. `read` raises `Rejected` for the parser's
own checks only ([The description](#the-description)). `checksums` is the
run's, fixed here because a shard's ranges join differently with them
([zarrify §2.1](../../spec/zarrify.md#21-virtual-shards)).

### 2. References and values

The IR accessors of `Base` move to `Parsed`: `parsed.reference(i,
member=None)` (an element's bytes as the profile reads them, its recipe
applied), `span(i, member=None)` (its own bytes, no recipe), `members`,
`form`, `element_at` and `source_of`, which refuse what `Base`'s refuse
([spec/layout.md §2](../../spec/layout.md#2-the-base)): a node, a derived
element or one in a derived space, an element of a store's object nothing
references, a chunk whose form skips a filter. `parsed.bytes(i)` is an
element's bytes, from the IR when the parser kept them (`ir.raw`) and
otherwise read through the input's transport, for any element: unknown
structures are structure, and reading them is how a caller decodes one. Its
reads are counted against a per-`Parsed` budget (`read(...,
max_bytes=...)`, 64 MiB by default) so that a loop over a file's elements
cannot read a file's worth by accident.

They return **values**, whose parts name what they read, so that numbering
the source table is left to `seal`:

- `Ref(parsed, source, offset, length)`: bytes of a url source of an input,
  `source` an index of `parsed.sources`;
- `Shared(data)`: a byte string many references share (a JPEG tile's
  tables), which `seal` makes a data source;
- `Zeros(n)`: zero bytes, of the zero data source;
- `Data(data)`: a data source of the caller's own; `cut(Data(blob), a, b)`
  is bytes `a` to `b` of that one source, so a caller can interleave one
  blob with `Ref`s (the TIFF stack's header and IFDs);
- `Literal(data)`: bytes in the reference itself, counted in its payload.

A value is one part or a sequence of them, in order. `cut`, `concat`,
`payload_size` and `fits` (why `seal` would not write a value) take values.
A value a routine of [Building](#3-building) returns is **checked**: it
carries the convention rule it was built under, so `seal` holds it to that
rule rather than to a caller's ([Writing](#4-writing-seal)). `cut` and
`concat` of a checked value give an unchecked one, and an array or `Written`
a routine returns keeps its values checked.

### 3. Building

What exists stays: `cut`, `concat`, `payload_size`, `chunk_key`,
`chunk_coords`, `fill_value`, the codec routines of `refkv.zarrify.coding`
(`tiff_tags`, `tiff_data_type`, `tiff_codecs`, `tiff_byte_order`,
`czi_codecs`, `vsi_codecs`, `dicom_codecs`, `gzip_codecs`, `hdf5_value`,
`hdf5_attributes`), and, with the `layouts` extra, `copy`, `array` and
`group`. The routines that walk the IR (`tiff_tags`, `tiff_byte_order`)
take a `Parsed`; `hdf5_attributes` does once HDF5's description lands; the
rest take plain values.

The helpers projections use become routines, each a binding of the Rust
function the projections call, as the codec routines are. A routine that
needs bytes reads them itself, through its `Parsed`, under the bounds its
convention gives those reads (an edge chunk copied only past the payload
threshold, at most one window sample per channel of at most 2^26 bytes, at
most 2^26 bytes of masked chunks a dataset); a caller never reads for it,
and cannot widen those bounds. The array routines return an `Array`: shape,
data type, codecs, fill value, chunk grid and checked chunks keyed by
coordinate tuples, which `hierarchy.array(a, attributes=..., to=...)`
writes.

- **`grid(parsed, span, shape, item_size, *, limit)`**: a contiguous C-order
  array cut as [conventions §7](../../spec/conventions.md#7-source-values-as-arrays)
  cuts it (`out::grid_chunks`): the chunk shape it chooses, of whole
  trailing slabs so that each chunk is one range, at most `limit` bytes
  (each convention's own: HDF5's is 2^20,
  [hdf5.md §3.2](../../spec/zarrify/hdf5.md#32-chunks)), and an edge chunk
  padded with `Zeros`, or copied when its payload would pass `MAX_PAYLOAD`.
  A caller that wants another chunk shape builds it with `cut` and `concat`,
  a chunk of many ranges, as the `hdf5-rechunk` example does, within `fits`.
- **`shard(parsed, array, shard_shape)`**: an `Array` as virtual shards, as
  [zarrify §2.1](../../spec/zarrify.md#21-virtual-shards) builds them
  (`shard.rs`): its codecs become `sharding_indexed`, with its inner codecs
  and index codecs, and its chunks the shards, joined as the input's
  `checksums` say.
- **`tiff_image(parsed)`**: a TIFF's image as
  [tiff.md §4.1](../../spec/zarrify/tiff.md#41-planes) and
  [§4.2](../../spec/zarrify/tiff.md#42-pyramid-levels) give it: its axes, its
  physical scale per axis with units, its position as the file gives it (OME's
  `Position`, the image's center), and its levels, each with its IFD paths,
  shape, scale and planes. A caller writing a 0.6 image of a TIFF takes its
  planes from here instead of restating the OME and ImageJ mappings.
- **`tiff_array(parsed, level)`**: a level's `Array` as
  [§4.3](../../spec/zarrify/tiff.md#43-data-types-and-codecs) to
  [§4.5](../../spec/zarrify/tiff.md#45-strips) give it, strips joined and
  padded, an LZW strip's tail built from its head, which it reads.
- **`hdf5_array(parsed, path)`**: an HDF5 dataset's `Array` as
  [hdf5.md §3](../../spec/zarrify/hdf5.md#3-datasets) to
  [§5](../../spec/zarrify/hdf5.md#5-object-references) give it: its shape
  (with the trailing axis a fixed-length string takes), data type, codecs and
  fill value (`array_type` and the code behind `facts.nodes[].array` today),
  its chunks from the description's chunk index, a chunk a filter skipped
  framed with its Adler-32, a contiguous dataset cut by `grid`, and a
  dataset of strings, records or references from the bytes it reads, under
  [§8.3](../../spec/zarrify/hdf5.md#83-checks)'s budgets.
- **`ome_image(...)`, `omero(parsed, level)`**: the OME-Zarr 0.5 group
  document the image layouts write (`out::image_ome`), and an image's
  `omero` with its windows, sampled as
  [tiff.md §4.4](../../spec/zarrify/tiff.md#44-arrays) says
  (`out::omero_json`, `rust/refkv-ir/src/project/window.rs`).
- **`mirror(parsed)`**, **`declaration(parsed, ...)`** and
  **`source_metadata(parsed, path)`**: the IR mirror's entries
  (`_core.mirror`), a root's `refkv_zarrified`, and a node's source
  metadata (`out::declare`), for an output that keeps refkv's conventions.

A routine returns a `Written` where it places its entries other than by
default: `mirror`'s documents are kept for later
([conventions §2](../../spec/conventions.md#2-attributes)), and so are the
lazily read nodes a convention places so (CZI's `TILES/`).

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
   source, as long as the most `Zeros` any value holds). A plain file input's
   source 0, the file, is source 0 whether or not a value uses it, as
   [zarrify §1.2](../../spec/zarrify.md#12-input) says. Equal url sources
   (URL and pins) are one; one URL with different pins (the file changed
   between two reads) is refused, and so is a `Ref` to a `Parsed` not among
   `inputs`. Sources no value uses are not in the table. The orders some
   documents give their sources today (a TIFF set's walk of its planes,
   [tiff.md §7.4](../../spec/zarrify/tiff.md#74-several-files); a file read as
   a store, [zarrify §1.8](../../spec/zarrify.md#18-files-read-as-stores)) and
   file runs' numbering of data sources change to this one rule, so a
   built-in table and a sealed one agree.
2. **Checks**, after numbering (a source's index is a varint of the
   payload): every key, every `Ref` against its source's size as `read`
   found it, the payload limit, the archive's limits, and the output's
   budgets of references and mirror rows. An unchecked value reads at most
   1024 url ranges, counted as readers merge them
   ([spec/refkv.md §6.7](../../spec/refkv.md#67-reader-policy) rule 5). A
   checked value is held to the rule it carries instead: an HDF5 strings
   chunk may read more, and a shard is held to the payload rule of
   [zarrify §2.1](../../spec/zarrify.md#21-virtual-shards) and to no read
   rule.
3. **Places** the entries: as the `Written` says, and otherwise every Zarr
   document is read at open.
4. **Checksums** the ranges, when the inputs were read with checksums
   (inputs read with and without them are refused together), through the
   inputs' transports, which stay open until `seal` returns.

The inputs must have been read under the same policy and registry, unless
`policy` and `registry` are given, which the store then keeps, as
`refkv.compose` treats its operands. A failed check is a `ValueError`
naming the key and the rule. `check(written, inputs)`, the problems a Zarr
reader would trip on, stays a separate function for a caller's tests; for
its `refkv` code (a declaration its subtree does not bear out) it runs
`project` on the inputs and compares the subtree with refkv's. An output
keeping refkv's declarations claims to follow refkv's convention, and
**Structure only** ([zarrify §1.2](../../spec/zarrify.md#12-input)) with it;
`seal` does not check the claim.

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
caller's, and so is where its translation puts a pixel (0.6 places a pixel's
center); everything about the TIFF comes from the tools.

```python
from refkv.build import read, seal, tiff_array, tiff_image
from refkv.zarrify.layouts.hierarchy import array, group

with read("image.ome.tif") as p:
    image = tiff_image(p)                            # axes, scale and units, position, levels
    out, datasets = {}, []
    for n, level in enumerate(image.levels):
        out |= array(tiff_array(p, level), dimension_names=image.axes, to=str(n))
        datasets.append({"path": str(n), "coordinateTransformations": [{
            "type": "scale", "scale": level.scale}]})  # and a translation from image.position
    ome = {"version": "0.6", "multiscales": [{"name": image.name, "coordinateSystems": [...],
                                              "datasets": datasets}]}
    out["zarr.json"] = group({"ome": ome})
    seal(out, inputs=[p]).save_vzip("image.vzip")
```

An OME-Zarr 0.6 image of an HDF5 dataset; its axes are the caller's options,
since HDF5 has none.

```python
from refkv.build import hdf5_array, read, seal
from refkv.zarrify.coding import hdf5_attributes
from refkv.zarrify.layouts.hierarchy import array, group

with read("ophys.nwb") as p:
    path = "stimulus/templates/movie/data"
    out = array(hdf5_array(p, path), attributes=hdf5_attributes(p, path), to="movie/0")
    out["movie/zarr.json"] = group({"ome": {...}})   # the 0.6 image, axes from the caller's options
    out["zarr.json"] = group()
    seal(out, inputs=[p]).save_vzip("movie.vzip")
```

The **TIFF stack** of d-v-b/refkv#282 reads every file and seals one
`stack.tif` over all the inputs, instead of parsing them again with
tifffile. For each page it walks the IFD's entries, a tag's repeats
(`tags/<tag>~<k>`) included ([tiff.md §5.1](../../spec/zarrify/tiff.md#51-elements)),
taking each entry's tag, type and count from `ir.value`. Most tags are of
unknown meaning to it and are copied uninterpreted: a value inline in its
entry from the entry's bytes, and one outside it as a `Ref` to its span,
placed in the new file by cutting the header blob around it
(`cut(Data(blob), a, b)`). An entry whose field type the model does not know,
so that its value's size cannot be told, and a value outside the file, have
no span: the stack leaves them out. It interprets only the tags whose values
point into the old file, as the example does today: the strip and tile
tables (273, 279, 324, 325), which it rewrites; SubIFDs, Exif, GPS and
Interoperability IFDs and free space (330, 34665, 34853, 40965, 288, 289),
which it leaves out; and an old-style JPEG's offset (513), which it refuses.
The strips are `span`s, not `reference`s, since a JPEG strip's reference
splices in tables the copied JPEGTables tag already gives. Each page costs a
few ranges, so a stack's pages are bounded by the payload limit and the 1024
reads, as the example's are.

A file that names others (one of an OME-TIFF set, whose IR has several
objects) is refused, since reading one file of a set alone is [not in this
proposal](#not-in-this-proposal). The tifffile example reads such a file's
own pages; until refkv can, the stack of a set is that example's, not this
one's.

None of the three needs a plugin. A caller that wants the command line to run
one can still register it.

## Plugins

`zarrify(location, layout=Layout(name, options))` and `--layout` stay, as the
one thing a function cannot do on its own: be named from the command line
and from `zarrify`. A plugin becomes `write(inputs: Sequence[Parsed],
options) -> Written`, given the parsed inputs: `zarrify` takes a sequence of
locations with `layout` (`zarrify(["a.tif", "b.tif"], layout=...)`), the CLI
several locations with `--layout`, a store input among them, and
`project(input)` gives a plugin that wants it the base. Its `formats` apply
to each input. refkv seals what it returns. The names, options and errors of
[spec/layout.md](../../spec/layout.md) stay; its determinism rule is
restated over the inputs (their descriptions and IRs, the bytes the
routines read, the canonical options and the plugin's code), and its section
on what a plugin is given becomes a pointer to the tools.

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
`omero`, `shard`, `mirror`, `declaration`), and HDF5's dataset layout
(`hdf5_array`, `hdf5_attributes`, the description's nodes and links). These
bind the functions the built-in projections call, so they show that the
tools are complete and wired, not that they are the right size for a caller.

**Building outputs refkv does not write.** The three outputs of [What a
caller writes](#what-a-caller-writes) become shipped examples, each built
without `project` and without code that restates a rule a format's document
specifies (a mapping of OME planes, a strip join, a codec table), and each
tested over its formats' fixtures by reading every array back and comparing it
with tifffile's or h5py's values. A tool too coarse for one of them, so that
a caller must restate a rule to get past it, is split until it is not. NumPy
has no such test: it is the rebuild test's smallest case only.

What either test cannot build is a missing tool, added before the test
passes. The Rust projections stay Rust: the browser and the Rust CLI run
them, and a Python projection is the test of the tools, not a replacement.

## What changes on main

- **Kept**: the codec routines, `cut`, `concat` and the other helpers
  (taking values), `hierarchy` (`copy`, `array`, `group`, `check`), the
  plugin entry points, names, errors and CLI, the `bare` example (over
  `project`), and the `zarrs` feature.
- **Moved**: `Base`'s IR accessors and `fits` to `Parsed` and `refkv.build`;
  its output accessors to what `project` returns; `prepare` and `store_of`
  into `seal`; the TIFF parser's joining, sharding, padding and LZW tails,
  and the HDF5 parser's Zarr types, codecs, masked-chunk Adler-32 and
  presented strings, into routines the projections call, with the checks and
  reads that go with them.
- **Rewritten**: the `ome-zarr-06`, `tiff-pages`, `hdf5-rechunk` and
  `xarray-v2` examples, over the tools; `ome-zarr-06` without `project`.
- **New**: `refkv.build` (`read`, `Parsed`, the values, `grid`, `shard`,
  `tiff_image`, `tiff_array`, `hdf5_array`, `ome_image`, `omero`, `mirror`,
  `declaration`, `source_metadata`, `seal`, `project`, `MAX_PAYLOAD`); each
  format's list of known structures, its description and schema; the tests
  of both kinds.
- **Changed outputs**: every output's source table is numbered by first use
  in key order, so some files' outputs number their sources differently (a
  TIFF set's, a file read as a store's, a file's JPEG tables' data sources);
  an HDF5 mirror past its room holds fewer storage elements.
- **Specification**: spec/layout.md splits in two. The checks `seal` applies
  become a document of their own (what an output built from parsed inputs
  must satisfy, for any number of inputs); what is left is plugins. Each
  format's document gains its known structures and description, and its
  source metadata is restated as a function of it. [zarrify
  §1.2](../../spec/zarrify.md#12-input)'s numbering, the documents' own
  source orders and hdf5.md's room change as above.

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
2. **One format at a time**, NumPy, then TIFF, then HDF5, each in one PR: its
   known structures listed, its facts and rejections sorted against them, its
   description specified, the parser's decisions and their reads moved into
   routines, the routines it needs from group 3, and its tests, which pass
   before the PR merges. The 0.6 examples move onto TIFF's and HDF5's as
   they land.
3. **The other formats**, one per PR the same way.

## Not in this proposal

- **Parser plugins**, for formats no built-in parser reads.
- **JavaScript.** The WebAssembly module exposes runs and their outputs,
  not IR accessors (`js/src/zarrify/ir/wasm.ts`). The tools are Rust
  functions, so a JS binding of them can follow.
- **An OME-TIFF's other series**: the TIFF parser reads one OME `Image`
  (`read_ome` in `rust/refkv-ir/src/tiff.rs`), so `tiff_image` gives the
  first; the IR holds every IFD's tables since #281, so a caller can present
  the others from the IR.
- **Reading a file without the files it names**, such as one file of an
  OME-TIFF set alone: `read` of one file of a set reads the set.

## Alternatives considered

- **Plugins given the base** (this note's previous version, on main). It
  keeps every external output downstream of the built-in projection, reaches
  the IR only through bridges from the base's chunks, takes one input, and
  needs a plugin even for a script. The tools give the same, and more,
  without it.
- **Decoding every structure** (every HDF5 message type, every TIFF tag's
  meaning), so that a caller never meets an unknown. It has no end: each
  format's document would have to specify structures no refkv output uses,
  for callers who may never come, and an interpretation nobody's output
  depends on goes untested. With the known and unknown line, the model grows
  one specified structure at a time, when refkv decides to understand it,
  and the rest is handed over uninterpreted in the meantime.
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

1. **Where a convention places a pixel.** A caller writing OME-Zarr 0.6 needs
   a pixel's center; refkv's conventions put a translation at the image's
   corner. With the move out of the TIFF parser, `tiff_image` gives the
   file's own position, and each projection, refkv's or a caller's, converts
   it as its convention says; a routine for the conversion would keep two
   callers from doing it differently.
2. **Large inputs.** `seal` checks each value in Python; an output of
   millions of chunks needs the check in Rust, over the same rules.
3. **The budget of `bytes`.** A default per `Parsed` is the simplest bound; a
   caller decoding a large unknown structure (a CZI attachment) raises it, or
   refkv specifies the structure as known, with a budget of its own.
