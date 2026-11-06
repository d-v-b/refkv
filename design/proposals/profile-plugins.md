# Proposal: layouts defined outside refkv

Every profile refkv has is compiled into its Rust core, and so is every layout
those profiles write. Someone who wants another presentation of a format refkv
already reads, such as an OME-Zarr 0.6 image of a TIFF or of an HDF5 dataset,
cannot add one without forking the repository and building their own core,
wheel and WebAssembly module. This note proposes **layout plugins**: Python
packages, found the way zarr finds codecs, that write the whole hierarchy of
an output themselves, built from what a built-in profile read: its output and
its IR. refkv ships the routines a plugin would otherwise have to rewrite,
the codec chain of each format whose coding parameters choose its codecs by
one table among them, and ships examples of plugins as part of the package. It is a design note.
d-v-b/refkv#279 implements it, and its rules become normative there in
spec/layout.md and the amendments [The specification
changes](#the-specification-changes) lists.

**The design criterion: a plugin has total control over the output's layout.**
refkv writes no node, path or attribute the plugin did not return; refkv's
own image layout, dataset layout, IR mirror and declarations are in the
output only if the plugin includes them. What refkv decides is outside the
hierarchy, in how the archive holds it: the source table, which is the
base's and so names every source the base read (a sibling file's URL among
them, whatever the plugin keeps); the checksums of the ranges, when the call
asks for them; which entries are compressed and read at open, unless the
plugin says; the entries' order; and the encoding of a value (a `Concat` of
one part is written as its `Range`). refkv enforces what no
layout can change: every reference reads bytes of a source refkv opened,
within it; every key is one the archive can hold; and the output fits the
archive's limits. Everything else a plugin might get wrong is its own to get
right, and
[`check`](#checking-a-layout-in-a-plugins-tests) is how its tests find out.
An earlier version of this note, and the
implementation through 0bf3c397, placed the plugin's layout under
`refkv_layouts/` beside refkv's image layout, declared it with a convention
of refkv's, checked its documents against Zarr v3, and let it reference only
what the base references; [Alternatives considered](#alternatives-considered)
says why each is gone.

New formats (parser plugins) are a larger question, left for later
([Not in this proposal](#not-in-this-proposal)).

## What fixes the set of layouts today

- **The Rust core.** [build.rs](../../rust/refkv-ir/build.rs) generates the
  `Format` enum, the sniff order and the module list from the registry's copies
  in `rust/refkv-ir/profiles/`, inside the crate. A profile is a
  [`FileProfile`](../../rust/refkv-ir/src/run.rs): its signature, its parser,
  how the planner answers it, and its projection, which writes the image
  layout and the format's dataset layout, if it has one. `parser!` is private to
  the crate, so is the trait object behind `AnyParser`, and `Format` is closed.
- **Python.** `zarrify(location, layouts=[Image(), Dataset()])` chooses among
  the layouts a format has ([spec/zarrify.md §1.7](../../spec/zarrify.md#17-named-inputs-and-layouts));
  no other kind of layout can be named, and `python -m refkv.zarrify` has no
  option for layouts at all. A `Source` subclass with an unknown `profile`
  fails in the core (`no format "acme.tiff"`).
- **The conventions.** Every convention's metadata object is named
  `refkv_zarrified`, its URLs are in `d-v-b/refkv`, and the root's `profile`
  is a bare name ([spec/conventions.md §1](../../spec/conventions.md#1-conventions),
  [§2](../../spec/conventions.md#2-attributes)).

Building a store by hand (`refkv.ReferenceStore(mapping, sources)`, or
`ReferenceStore.from_virtualizarr(vds)`) works outside refkv, but nothing
vouches for what it reads: its sources were not opened under pins or a reader
policy, and its ranges were not checked against them. A plugin's output may
leave out refkv's convention and mirror too; what it keeps is that every
reference was checked against a source refkv opened.

## Built from the base and its IR

The first version of this note had plugins turn a built-in parser's IR into a
hierarchy of their own; the second had them reference only what the built-in
output references. Each failed cases the other covers:

- **The IR alone holds too little, too low.** The IR
  ([conventions §8](../../spec/conventions.md#8-the-ir-mirror)) partitions the
  source's bytes into typed elements, but many leaves are opaque. In the HDF5
  IR, every object header message (dataspace, datatype, layout, filters, links)
  is a `bytes[n]` leaf with no value, and objects are named `objects/0`,
  `objects/1`, ...; the dataset names, shapes, data types and attributes are
  in the parser's **facts**, a JSON object that is specified nowhere. And
  turning a format's coding parameters into Zarr codecs (TIFF's compression,
  predictors and sample formats, CZI's pixel types) is the step most likely to go
  silently wrong, which every plugin would have to redo.
- **The built-in output's references are too few.** An HDF5 file's
  contiguous dataset is referenced in slabs of 1 MiB, which no layout can
  re-tile into 2-D chunks; a TIFF's other IFDs (a slide's label and macro
  images, a thumbnail) are not in the TIFF output at all
  ([tiff.md §4](../../spec/zarrify/tiff.md#4-the-image)); a crop, or chunks
  joined into shards a reader wants, need ranges the output does not have.

So a plugin is given both, and what lies between them. The **base**, the
built-in profile's output, holds the decoded metadata the format's convention
specifies: arrays with their shapes, data types, codecs and chunk references,
and nodes with the source's metadata. A plugin that presents the same chunks
another way copies from it, as the dataset layouts of
[conventions §9](../../spec/conventions.md#9-dataset-layouts) already do;
reviewers of an earlier draft ran this note's TIFF example over 177 fixtures
(124 TIFF, 13 NDPI, 8 QPTIFF, 32 IMS), and every copied array read back equal
to the base's; the shipped example's tests run that check over the fixtures. The
**IR** is there for the rest: `base.reference(i)` reads any element's bytes as
the profile would, and the **routines** of [Codecs and data
types](#codecs-and-data-types) turn the parameters the IR holds into the
codecs and data types the built-in profile writes, by the same code, so that a
plugin and refkv cannot disagree. The routines exist for each codec choice a
format's specification makes by one table of its coding parameters; where the profile
decides (how it joins strips, when it shards), a plugin decides for itself.

What stays out of a plugin's reach is what the reader policy and the pins
depend on: a plugin opens no source of its own, reads no sibling, and takes
no part in sniffing. A mirror is the base's to make, since its rules switch
on the profile's name (`rust/refkv-ir/src/mirror.rs`): a plugin that wants
one keeps the base's, entry for entry, and `check` reports a `refkv_source`
that is not (refkv writes whatever the plugin returns there).

## The proposal

### What a plugin is

A package registers a layout under the entry-point group `refkv.layouts`, its
key the layout's name, its value the plugin's class (which refkv calls with no
arguments) or an instance of it. A plugin need not be installed to be used:
`Layout` also takes the plugin object itself, for a notebook or a test.

```toml
[project.entry-points."refkv.layouts"]
"acme-ome.ome06" = "acme_ome:Ome06"
```

```python
from collections.abc import Mapping

from refkv import Concat, Range
from refkv.zarrify import Rejected
from refkv.zarrify.layouts import Base, Written, dump  # new
from refkv.zarrify.layouts.hierarchy import copy       # new; the `layouts` extra

class Ome06:
    name = "acme-ome.ome06"                          # the entry point's key
    formats = {p: {0} for p in ("tiff", "ndpi", "qptiff", "ims", "hdf5")}  # base profiles, convention versions

    def options(self, given: Mapping[str, object]) -> dict[str, object]:
        """The options in canonical form (defaults filled in); raise ValueError if invalid."""

    def write(self, base: Base, options: Mapping[str, object]) -> Mapping[str, bytes | Range | Concat] | Written:
        """The output's entries, keys from its root; raise Rejected for an input it cannot present."""
```

Four modules are new:

- `refkv.zarrify.layouts`: `Base`, `base`, `base_async`, `Layout`, `Written`,
  `Plugin` (a `typing.Protocol` of the attributes above, for annotations),
  the types `EntryValue` (`bytes | Range | Concat`) and `Entries` (a mapping of
  keys to them), `FILE_PROFILES`, `MIRROR` (`"refkv_source"`), `MAX_PAYLOAD` and
  `MAX_READS`, `cut`, `concat`, `payload_size`, `without_checksums`,
  `encode_entries` (an output's entries as bytes, one way only, for a
  determinism test to compare across processes), `dump`, and the error classes
  `LayoutUsageError`, `LayoutRejected` and `LayoutFailed` ([Choosing a
  plugin](#choosing-a-plugin)). It needs nothing beyond refkv.
- `refkv.zarrify.coding`: the routines of [Codecs and data
  types](#codecs-and-data-types): the codec and data type routines, bindings
  of the core's functions that return plain JSON values, and `tiff_tags`, a
  Python helper over `Base`. It needs nothing beyond refkv either; the
  built-in profiles call the same code. Its error is `NoCodec`.
- `refkv.zarrify.layouts.hierarchy`: `copy`, `array`, `group`, `check`,
  `Problem`, `chunk_coords` and `chunk_key` (`refkv.zarrify.layouts`' own,
  which need no extra: a chunk key in an array's encoding, Zarr v3's or a v2
  `.zarray`'s, and back), and `node_name` (Zarr v3's rule for a node's name, for
  checking names an `options()` is given before any base exists), the
  routines that build and check Zarr documents. They lean on
  Zarr's metadata rules throughout, so they use
  [zarr-metadata](https://github.com/zarr-developers/zarr-python/tree/main/packages/zarr-metadata)'s
  models of Zarr v2 and v3 documents (its strict parsers, its round trip to
  JSON) rather than rules of refkv's own, through a new extra,
  `refkv[layouts]` (`zarr-metadata>=0.6`). The module imports `zarr_metadata`
  at its top, so importing it without the extra is an `ImportError` that
  names the extra. A plugin that calls `copy`, `array` or `group` depends on
  `refkv[layouts]`; one that writes its documents with `dump` does not, though
  its tests need the extra for `check`. A plugin whose module cannot be
  imported fails to load, which is a `LayoutUsageError`, its message the
  `ImportError`'s, which names the extra.
- `refkv.zarrify.layouts.examples`: the [examples shipped with
  refkv](#examples-shipped-with-refkv). They reach zarr-metadata only
  through `hierarchy`, so an example that calls it needs the extra, and one
  that does not (`bare`) runs without it.

No other module of refkv imports zarr-metadata.

`formats` maps profile names to convention versions; `formats = "*"` reads
every file profile at every version, for a plugin that does not look at the
format (one that keeps refkv's output and drops the mirror, say). A plugin has no path,
no convention of refkv's making and no key refkv writes: what its output
declares, and where, is its own. A plugin that wants its output to declare a
convention, as the [Zarr conventions
framework](https://github.com/zarr-conventions/zarr-conventions-spec)
describes, writes that convention's metadata object (CMO) itself.

The options a call gives are a `Mapping` (`{}` when the call gives none; the
CLI's `--layout-options` is a JSON object, refused when a member repeats);
`options()` returns them in canonical form, a `Mapping` the plugin chooses
the shape of, and each `write` is given a deep copy of it, so that one
`write` cannot change what the next sees. `dump(value, *, allow_nan=False)` is the JSON refkv
itself writes: compact, UTF-8, and a `ValueError` for `NaN`, infinities and anything else
strict JSON cannot hold, which `json.dumps` writes without complaint (with `allow_nan`, it
writes them bare, as zarr-python writes a Zarr v2 `.zattrs`, and `check` reports `json`).
`fill_value(doc)` is a Zarr v3 array document's fill value as a Python value (a float's
`"NaN"` or bits in hex as a float).

### What it is given: `Base`

`Base` is a read-only view of the built-in profile's output with every layout
the format has (the image layout, the dataset layout if there is one, and
`refkv_source`), its root as refkv writes it, and no checksums (which are of
the source's bytes) or provenance. It depends on the input, the refkv release
(whose conventions are at version 0 until 1.0), the source URL (the root's
`source`), and whether the call asks for checksums, since with them a
profile keeps a shard's ranges apart instead of joining them
([zarrify §2.1](../../spec/zarrify.md#21-virtual-shards)); a plugin sees the
ranges as they are. It does not depend on anything else the call asks for.
`base(location, url=None, *, checksums=False, policy=None)` (and
`base_async`) reads a file as `zarrify` does and returns its `Base`, so that
a plugin's tests and a notebook can call `write` and `check` directly; it
raises what `zarrify` raises for the input, and `LayoutUsageError` for a
store input. A `zarrify` call's `checksums=None` is `False` here too.

- `base.checksums`: whether the call asks for checksums, which the payload
  limit depends on ([What refkv does with it](#what-refkv-does-with-it)).
- `base.profile`, `base.version` and `base.uuid`: the profile that read the
  input, as the root's `refkv_zarrified` names it in `profile` (a TIFF the
  parser found to be NDPI is `ndpi`), and its convention's version and UUID.
- `base.ir`: the IR, as a public `refkv.ir.Ir`, always present (store
  inputs take no layout), with the accessors of `refkv._core.Ir` today (each
  element's path, kind, type, extent, value, form, target and children, and
  its data members) and `at(path)`, an element's index by its path. For a file read as
  a store it is the multi-source IR of
  [conventions §8.9](../../spec/conventions.md#89-stores).
- `base.reference(i: int, member: int | None = None)`: the reference value
  that reads element `i`'s bytes as the profile reads them, or those of its
  data member `member`, an index into the parallel lists
  `base.ir.members(i)` returns (a TIFF's tiles are both elements of their
  own, `ifds/0/tiles/<j>` of kind data, and the members of `ifds/0/tiles`,
  and its strips likewise under `…/strips`; either names the same bytes,
  and `reference` gives the same value for both). The value is a `Range` of
  the source that holds the bytes, when the recipe is the element's bytes
  alone (`[["src", 0, null]]`) or there is none (source 0 for a file IR, the source of the element's object for a
  store IR), or a `Concat` when the element's **form** (the description of
  its bytes the IR keeps beside it, [conventions
  §8.5](../../spec/conventions.md#85-forms-and-recipes)) has a **recipe**,
  the parts that make it whole (a JPEG tile's, which splices its file's
  tables in before its scan). The shared bytes a recipe names are the base's
  **data source** of those bytes ([spec/refkv.md
  §5](../../spec/refkv.md#5-source-table), a source of literal bytes) when its
  table has one, and a literal part otherwise. An alias ([conventions
  §8.1](../../spec/conventions.md#81-elements), a TIFF IFD whose tiles
  another names) is read by its own extent and its own form's recipe (its
  form may differ from its target's, as its IFD's tables may), and its data
  members are its target's; an alias whose extent is not within its
  target's is a `ValueError`. A member that is missing (a tile of byte
  count 0) has no index: the tile's number is `base.ir.members(i)[0][member]`,
  and a plugin leaves its chunk out. It raises `KeyError` for an index or
  member the IR does not have, and `ValueError`, naming why, for an element
  that holds no bytes of a source: a node; an element in a derived space (its
  extent in the inflated bytes of another element, [conventions §8.1](../../spec/conventions.md#81-elements)), or a
  derived element, which no range of a source reads; an element of an object
  of a file read as a store that the base does not reference (whose file
  [zarrify §1.9](../../spec/zarrify.md#19-sources-beside-the-input) leaves out
  of the table, which a plugin cannot add to); or one whose form changes how
  it decodes from its siblings' (an HDF5 chunk whose filter mask skips a
  filter, which one chain of codecs for the array would decode wrongly). It
  reads nothing.
- `base.form(i: int, member: int | None = None)`: the element's (or
  member's) form, parsed: a new `dict` (its `geometry`, `codec` and `recipe`),
  or `None` for one without a form. `czi_codecs`' `flag` and a TIFF tile's
  shape are read from it.
- `base.element_at(source: int, offset: int)`: `(i, member)` for byte
  `offset` of the base's source `source`: the innermost element of kind data
  that holds it, with `member` `None`, or, where the byte is in a data member
  of a struct with no element of its own, that struct and the member's
  index; `None` when no data element or member holds it (a header, a gap),
  and `KeyError` for a source the table does not have. It is the bridge from
  a base array's references to the IR: a plugin that re-tiles an HDF5
  dataset finds the element of its contiguous data from the base's first
  chunk, and reads it whole with `base.reference`.
- `base.elements(path)`: for every chunk of the base's array at `path`, keys
  relative to the array as `base.array` gives them, a tuple with one item per
  element each part of its value runs across (a range of strips the profile
  joined is one item a strip), in order: `(i, member, start, stop)`, the element (as
  `element_at`) and the bytes of it the part reads, `start` to `stop`
  half-open, offsets within the element's (or member's) **span**,
  `base.span(i, member=None)`, the one range of a source its own bytes take,
  no recipe applied (`cut(base.span(i, m), start, stop)` reads them; a JPEG
  tile's `reference` splices its tables in, and so has other offsets), or `None` for a literal
  part or one of a data source (an HDF5 edge slab is its data's range and a
  prefix of the zero data source; a variable-length string's chunk is
  literal bytes and a heap object's), or the part itself, a `Range`, for bytes
  of a url source no data element holds (an HDF5 dataset's external storage).
  `base.ir.parent(i)` then gives the node
  above (a TIFF level's `ifds/<n>`, an HDF5 dataset's `objects/<n>`).
- `base.sources`: the base's source table, a tuple of `refkv.Source`, in its
  order, each url source with its `size`, so that a plugin can tell where a
  range of it ends. For a file IR, source 0 is the input; for a file read as
  a store, it need not be ([zarrify §1.8](../../spec/zarrify.md#18-files-read-as-stores)).
  `base.zero_source` is `(index, length)` of the base's zero data source, or
  `None`; `base.zeros(n)` is a range of `n` zero bytes, of that source when it
  is long enough and otherwise of source `len(base.sources)`, the zero data
  source refkv adds after the table's when the output references it, as long
  as the most bytes the output reads of it (at most 256 MiB, a source table's
  bound), so that a plugin can pad a regular grid's edge chunks of any size;
  a range of it is the same whatever `Base` made it.
- `base.source_of(i)`: the index in the table of the source that holds
  element `i` (for a store IR, its object's: an element's extent is in its
  object, [conventions §8.9](../../spec/conventions.md#89-stores)), and
  `base.members(i)`, element `i`'s data members as `(number, start, length)`,
  an alias's being its target's.
- `base.fits(value)`: why refkv would not write a reference value in this
  base's output (step 1 of [What refkv does with
  it](#what-refkv-does-with-it), with the most checksums can add when the
  call asks for them), or `None`, so that a plugin can choose a chunk shape
  that fits before it returns.
- `base.datasets`: the base's dataset layouts, by the name the root gives
  each, each its node's path; `base.mirror`: the IR mirror's path,
  `"refkv_source"`, or `None`; `base.nodes(*, mirror=False, datasets=False)`:
  its nodes' paths, the root `""`, the image layout's, and the mirror's and
  the dataset layouts' as asked; `base.node_type(path)`: `"group"`,
  `"array"`, or `None` for no node; `base.base_value(value)`: the base's own
  value equal to `value` but for checksums, or `None`.
- `base.keys()`, sorted, and `base.document(key)`, a JSON document (a
  `zarr.json`) as a dict.
- `base.attributes(path)`: a node's attributes without refkv's declarations,
  that is, without `refkv_zarrified`, `refkv_provenance` and the CMOs of
  refkv's conventions in `zarr_conventions` (dropped when nothing is left in
  it). For an HDF5 base these are the source's own attributes and the members
  [hdf5.md §2.1](../../spec/zarrify/hdf5.md#21-declaration-and-attributes)
  adds to describe the node (`_LINKS`, a group's links by the base's paths;
  `_DTYPE` and `_REFERENCE_FIELDS`, which say how to read an array's
  values), which stay: a plugin that moves a group drops or rewrites
  `_LINKS`. The attributes are as the base wrote them: an HDF5 attribute that
  JSON cannot hold is written as
  [hdf5.md §6](../../spec/zarrify/hdf5.md#6-attributes) says, all of whose
  rules a plugin that writes them elsewhere must undo: `"NaN"`, an odd NaN
  or −0 as its bit pattern in hex such as `"0x7fc00001"`, an integer beyond
  2^53 as its digits, a netCDF `_FillValue` of a float dataset as the Base64
  of its binary64, a one-element array as its one value.
  For an HDF5 base, `source_metadata(path)["address"]` is the node's object
  header's address, the start of the extent of its `objects/<n>` in the IR.
- `base.source_metadata(path)`: the source metadata `S` a node declares,
  the member named after the profile in its `refkv_zarrified`
  ([conventions §2](../../spec/conventions.md#2-attributes)), or `None`: a
  TIFF root's byte order and BigTIFF flag ([tiff.md §5](../../spec/zarrify/tiff.md#5-source-metadata);
  its tags, and its OME-XML in tag 270, are the IR's values, by
  `tiff_tags`), an HDF5 node's datatype, layout and attribute types (which
  say which strings above are numbers). A plugin that keeps it writes it
  under a member of its own, where it chooses; refkv's member names are for
  refkv's declarations.
- `base.array(path)`: an array's document, as a dict, and its chunks, a
  mapping from chunk key (relative to the array) to value, each a `Range`,
  `Concat` or bytes of the base.
- `base.entries(prefix="", *, declarations=True, mirror=True, datasets=True)`: a new `dict` of the base's
  entries whose keys start with `prefix`, keys as the base holds them and
  references without checksums, as everywhere in `Base`; without
  `declarations`, each `zarr.json` with its node's `attributes` as
  `base.attributes` gives them; without `mirror` or `datasets`, the IR
  mirror's or the dataset layouts' entries left out. `out |= base.entries()` keeps the whole of
  refkv's output; `out |= base.entries("refkv_source/")` keeps the IR mirror,
  so that the source can still be rebuilt from the output. `key in base` asks
  whether the base has an entry.

Each accessor returns new objects, which the plugin may change; `document`,
`attributes`, `source_metadata`, `array` and `elements` raise `KeyError`,
naming the key
or path, for one the base does not have. Paths are keys' prefixes, with no
leading `/`: an HDF5 dataset `/acquisition/raw/data` is
`acquisition/raw/data`. `Base` has no way to read a chunk, and **Structure only**
([zarrify §1.2](../../spec/zarrify.md#12-input)) binds a plugin with no
exception: the values the IR holds are the structure the parser read. The
parser's facts are not in `Base` either: they are the built-in projection's
private input and change whenever it does. What a plugin needs from them has
a routine instead ([Codecs and data types](#codecs-and-data-types)).

`cut(value, start, stop)`, in `refkv.zarrify.layouts`, is the reference value
that reads bytes `start` to `stop` of what `value` reads: a `Range` when they
fall in one part, else a `Concat` of the parts they span, the first and last
cut (a part of a data source stays a range of that data source).
`concat(*values)` is the one flat `Concat` of the parts of `values` (each a
`Range` or `Concat`), in order, a `Range` when there is one part. A plugin
re-tiles a contiguous array with them, a row of a 2-D tile at a time;
`cut` raises `ValueError` for bounds outside `value`, and neither reads
anything. `payload_size(value, *, checksums=False)` is a value's payload in
bytes as step 1 below measures it, with checksums the most they can take (6
bytes a source range: a `crc32c` field is 2 to 6 bytes, known only once the
bytes are read), so that a plugin can keep a `Concat` of many cuts within
the limit whatever the checksums turn out to be; an array whose last chunk on an axis is short
takes a `rectilinear` grid there rather than padding it with literal zeros,
which count against the payload. A plugin may instead pad with `base.zeros(n)`,
ranges of a **zero data source** (the source of zero bytes some profiles pad
short chunks with, [zarrify §1.2](../../spec/zarrify.md#12-input), or the one
refkv adds), as many as the padding needs (a right-edge tile pads every row).
`base.fits(value)` says whether a value so built is one refkv writes. Which a
plugin does is its choice, not a rule refkv checks: `check`'s `size` finds an
edge chunk of a `regular` grid that is not padded. `concat(*values,
merge=False)` raises `ValueError` for no values; with `merge`, it joins
ranges that continue one another.

### What it returns

`write` returns the output's entries, keys from the output's root
(`zarr.json`, `0/zarr.json`, `0/c/0/0/0`, ...): bytes entries the plugin makes
(documents among them), and reference values, the base's, those
`base.reference` gives, or any the plugin builds within the rule of [What
refkv does with it](#what-refkv-does-with-it). The output has exactly these
keys. A plugin may write any hierarchy, Zarr v3 or v2 or
neither, with any metadata; whether it is a valid one is the plugin's to
ensure, and [`check`](#checking-a-layout-in-a-plugins-tests) is how its tests
can ask. Values are `bytes` (a `bytearray` or `memoryview` is taken as
its bytes), `Range` or `Concat`; anything else, or a `write` that returns
neither a `Mapping` nor a `Written`, is `LayoutFailed`.

It may instead return
`Written(entries, read_at_open=None, stored=())`, `entries` that mapping,
`read_at_open` an iterable of `str` keys of bytes entries a reader is given
when it opens a vzip archive, and `stored` an iterable of `str` keys of bytes
entries kept uncompressed (an entry already compressed, say)
([below](#what-refkv-does-with-it)). refkv reads each once, into a set.
`read_at_open=None` keeps the default rule of step 2 below; a key either
names that is not a bytes entry of `entries` is `LayoutFailed`.

`copy(base, path, order=None, attributes=None, dimension_names=None, *, to=None)` copies
an array, by rules spec/layout.md states, so that two producers copy alike:
it returns an array's entries, keys relative to the array, its `zarr.json`
among them. Its document is the base's with `attributes` as given (`{}` by
default; `base.attributes(path)` gives the source's own) and
`dimension_names` as given, in the copy's axis order and not permuted (else
the base's, permuted with the axes). Given `order`, a permutation of
the array's axes as integers, as `numpy.transpose` takes it (the copy's axis
`i` is the base's axis `order[i]`: `[0, 3, 1, 2]` makes `(t, y, x, c)`
`(t, c, y, x)`), the array is transposed: the shape, the chunk grid (each
axis's list, for a `rectilinear` grid), `dimension_names` and every chunk key
are permuted (in the array's chunk key encoding, `default` or `v2`, with its
separator), and a `transpose` codec of the inverse order `q` goes first,
composed with a `transpose` of order `t` the base already starts with into
one of order `[q[t[i]] for each i]`, and left out when that is the identity.
A sharded array keeps `sharding_indexed` first, with its shard shape
permuted, its inner `chunk_shape` permuted, `transpose(inverse)` first in its
inner codecs and `transpose(inverse + [n])` first in its index codecs, `n`
the rank. A channel-last HDF5 image then becomes an OME-Zarr image in time,
channel, space order and decodes to the same values.

`copy` is a convenience, not a gate: a plugin may build an array by hand,
around the base's references (with a size-1 axis added for OME-Zarr 0.4's five
dimensions, say, or only some of its chunks) or around references of its own
from the IR, and refkv writes it. That such an array decodes to the right
values is then the plugin's to show. `array(shape, data_type, chunk_shape,
codecs, chunks, dimension_names=None, attributes=None, fill_value=None, *,
to=None)` writes one by hand, raising `ValueError` (chaining zarr-metadata's error) for
a document zarr-metadata's model refuses (`data_type` and `fill_value` as Zarr v3 writes them in
JSON, a `str` or `dict` and a JSON value; `chunk_shape` a list of `int`, or
of lists for a `rectilinear` grid, each length an `int` or a `[length, count]`
run as refkv's own grids write them): a Zarr v3 array document, through zarr-metadata's model
(which refuses one it does not hold valid), with a `regular` grid, or a
`rectilinear` one when `chunk_shape` gives each axis's lengths, the `default`
chunk key encoding with the separator `/`, and the fill value 0 of the data
type unless given; it returns one `dict` of entries keyed relative to the
array (or, with `to`, from the output's root below that path, as `copy`'s
`to` does): its `zarr.json`,
and `chunks` (a mapping from chunk coordinates, tuples of `int`, to values)
at their keys. `group(attributes=None)` is a group's `zarr.json`, as bytes. A plugin that writes Zarr v2, or something else, writes its own
documents with `dump`.

`write` is a plain function. refkv runs it, as it runs a plugin's loading and
`options()`, on a thread of its own, so it does not hold up the caller's event
loop and may call refkv's blocking API, `zarrify` included, without
exhausting a shared pool. Only the values `Base` gives, and those another
plugin's `write` gives for the same `Base`, are this output's: a nested
`zarrify` call's references are of its own source table. Cancelling `zarrify_async` does not stop a `write`
already running. Whatever a plugin raises, `SystemExit` and
`KeyboardInterrupt` included, ends the call with one of the errors below.

### Codecs and data types

`refkv.zarrify.coding` turns a format's coding parameters into the Zarr
codecs and data type the built-in profile writes. Each routine is a binding of
a Rust function the profile itself calls, so a plugin and refkv agree by
construction. Most are refactorings: `out.rs`'s `tiff_codecs` is one today,
but the TIFF data type is read from the parser's facts (`project/tiled.rs`),
and becomes a function of the tags both call; the CZI output's codecs are
`project/czi.rs`'s `codecs`, which becomes public; DICOM's choice, inline in
its projection, and VSI's, in `project/vsi.rs`, become functions both call;
Zarr v2's are not among them: Zarr v2 is read as a store, and a store input
takes no layout. Each is
tested over every fixture of its format: given what the IR or the source
metadata holds for one of the base's arrays, it returns what that array
holds. Codecs are returned as Zarr v3 codec objects (plain `dict`s), a data
type as Zarr v3 names it (a `str`, or a `dict` for a structured type). A
codec routine exists for each format whose section chooses its codecs by one
table of coding parameters the IR holds, since that is the step most likely
to go silently wrong; a data type routine exists where the IR describes
arrays the base does not present (a TIFF's other IFDs). A format whose data type is one table of a header field
(CZI's pixel types, LIF, FITS, NRRD, DICOM, NIfTI, NumPy, MRC, safetensors)
has no data type routine: the base presents every array its IR describes, with its type.

| routine | specified in |
|---|---|
| `tiff_tags(base, i: int) -> dict[int, Value]`: the tags of the IFD that is IR element `i` (`ifds/<n>`, `ifds/<n>/subifds/<j>`, an Exif IFD) by number, each the IR's value as it is (`Value` is `refkv.ir.Value`: `int`, `float`, `str`, `bytes`, or a `list` or `dict` of them; a count-1 tag is a one-item list, as the IR holds it; a rational a pair), `None` for a value the IR does not keep (text of more than 1024 bytes, a value outside the file), and only a tag's first entry (the profile reads the first; later ones are `tags/<tag>~<k>` in the IR) | [tiff.md §5.1](../../spec/zarrify/tiff.md#51-elements), [§5.3](../../spec/zarrify/tiff.md#53-values) |
| `tiff_data_type(tags) -> str`: from SampleFormat and BitsPerSample, packed samples held in the smallest type that takes them | [tiff.md §4.3](../../spec/zarrify/tiff.md#43-data-types-and-codecs) |
| `tiff_codecs(tags, *, little_endian: bool, x_axis: int, row_length: int, chunk_shape=None) -> list[dict]`: from Compression, Predictor, BitsPerSample, SampleFormat, SamplesPerPixel, PlanarConfiguration, PhotometricInterpretation, FillOrder and YCbCrSubSampling (and, for EER, ImageLength, TileWidth and tags 65007 to 65009); `x_axis` the index of `x` in the chunk as stored (after any `transpose`), `row_length` the samples in a stored row of a chunk (its width, times SamplesPerPixel when they are interleaved), which packed samples need, and `chunk_shape` a chunk's `[rows, columns]`, which EER's codec decodes one shape of. For an RGB image of interleaved samples, stored `(y, x, s)` and presented `(c, y, x)` after a `transpose`, `x_axis` is 1, the index in the stored order | [tiff.md §4.3](../../spec/zarrify/tiff.md#43-data-types-and-codecs) (TIFF, NDPI, QPTIFF) |
| `tiff_byte_order(base, i: int) -> bool`: whether the file that holds IR element `i` is little-endian, its own header's (a set read as a store may mix them) | [tiff.md §5.1](../../spec/zarrify/tiff.md#51-elements) |
| `vsi_codecs(codec: str, item_size: int) -> list[dict]`: a tile file's codec by its name | [vsi.md §4.1](../../spec/zarrify/vsi.md#41-the-pyramid) |
| `czi_codecs(pixel_type: int, compression: int, *, flag: bool) -> list[dict]`: `flag` the form's (Zstd1's hi-lo packing, a JPEG stream's being lossless), which `base.form` gives; the chain after `transpose`, which `project/czi.rs`'s `codecs` puts first for several samples and which is split off for the routine | [czi.md §3.1](../../spec/zarrify/czi.md#31-pixel-types-and-codecs) |
| `dicom_codecs(transfer_syntax: str, data_type: str, *, samples: int) -> list[dict]`: the codec after any `transpose` (which 3 interleaved samples take, but for RLE, whose frames decode to planar ones), the byte order the transfer syntax's; `data_type` the base array's | [dicom.md §2.1](../../spec/zarrify/dicom.md#21-transfer-syntax) |
| `gzip_codecs(skip: int, level: int) -> list[dict]`: `gzip` of compression level `level` (the NRRD profile's 1, the others' 6), with `refkv.skip` of the first `skip` bytes of the inflated stream (a NumPy file's preamble and header) before it when `skip` is not 0 | [npy.md §4.4](../../spec/zarrify/npy.md#44-the-refkvskip-codec) |
| `hdf5_attributes(base, path) -> dict`, `hdf5_value(value, datatype)`: an HDF5 node's attributes with the encodings JSON needed undone by their described datatypes (float values, integers past 2^53, a netCDF `_FillValue`'s Base64), Python functions over the base | [hdf5.md §6](../../spec/zarrify/hdf5.md#6-attributes), [§7](../../spec/zarrify/hdf5.md#7-source-metadata) |

The routines take what a plugin can get: the IR's values (a TIFF's tags are
values of its IR), the form of an IR element, or a node's source metadata. An
HDF5 file's object header messages are leaves whose bytes the IR does not
keep, so there is no routine over them: the base presents every dataset whose
filters a reader's codecs undo, with its codecs, and a plugin that re-tiles
one takes them from the base's array, whose data type is there too (the
profile chooses it from more than the datatype: a string's, a compound's
records). Each routine applies every rule role (the source model's checks,
`rust/refkv-ir/schema/<format>.json`) the profile applies to those
parameters, since for an IFD the base does not present the routine is the
only check: `tiff_codecs` applies TIFF's `format`, `image`, `samples` and
`predictor`, `tiff_data_type` its `format` and `image`; the codec functions
themselves refuse nothing (an unknown compression falls through to `bytes`).
A routine raises `NoCodec` (a
`ValueError`) for parameters the format's section rejects (a TIFF
compression with no codec, JPEG of a photometric interpretation the section
does not take), naming them; a plugin turns it into `Rejected`. A codec chain
starts after any `transpose`, which is the array's (a TIFF's samples, a
CZI's), and so the plugin's. Where the profile decides rather than the format
(which strips it joins into a chunk, when it shards), or where a codec's configuration is built
from many structures of the file (`refkv.stream` for MP3, FLAC and MP4
tracks, `refkv.grib`, `refkv.fits_tiles`), there is no routine: a plugin
copies those arrays from the base, or decides for itself. A routine for one
of the latter, taking the IR element it describes, can follow when a plugin
needs it, without changing the others.

### What refkv does with it

1. **Checks what no layout may change.** A failed check is the plugin's
   error, `LayoutFailed`. refkv first replaces each value equal to one of the
   base's, checksums aside, by the base's own value, checksums included; it
   strips the checksums of every other range (a checksum the plugin set is
   not trusted), and writes a `Concat` of one part as that part's `Range`, as
   [spec/refkv.md §4.4](../../spec/refkv.md#44-reference-payloads) asks of
   writers. The checks run on the values it will write.
   - **Keys.** Every key is a `str` the archive can hold
     ([spec/refkv.md §2.1](../../spec/refkv.md#21-keys),
     [vzip §10.3](../../spec/bindings/vzip.md#103-file-names)): non-empty,
     valid UTF-8 (no lone surrogate), at most 65535 bytes, and not beginning
     with `__vz__/`, the names the ZIP binding keeps for its own entries
     ([vzip §11.4](../../spec/bindings/vzip.md#114-hidden-entries)). Keys are
     otherwise the plugin's: `a/../b` is an ordinary key, and a tool that
     extracts an archive to disk does not trust keys as file names
     ([spec/refkv.md §8](../../spec/refkv.md#8-security-considerations)).
   - **References.** Every value is `bytes` or a reference value, a `Range`
     or a `Concat` whose `parts` is a `tuple` of `Range`s, of those very
     types, and each `Range` is one of two kinds. A literal has `data` of type
     `bytes` and `source`, `offset` and `length` all 0, and no `crc32c`. A
     source range has `data` `None`, and `source`, `offset` and `length` of
     type `int` (not `bool`), none negative, with `source` an index of the
     base's table and `offset + length` at most 2^64 − 1 and within the
     source: within a url source's size (the size refkv found when it
     opened the source, `Output.size` for the input and the listed sizes for
     a file read as a store, whatever its pin), or within a data source's
     bytes (`offset + length ≤` its length, which a reader needs of any
     source, [spec/refkv.md §6.3](../../spec/refkv.md#63-resolving-a-reference),
     so that a prefix of the zero data source of [zarrify
     §1.2](../../spec/zarrify.md#12-input) pads a chunk as the base's do). The
     base's table holds url and data sources only (refkv's outputs have no
     `key` source), so a range of a `key` source, or of an index past the
     table, is refused. Every reference's payload, as written (its `crc32c`
     fields included, when the call asks for checksums), is at most 65519
     bytes, as [zarrify §1.2](../../spec/zarrify.md#12-input) (**References
     stay in the file**) requires of every output and [vzip
     §11.3](../../spec/bindings/vzip.md#113-reference-entries) of every
     archive; a reference that passes without checksums and fails with them
     fails, as the base's do; the total size of a value's ranges is at most
     2^64 − 1 ([spec/refkv.md §4.3](../../spec/refkv.md#43-concat)). A
     reference that is not the base's reads at most 1024 url ranges, counted
     as the repository's readers merge them (ranges of one source at most
     64 KiB apart are one read), so that those readers, with [spec/refkv.md
     §6.7](../../spec/refkv.md#67-reader-policy) rule 5's default of 1024
     reads per `get`, read it; this is refkv's own bound, as coalescing is
     only a SHOULD, and the base's values (an ND2 frame's padded rows, a
     virtual shard's inner chunks) are not held to it. The values that are not the base's get
     checksums, when the call asks for them, through the callback the base's
     use, which reads the ranges as the base's are read, siblings
     included. So the output reads
     only bytes of sources refkv opened, under its pins and reader policy,
     and within them. This is the rule the plugin interface exists to keep,
     and the only one about the source.
2. **Writes exactly what was returned.** The output's mapping is the plugin's
   entries, with each reference equal to one of the base's replaced by the
   base's own value. Its source table is the base's, with nothing added but
   the zero data source `base.zeros` asked for, when the base has none that
   long (a literal a plugin repeats stays a literal), in the base's order, with its `page_size`, `url`,
   reader policy and concurrency. Which bytes entries a vzip reader is given
   at open is a placement choice of the vzip binding
   ([vzip §14.2](../../spec/bindings/vzip.md#142-recommended-layout-informative));
   a store written in refkv's other binding, phkv, places entries by its own
   size threshold (`inline_max`), and ignores `read_at_open` and `stored`,
   which are placement hints of the vzip binding. The entries read at open
   are the set `read_at_open` names, when `write` returned `Written` with one
   (each a key of a bytes entry of the output, as each of `stored` must be,
   else `LayoutFailed`); otherwise, a bytes
   entry equal to the base's at the same key is read at open when the base's
   is (the base keeps its mirror's documents for later, as
   [conventions §2](../../spec/conventions.md#2-attributes) asks), and any
   other bytes entry is read at open when its key's last segment is
   `zarr.json`, `.zgroup`, `.zarray`, `.zattrs` or `.zmetadata`. A bytes entry
   is compressed unless it is read at open, is in `stored`, or is one whose
   DEFLATE stream could reach 0xFFFFFFFF bytes (zlib's worst-case bound for
   its size), which [vzip §14.1](../../spec/bindings/vzip.md#141-requirements)
   does not let a writer write compressed. Entries are in the order outputs have
   today, reference entries by key and then bytes entries by key.
3. **Checks what the archive needs.** The bytes entries read at open total
   at most 256 MiB, refkv's own bound (the size of the default
   `max_format_entry` of [spec/refkv.md
   §6.7](../../spec/refkv.md#67-reader-policy) rule 6, which bounds a
   binding's own data), so that what a reader is given at open stays as
   small as refkv's own outputs keep it; refkv builds the
   output's archive structure once, as `save_vzip` would, and an output its
   writer refuses (a page index of 0xFFFFFFFF bytes or more, say), or whose
   page index or source table is more than 256 MiB (the default
   `max_format_entry` of [spec/refkv.md §6.7](../../spec/refkv.md#67-reader-policy),
   which a reader applies to both, [vzip
   §13.1](../../spec/bindings/vzip.md#131-opening)), is `LayoutFailed` from
   `zarrify`, not a `ValueError` from `save_vzip` or a reader's refusal
   later.
   The checks and the writing run on a thread, as the projection does today.
4. **Reports it.** `info["layouts"]` is the plugin's name, as a one-element
   list; the rest of `info` is the base's.

The checks guard against a plugin's mistakes, not against a hostile plugin: a
plugin is Python code running in the caller's process, and can do anything the
caller can.

### Checking a layout in a plugin's tests

`check(base, entries)` (or `check(base, written)`), in
`refkv.zarrify.layouts.hierarchy`, returns the problems it finds, a list of
`Problem(key, code, message)` (`key` `None` for a problem of the output as a
whole: what `write` returned, the bytes read at open, the archive), `code` one of:

| code | the problem |
|---|---|
| `key`, `reference`, `open`, `archive` | what `zarrify` refuses ([What refkv does with it](#what-refkv-does-with-it)): a key the archive cannot hold; a value that is not bytes or an allowed reference; a `read_at_open` or `stored` key naming no bytes entry; more than a reader reads at open, or an archive structure the writer refuses (`check` builds it as step 3 does). These are never allowed: `zarrify` fails on them, and `check` finding none of them means `zarrify` writes the output |
| `document` | a reference at a key whose last segment is a Zarr document's name, which a reader would read as JSON |
| `orphan` | a key with no node above it: a chunk without its array's document |
| `chunk` | a key below an array (Zarr v3 or v2) that is not one of its chunk keys, in its chunk key encoding or `dimension_separator`, or is outside its grid |
| `json` | a Zarr document (`zarr.json`, `.zgroup`, `.zarray`, `.zattrs`, `.zmetadata`) is not strict JSON: a byte order mark, `NaN` or an infinity, a member twice in one object, a lone surrogate |
| `zarr` | a Zarr document zarr-metadata's strict parser refuses (a v3 `zarr.json`, a v2 `.zarray`, `.zgroup` or `.zmetadata`), its message the parser's, a `.zattrs` that is not an object, a Zarr v3 member zarr-metadata leaves unclaimed; or a node's name starts with `__`, which Zarr v3 reserves |
| `consolidated` | a `.zmetadata` whose entries differ from the Zarr v2 documents below it, leave one out, or name one the output does not hold |
| `parent` | a node whose parent has no group document, or a node inside an array |
| `copy` | a Zarr v3 array whose references are all the base's is not a `copy` of the base array that holds them, each reference at the key `copy` gives it, attributes and dimension names aside: a hand-edited shape, a chunk moved |
| `size` | an array whose codecs leave a chunk's size fixed (Zarr v3: `transpose`, then `bytes`, and nothing after; Zarr v2: no compressor and no filters) has a chunk whose reference reads more or fewer bytes than the chunk holds, its shape's product times the element's size (for a `rectilinear` grid, that chunk's own shape): a slab cut at the wrong place, an edge chunk of a `regular` grid not padded |
| `refkv` | refkv's members (`refkv_zarrified`, `refkv_provenance`, refkv's CMOs) on a node, in a Zarr v3 or v2 document or in consolidated metadata, other than the base's at that key; or a node that keeps them whose subtree is not the base's, entry for entry, with nothing added |

`check(base, base.entries())` finds nothing, which the implementation's
tests assert over every fixture. `check` applies the payload limit as the
call `base` came from will, with or without checksums (`base.checksums`),
and raises `TypeError` for arguments that are not a `Base` and a mapping or
`Written`. refkv never calls `check`; a
plugin's tests call it, and may allow the codes they expect for the keys they
expect (an array built by hand around the base's references is not a copy; a layout beside refkv's whole
output adds to the subtree refkv's root declares, where a reader of the
format's convention may take it for part of the source). The guide,
docs/layout-plugins.md, shows a
test that gets a `Base` from each fixture with `layouts.base`, runs `write`
and `check` on it, opens the output with zarr, and runs `write` again in a
second process with another `PYTHONHASHSEED` to compare the two outputs.

### Choosing a plugin

A layout is chosen by name, never by sniffing, through a new argument,
`layout`, which takes the place of `layouts` (the built-in layouts): a call
names one or the other, since a plugin's output is the whole output.

```python
from refkv.zarrify import zarrify
from refkv.zarrify.layouts import Layout

zarrify("image.ome.tif", layout=Layout("acme-ome.ome06"))
zarrify("ophys.nwb", layout=Layout("acme-ome.ome06", options={"images": [...]}))
```

`Layout(plugin, options=None)` names a plugin, by its entry point's name or
as the plugin object itself (an instance, or a class refkv instantiates with
no arguments, as for an entry point) (which skips the entry point and the name's
distribution check, but not the other checks of its attributes), and its
options. `layout` takes one `Layout`; `layouts=None` beside it is the
default, and any other `layouts` is a usage error. `python -m refkv.zarrify`
gains `--layout NAME` and `--layout-options JSON`, each at most once, the
second only with the first. One plugin owns a call's output; a plugin
that wants several presentations in one archive writes them all, and may
import another plugin and call its `options` and `write` with the same `Base`
to do it. A plugin is loaded, and its options checked, when it is named,
before the input is opened; `options()` cannot know the input's profile, so a
plugin whose options matter for one format only makes them optional, and
rejects an input that needs them from `write`.

| what goes wrong | error | CLI exit |
|---|---|---|
| no entry point of the name, installed entry points that cannot be read, a plugin that fails to load or whose attributes break the rules below (its `name` other than the entry point's key, or `formats` neither `"*"` nor a mapping of names to iterables of `int`, say), options the plugin refuses (a `ValueError` from `options()`), canonical options that are not JSON as they are (and so cannot be copied for each `write`), `layout` beside a `layouts` other than `None`, a `layout` that is not one `Layout`, `--layout` or `--layout-options` given twice, `--layout-options` without `--layout` or not a JSON object, a layout on a store input | `LayoutUsageError` (a `ValueError`) | 2 |
| a base profile or version the plugin does not read, a `Rejected` from `write` | `LayoutRejected` (a `Rejected`) | 3 |
| a failed check; any other exception from `options()`, or any exception from `write`, `SystemExit` and `KeyboardInterrupt` included (its cause chained; the CLI prints its traceback), among them the `KeyError` or `ValueError` of a helper `write` called and did not catch (`copy` of a path that is no array, an `order` that is no permutation, an array zarr-metadata refuses, a `NoCodec`) | `LayoutFailed` | 1 |

A layout on a store input (a directory, or a URL ending in `/`) is a usage
error for now; a file read as a store (VSI, NRRD, GRIB, an HDF5 file with
external storage) is a file input and takes layouts. Nothing a plugin does can
change which profile reads a file or what the profile accepts. A
`LayoutRejected` refuses the layout request, which depends on the input (a
sniffed file's profile is not known in advance), so it is a rejection, and
like every rejection writes no output.

### Names, versions and determinism

- **Names.** `<distribution>.<layout>`: segments of `[a-z0-9-]+` joined by
  `.`, the first the normalized name ([PEP 503](https://peps.python.org/pep-0503/))
  of the distribution that provides the entry point, which refkv checks, so
  that independent authors need no registry and one package cannot take
  another's name. Names whose first segment is `refkv` are refkv's, for its
  [examples](#examples-shipped-with-refkv): an entry point of another
  distribution may not take one, since its first segment is its own
  distribution's name. A plugin object `Layout` is given
  has no entry point to collide with, so its name is checked against the
  pattern alone (an example's class, given as an object, keeps its name). A
  name is for choosing a plugin, and appears in
  the output only if the plugin writes it.
- **Formats.** `formats` is `"*"`, or a mapping whose keys are file profiles'
  names exactly (`ndpi` is not covered by `tiff`) and whose values are
  iterables of `int`, the versions of that profile's convention the plugin
  reads. Names this refkv's registry does not have as a
  file profile are ignored, so a plugin written for a later refkv loads in an
  earlier one, as long as one name is a profile this refkv has (otherwise
  `LayoutUsageError`). A profile a later refkv adds is read by no plugin until the
  plugin names it. Until refkv 1.0 every built-in convention is at version 0,
  which promises nothing ([conventions §1](../../spec/conventions.md#1-conventions)):
  a refkv release can change a base, and with it a plugin's output, so
  `formats` protects nothing before 1.0, and a plugin's documentation should
  say which refkv releases it was tested with.
- **Options.** They are the plugin's, and so is keeping them free of local
  paths, user or host names and anything else private, if it writes them in
  its output, as archives are published.
- **Determinism.** A plugin's output is a function of its `Base`, its
  canonical options, and the plugin's code (its release, and the versions of
  what it imports, any plugin it calls among them) alone, not of set or dict
  iteration order that `PYTHONHASHSEED` changes; and the plugin fixes the
  exact bytes of every bytes entry it writes. Then two runs with the same
  refkv release, input, source URL, checksums choice, plugin code and options
  write the same output, as refkv's own outputs are compared: entry for entry
  ([zarrify §1.1](../../spec/zarrify.md#11-output-and-equivalence)). refkv
  cannot check this; the plugin promises it. The output need not record how
  it was made; a plugin whose output should say so writes that itself.
- **Readers.** A plugin's output is read as whatever the plugin made it. A
  reader that finds refkv's outputs by the root's `refkv_zarrified` (refkv's
  JavaScript viewer, `refkv.zarr`'s helpers) sees a plugin's output as plain
  Zarr unless the plugin kept refkv's root. A kept `refkv_source` is found at
  that key by its own declaration, and the source rebuilt from its tables, as
  [conventions §8](../../spec/conventions.md#8-the-ir-mirror) says. The
  source table, which is the base's, names the source's URL and its
  siblings' whatever the plugin keeps, so a plugin cannot hide a source's
  location by leaving out refkv's root. A copied array keeps
  its codecs: an OME-Zarr layout of a TIFF in LZW reads with
  `refkv.tiff_lzw`, and one of an HDF5 file with its filters' codecs, as the
  image layout does. A TIFF level whose last strip is short has a
  `rectilinear` chunk grid, which older zarr-python releases open only with
  an experimental flag; a copy keeps its base's grid.
- **HDF5 paths.** An HDF5 object reachable by several links is presented once,
  at its canonical path ([hdf5.md §2.2](../../spec/zarrify/hdf5.md#22-paths)),
  so a plugin's options name canonical paths.

### The specification changes

The implementation adds, as normative text:

- **spec/layout.md**, a short document and not a convention (it has no UUID,
  CMO or schema, since nothing it specifies is declared in an output): what a
  plugin is given (what `Base` gives, in general terms: the output, the IR,
  references of elements, the zero data source), what it returns (`Written`
  and its values), the checks and the
  writing of [What refkv does with it](#what-refkv-does-with-it), the rules
  of `copy`, `array` and `group`, names, `formats` and its versions, options
  (canonical, and copied for each `write`), the three errors and what each
  leaves (no output for any), and determinism. What is Python's alone (the
  `Plugin` protocol, the entry-point group, the CLI's flags) is in
  docs/layout-plugins.md. The routines of
  `refkv.zarrify.coding` add no rule: each is the table or rule of the
  format's section it names, and spec/layout.md lists them by those links.
- **[zarrify §1.7](../../spec/zarrify.md#17-named-inputs-and-layouts)**: a
  zarrifier may be told a plugin's layout, by name and options, in place of
  the built-in layouts, and its output is then the plugin's (spec/layout.md);
  a refused layout request is not a change to what the profile accepts. Its
  sentences that a layout request changes nothing in "what it writes for a
  layout" and that the image layout "is always written" hold for an output
  without a plugin.
- **[zarrify §2](../../spec/zarrify.md#2-the-zarr-layout)**: "A zarrifier MUST
  produce exactly the layout the input format's convention specifies for the
  input" holds for an output without a plugin; a plugin's output has the entries
  spec/layout.md says.
- **[zarrify §1.2](../../spec/zarrify.md#12-input)**: **Structure only** binds a
  plugin, with no exception beyond the checksums [§1.2](../../spec/zarrify.md#12-input) already allows (refkv
  reads a plugin's ranges to checksum them, as it does the base's), and the
  rules on references (**References stay in the file**, the payload limit)
  hold for every reference of its output; a plugin's output that breaks
  them is a failure (`LayoutFailed`), not a rejection of the input.
  The references' budget bounds the base, which a plugin's output is built
  from, and not the plugin's output, which may put one reference at many
  keys and make references of its own: a plugin is trusted code, and the
  budget is there so that a small input cannot make a large output by
  itself. The coding parameters a plugin reads from the IR are structure, as
  they are for the profile.
- **[zarrify §1.9](../../spec/zarrify.md#19-sources-beside-the-input) and
  [§1.2](../../spec/zarrify.md#12-input)**: a plugin's output keeps the base's source table, in the base's
  order, sources it does not use included, not one numbered by first use
  over the plugin's keys; that a url source is one "a reference uses", and
  the numbering of url and data sources by first use, hold for an output
  without a plugin.
- **[zarrify §1.1](../../spec/zarrify.md#11-output-and-equivalence) and
  [§1.2](../../spec/zarrify.md#12-input)**: a reference of a plugin's output may name any range within a data
  source, as [spec/refkv.md §6.3](../../spec/refkv.md#63-resolving-a-reference)
  lets a reader resolve it, where refkv's own outputs name a data source
  whole or a prefix of the zero data source.
- **[zarrify §1.1](../../spec/zarrify.md#11-output-and-equivalence) and
  [Appendix A](../../spec/zarrify.md#appendix-a-conformance)**: a plugin's
  output is compared as every output is, but that a `zarr.json` that is not
  JSON is compared by its bytes; an implementation that cannot load plugins
  (the browser, the Rust CLI) conforms without them, and refuses a plugin's
  name.
- **[conventions §2](../../spec/conventions.md#2-attributes)**: "the root
  always declares its format's convention" holds for refkv's outputs. In a
  plugin's output the root is the plugin's, and a declaration a node of it
  holds (one kept from the base among them) is the plugin's claim, which
  refkv does not check and does not vouch for; `check`'s `refkv` code is how
  a plugin's tests find one its subtree no longer bears out.
- A changelog fragment for each of zarrify.md and conventions.md, in the
  form `spec/tools/changelog.py` checks.

The implementation adds, besides the specifications above:

- **Python.** `refkv/zarrify/layouts.py` becomes the package
  `refkv/zarrify/layouts/`, with `hierarchy.py` and `examples/`;
  `refkv/zarrify/coding.py`; the `layouts` extra; and the examples' entry
  points in refkv's own `pyproject.toml`.
- **Rust.** `rust/refkv-ir/src/routines.rs`, the routines' one entry point
  for the bindings, with the refactorings [Codecs and data
  types](#codecs-and-data-types) names, and a binding of it in
  `rust/refkv-python`; the IR accessors behind `base.reference`,
  `element_at` and `elements` are Python's, over the IR's existing columns,
  members and forms. A Cargo feature of `refkv-ir`, `zarrs`, adds an optional
  dependency on [zarrs_metadata](https://crates.io/crates/zarrs_metadata), the
  Rust counterpart of zarr-metadata: with it, the routines' codecs and the
  projections' array documents parse as its typed Zarr v3 metadata, which the
  core's tests use to check every document refkv writes; without it, nothing
  changes, and the WebAssembly module and the wheels build as they do now.
- **Changelog fragments** for zarrify.md and conventions.md only: the
  fragments of the earlier design were never released, and are deleted with
  what they record, and spec/layout.md is new to every release.
- **`refkv.ir.Value`**, the type of an IR value, exported from `refkv.ir`
  (it is only in `refkv/_core.pyi` today).

The implementation through 0bf3c397 is reworked, and what it added for the
earlier design is taken back, as none of it is needed when refkv writes
nothing into a plugin's output:

- **Specifications.** spec/layout.md's `refkv_layout` convention, replaced
  whole, and `spec/layout/schema.json`; conventions [§1](../../spec/conventions.md#1-conventions), [§2](../../spec/conventions.md#2-attributes), [§9](../../spec/conventions.md#9-dataset-layouts) and [§11.5](../../spec/conventions.md#115-layout-tables) on
  `refkv_layouts`; the reserved name `refkv_layouts` in hdf5.md, npy.md and
  safetensors.md; provenance's `plugins` member and its schema; zarrify
  [§1.7](../../spec/zarrify.md#17-named-inputs-and-layouts)'s root "without `dataset`", its
  equivalence sentence and its link into the old spec/layout.md,
  [§1.2](../../spec/zarrify.md#12-input)'s "not counted", and
  [§1.9](../../spec/zarrify.md#19-sources-beside-the-input)'s keys "not walked"; and the five changelog fragments that record those.
- **Rust.** The reserved name in the HDF5, NumPy and safetensors parsers and
  `rust/refkv-ir/src/out.rs`. `rust/refkv-cli/src/main.rs`'s comment and
  `docs/README.md`'s first hunk are reworded, not reverted: `vzip convert`
  still has no `--layout`, which is Python's.
- **Python.** In layouts.py, `Plugin.path`, `Plugin.convention`,
  `ConventionInfo`, `Layout.path`, `NODE`, `KEY`, `UUID`, `CMO`, the options'
  depth bound, the mandatory document checks (which become `check`) and the
  rule that a reference be equal to one of the base's (which becomes the
  rule of [What refkv does with it](#what-refkv-does-with-it)); `copy` and
  `check` move to `layouts.hierarchy`; in `zarrify/__init__.py`, the
  `plugins=` argument, a list of `Layout`s (now `layout=`, one `Layout`), and
  the usage text; in `__main__.py`, the repeatable `--layout NAME` and
  `--layout-options NAME=JSON` (now each at most once, the second a bare
  JSON object),
  `--layout-path`, which named the old subpath, and `--no-dataset`, which
  only kept the dataset layout out of a base that a plugin now owns the whole
  of (`vzip convert` has neither).
- **Docs and tests.** docs/layout-plugins.md is rewritten for the root the
  plugin owns, and docs/examples/ome_zarr_06.py moves into the package as an
  [example](#examples-shipped-with-refkv); `docs/README.md`'s "beside
  refkv's" goes; python/tests/test_layouts.py is rewritten: its tests of
  several plugins, paths, CMOs and mandatory checks become tests of the two
  checks, the writing, `Base`, `copy` and `check`.

### The test case: OME-Zarr 0.6 from TIFF and HDF5

For a TIFF (and an NDPI, QPTIFF or IMS file), the base's root is OME-Zarr 0.5:
it holds the `multiscales`, and its levels are arrays `0`, `1`, .... The plugin
writes an OME-Zarr 0.6 image at the output's root: it copies each level to the
same path and writes 0.6 metadata over the copies, keeping each level's scale,
the image's name and its `omero` channels, and, unless its option `source` is
`false`, keeps `refkv_source`, which an OME-Zarr reader skips as a child group
it does not know. refkv's translation places a level's upper-left corner in y
and x (and z, for IMS; an NDPI file's z is a plane's own position), where 0.6
places a pixel's center, so each level moves by half its own pixel on those
axes. The method below is the plugin class's; `dump` comes from
`refkv.zarrify.layouts` and `copy` from `refkv.zarrify.layouts.hierarchy`, and `options()` fills in `"source": True` when the
caller leaves it out:

```python
def write(self, base: Base, options: Mapping[str, object]) -> dict[str, bytes | Range | Concat]:
    out: dict[str, bytes | Range | Concat] = base.entries("refkv_source/") if options["source"] else {}
    if base.profile == "hdf5":           # an HDF5 base mirrors the file's own groups
        return out | self.write_hdf5(base, options)
    ome = base.document("zarr.json")["attributes"]["ome"]
    ms = ome["multiscales"][0]
    cornered = {"y", "x", *({"z"} if base.profile == "ims" else ())}
    space = [a.get("name") in cornered for a in ms["axes"]]
    datasets = []
    for level in ms["datasets"]:
        p = level["path"]
        out |= {f"{p}/{k}": v for k, v in copy(base, p).items()}
        steps = level["coordinateTransformations"]
        scale = next(t["scale"] for t in steps if t["type"] == "scale")
        corner = next((t["translation"] for t in steps if t["type"] == "translation"), [0.0] * len(scale))
        center = [c + s / 2 if is_space else c for c, s, is_space in zip(corner, scale, space, strict=True)]
        datasets.append({"path": p, "coordinateTransformations": [{
            "type": "sequence", "input": {"path": p}, "output": {"name": "physical"},
            "transformations": [{"type": "scale", "scale": scale},
                                {"type": "translation", "translation": center}]}]})
    image = {"name": ms.get("name", "image"),
             "coordinateSystems": [{"name": "physical", "axes": ms["axes"]}], "datasets": datasets}
    out_ome = {"version": "0.6", "multiscales": [image], **({"omero": ome["omero"]} if "omero" in ome else {})}
    out["zarr.json"] = dump({"zarr_format": 3, "node_type": "group", "attributes": {"ome": out_ome}})
    return out
```

A viewer is given the archive's URL, as for refkv's own output. This targets
the 0.6 `image` schema; the strict schema's `type` and `metadata` members are
the plugin's to add.

For an HDF5 file, the base mirrors the file's groups and datasets as Zarr
groups and arrays. Which datasets are images, and their axes and scales, are
the file's conventions (NWB's, BigDataViewer's), not HDF5's, so they come from
the options, one image group each:

```json
{"images": [{"name": "movie", "array": "stimulus/templates/movie/data",
             "dims": ["t", "x", "y"], "order": ["t", "y", "x"],
             "axes": [{"name": "t", "type": "time", "unit": "second"},
                      {"name": "y", "type": "space", "unit": "micrometer"},
                      {"name": "x", "type": "space", "unit": "micrometer"}],
             "scale": [0.033, 0.5, 0.5]}]}
```

NWB writes an `ImageSeries` as `(frame, x, y)`, so the plugin copies the array
with `copy(base, array, order=[0, 2, 1], attributes=base.attributes(array))`
(the positions in `dims` of the names in `order`; the attributes keep NWB's
`unit`, `conversion` and `offset`) into time, `y`, `x` order, at `<name>/0`,
writes the 0.6 group `<name>/zarr.json` over it, and a root group
`zarr.json`. `images` is optional, since `options()` runs before the input
is opened and cannot know it is HDF5: an HDF5 base without `images` is
`Rejected` from `write`. A `name` of `refkv_source` while the mirror is kept
is a `ValueError` from `options()`, and so are two images of one name, a
name that `node_name` refuses (one that holds `/` among them), and an HDF5
array whose data type is not a number's; an option that names no array of
the base (the plugin turns `base.array`'s `KeyError` into it), or an array
whose rank does not match its dimensions, is `Rejected`. Neither case needs the facts, a codec table, or a read of the
source. refkv ships this plugin in full, with its option checks, as the
example `refkv.example.ome-zarr-06` ([below](#examples-shipped-with-refkv)).
A TIFF's other IFDs are a second example's, which builds them from the IR.

### Examples shipped with refkv

refkv ships example plugins as part of the package, in
`refkv.zarrify.layouts.examples`, one module each, registered under refkv's
own names (`refkv.example.<name>`), so that `python -m refkv.zarrify --layout
refkv.example.ome-zarr-06 image.ome.tif out.vzip` works once the `layouts`
extra is installed. Each module is self-contained: it imports only the
standard library and refkv's public API (`refkv`, `refkv.ir`,
`refkv.zarrify`, `refkv.zarrify.layouts`, `refkv.zarrify.layouts.hierarchy`,
`refkv.zarrify.coding`), which a test checks by reading the module's imports,
so that a user can copy one into a package of their own, rename it, and change
it. Each has tests that run it over the fixtures of its formats, call `check`
(allowing only the codes the module's docstring names), open the output with
zarr, and compare every array it writes with the same values read another way
(the base's array, or tifffile and h5py, which the tests already use).

They are examples, not refkv's layouts: their output declares nothing of
refkv's unless they keep the base's nodes, and refkv's conventions do not
specify them. The first set is written by agents against the implementation,
as its first users, each covering what the others do not:

| example | formats | what it exercises |
|---|---|---|
| `ome-zarr-06` | TIFF, NDPI, QPTIFF, IMS, HDF5 | [the test case](#the-test-case-ome-zarr-06-from-tiff-and-hdf5): `copy`, transposes, attributes, options, keeping the mirror |
| `tiff-pages` | TIFF | every main-chain IFD of a TIFF, with its SubIFDs as its pyramid, as an image of its own, built from the IR with `tiff_tags`, `tiff_data_type`, `tiff_codecs` (EER's for its chunk shape), `tiff_byte_order`, `base.members`, `base.reference`, `array` and `group`, without `copy`; an IFD whose tile tables the IR does not hold is `Rejected`, naming it |
| `hdf5-rechunk` | HDF5 | contiguous datasets re-tiled into chunks of a shape the options give, from `base.elements`, `base.reference`, `cut`, `concat(merge=True)`, `base.zeros` and `base.fits`, with the codecs of the base's array; `check`'s `size` |
| `xarray-v2` | HDF5 | an HDF5 or NetCDF-4 file as Zarr v2 with consolidated metadata and `_ARRAY_DIMENSIONS`, for xarray: a hierarchy that is not Zarr v3, with only `.zmetadata` and the root's documents read at open (`Written.read_at_open`; `Written.stored` has no use here, since HDF5 gives no bytes chunks), and its own translation of the base's Zarr v3 codecs into v2's `compressor` and `filters`, rejecting an array whose codecs have none |
| `bare` | every file profile (`"*"`) | refkv's image layout with none of refkv's declarations and no mirror: the smallest plugin |

What the agents find hard to do with the API is changed in the API before it
is merged, and the PR records each change and the report behind it.

## Not in this proposal

- **Reading the source**: a plugin reads no chunk, not even a small metadata
  array (an NWB `ImagingPlane`'s `grid_spacing`, a pyramid's `resolutions`),
  whose values its options give today. A read during `write` would have to
  go through the run's own transport, pins and reader policy, and be bounded
  by a budget of its own, which nothing in this design needs yet. (The
  checksums of a plugin's ranges are read after `write`, through the
  callback the base's ranges are checksummed through today, after the run
  has ended; a plugin's ranges of a sibling are read as the base's are.)
- **Parser plugins**, for formats no built-in parser reads. Each would need a
  place in the signature order of [zarrify §1.2](../../spec/zarrify.md#12-input)
  (on `H` or `L`, before or after the gzip retry, and what a tie between two
  plugins is: a failure, since it is a property of the installation and not
  of the input), the reader policy, pins and sibling rules over its own reads,
  and a mirror with no built-in profile to declare it.
- **Reading a file without the files it names**, such as one file of an
  OME-TIFF set: a new mode in each parser that stops at its siblings (TIFF,
  CZI, HDF5, LIF, NIfTI pairs), specified in each format's document.
- **An OME-TIFF's other series.** The TIFF profile reads an OME-XML's first
  `Image` only (`read_ome` in `rust/refkv-ir/src/tiff.rs`), and its IR holds
  the tile tables of an IFD the base does not present only when the parser
  read them ([tiff.md §5.1](../../spec/zarrify/tiff.md#51-elements) lists an IFD's `tiles` whenever it has both
  tables, which the parser does not do for large tables outside the image it
  presents). A plugin presenting every series needs the parser to read
  every `Image`'s planes and every IFD's tables, deterministically, as a
  change to the TIFF profile and its specification; `tiff-pages` presents
  what the IR holds and rejects the rest.
- **Layouts on store inputs**, whose output is joined in more than one place
  (the DICOM store form among them).
- **Reshaping in `copy`**: options to add or drop size-1 axes, or to keep a
  region of an array's chunks. A plugin can build such arrays by hand today;
  `copy` options would let `check` vouch for them too.
- **Provenance for a plugin's output.** refkv writes no provenance today; when
  it does, it would be on the root, which is the plugin's, so a plugin's
  output needs a place for it the plugin can choose.
- **Rust plugins.** An installed wheel cannot load a profile written in Rust.

## Alternatives considered

- **The IR alone, without the base** (this note's first design). See [Built
  from the base and its IR](#built-from-the-base-and-its-ir).
- **Specifying the facts per format.** It would make them a second,
  unversioned description of each format beside its convention. The base
  output is that description already.
- **The plugin's layout beside refkv's, under `refkv_layouts/`** (this note's
  earlier design). refkv's image layout stayed at the root, so a viewer had to
  be given the layout's subpath, every reference was stored twice, and refkv
  wrote a node, a group and attributes into an output the plugin was meant to
  shape. A plugin that wants its layout beside refkv's whole output can still
  write that, from `base.entries()`; `check` notes that its node is inside
  the subtree refkv's root declares.
- **A convention of refkv's on the plugin's node** (`refkv_layout`, naming the
  plugin, its options and its base). It made refkv the author of attributes
  in the plugin's hierarchy, and a plugin's schema had to allow them. A plugin
  that wants its output to say how it was made declares its own convention.
- **Mandatory checks of the plugin's documents** (strict JSON, Zarr v3's
  members, parents that are groups, references only where `copy` puts them).
  They caught real mistakes in review, but every one is a mistake in the
  plugin's own output, which no other part of the archive depends on, and
  each would have to grow with every form a plugin is free to write (Zarr v2,
  arrays reshaped by hand). They are `check`'s.
- **Refusing documents that change refkv's declarations.** It would keep refkv
  from writing a claim of its conventions it did not make, but it is not about
  which bytes the output reads, and it cannot be made whole: a declaration can
  sit in a v2 `.zattrs`, in consolidated metadata, or in a document that one
  JSON parser reads one way and another another, and an exact match forbids
  what [conventions §2](../../spec/conventions.md#2-attributes) allows, other
  conventions' members beside refkv's. Keeping a declaration while changing
  what it declares is a mistake in the plugin's output like any other, and
  `check` reports it under `refkv`.
- **Several plugins per call.** Each wrote under its own path, which only works
  when refkv owns the root. One plugin owns the root now, and composes others
  itself.
- **Only the base's references** (this note's second design). A plugin could
  present the base's chunks differently, and nothing else: no re-tiled HDF5
  slab, no TIFF IFD the base leaves out, no crop. Checking a range
  against its source's size and the payload limit, and computing its
  checksum, is what refkv does for its own references already;
  `base.reference` keeps a plugin from having to know which source a store
  IR's offset is in.
- **VirtualiZarr parsers.** They produce an xarray dataset, not an arbitrary
  Zarr hierarchy, and keep no record of the source's other bytes.

## Open questions

0. **Where an image layout puts a pixel.** A plugin that writes OME-Zarr 0.6
   moves each level by half its pixel on the axes whose translation refkv's
   layouts give at a pixel's corner (`y` and `x`, and `z` for IMS but not for
   NDPI, whose `z` is a plane's own position). That is knowledge of each
   format's convention a plugin hard-codes today, and a wrong guess passes
   every check. The conventions could state, per axis, what a translation
   places; until they do, the shipped example names its rule.

1. **Large inputs.** Copying an array builds a Python mapping of its chunks,
   which for a file of millions of chunks is slow, and so is checking that
   each value is one of the base's; a lazier form can come later without
   changing the interface.
2. **Sources nothing references.** The output keeps the base's source table
   whole, so a source only the left-out entries read (a sibling file of a
   layout the plugin dropped) is still listed. Pruning and renumbering the
   table is possible, at the cost of a rule for the new numbering.
3. **What the TIFF IR keeps of IFDs the base does not present.** If which
   tile tables the parser reads depended on how the planner joined reads
   (`connections`, `byte_cost`), so would `base.ir`, and with it a plugin's
   output. The implementation's tests compare the IR of each TIFF fixture
   under different planner settings; a difference is a parser bug, fixed
   there, not something a plugin works around.
