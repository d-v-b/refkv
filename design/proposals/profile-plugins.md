# Proposal: layouts defined outside refkv

Every profile refkv has is compiled into its Rust core, and so is every layout
those profiles write. Someone who wants another presentation of a format refkv
already reads, such as an OME-Zarr 0.6 image of a TIFF or of an HDF5 dataset,
cannot add one without forking the repository and building their own core,
wheel and WebAssembly module. This note proposes **layout plugins**: Python
packages, found the way zarr finds codecs, that add a layout of their own to
the hierarchy a built-in profile writes, built from what that profile read: its
output and its IR. It is a design note. d-v-b/refkv#279 implements it, after
nine rounds of adversarial review, and its rules are normative there in
spec/layout.md and the amendments
[The specification changes](#the-specification-changes) lists; where this
note and spec/layout.md differ, spec/layout.md is the rule. That PR's guide,
docs/layout-plugins.md, walks through the OME-Zarr 0.6 example in full
(docs/examples/ome_zarr_06.py).

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
`ReferenceStore.from_virtualizarr(vds)`) works outside refkv, but the result is
not a zarrified input: no convention, no IR mirror, no pins, and no reader
policy over what it reads.

## Why a layout, not a projection of the raw IR

The first version of this note proposed plugins that turn a built-in parser's
IR into a hierarchy of their own. Reviews, and experiments run for them,
showed why that does not work:

- **The IR does not hold what a projection needs, for every format.** The IR
  ([conventions §8](../../spec/conventions.md#8-the-ir-mirror)) partitions the
  source's bytes into typed elements, but many leaves are opaque. In the HDF5
  IR, every object header message (dataspace, datatype, layout, filters, links)
  is a `bytes[n]` leaf with no value, and objects are named `objects/0`,
  `objects/1`, ...; the dataset names, shapes, data types, filter parameters
  and attributes are in the parser's **facts**, a JSON object that is
  specified nowhere. ND2 has opaque leaves too. For TIFF the IR is enough for an
  uncompressed image (a sketch of about forty lines reproduced the built-in
  array on two fixtures), but a compressed one needs the codec chain of
  [tiff.md §4.3](../../spec/zarrify/tiff.md#43-data-types-and-codecs), which a
  plugin would have to reproduce, and could get silently wrong.
- **The mirror depends on the profile.** The mirror's table rules, its view,
  and the members a store keeps beside it switch on the profile's name
  (`rust/refkv-ir/src/mirror.rs`, `project/store.rs`), and readers take that
  name from `refkv_source`'s declaration. A mirror cannot be declared under a
  plugin's convention; `_core.mirror(ir, "acme.x")` panics today.
- **A plugin that owns its sources, reads the source, reads siblings, or joins
  sniffing** has to get the reader policy, the pins, the sibling rules
  ([zarrify §1.9](../../spec/zarrify.md#19-sources-beside-the-input), whose name
  checks live in Rust) and the signature order
  ([§1.2](../../spec/zarrify.md#12-input)) right by itself.

The built-in profile's output already holds the decoded metadata, specified by
the format's convention: its arrays have their shapes, data types, codecs
(TIFF's predictors and JPEG tables, HDF5's filters) and chunk references, and
its nodes carry the source's metadata. The dataset layouts of
[conventions §9](../../spec/conventions.md#9-dataset-layouts) already show how
a second presentation of the same bytes sits beside the image, its arrays
referencing the same bytes, range for range. A plugin layout is that, written
by someone else. Reviewers ran this note's TIFF example over 177 fixtures the
built-in profiles accept (124 TIFF, 13 NDPI, 8 QPTIFF, 32 IMS): every node's
attributes passed the OME-Zarr 0.6 `image` schema, and every copied array,
transposed copies of HDF5 arrays included, read back equal to the base's.

## The proposal

### What a plugin is

A package registers a layout under the entry-point group `refkv.layouts`, its
key the layout's name:

```toml
[project.entry-points."refkv.layouts"]
"acme-ome.ome06" = "acme_ome:Ome06"
```

```python
import json
from collections.abc import Mapping

from refkv import Concat, Range
from refkv.zarrify import Rejected
from refkv.zarrify.layouts import Base, ConventionInfo, copy  # new

class Ome06:
    name = "acme-ome.ome06"                          # the entry point's key
    path = "ome06"                                   # its node by default, under refkv_layouts/
    formats = {p: {0} for p in ("tiff", "ndpi", "qptiff", "ims", "hdf5")}  # base profiles, convention versions
    convention = ConventionInfo(
        uuid="5f0c2a3e-8d1b-4c7e-9a64-2b1f0e9d7c55",
        version=0,
        schema_url="https://raw.githubusercontent.com/acme/acme-ome/refs/tags/v0/schema.json",
        spec_url="https://github.com/acme/acme-ome/blob/v0/spec.md",
        description="An OME-Zarr 0.6 image of a source zarrified by refkv",
    )

    def options(self, given: Mapping[str, object]) -> dict[str, object]:
        """The options in canonical form (defaults filled in); raise ValueError if invalid."""

    def write(self, base: Base, options: Mapping[str, object]) -> Mapping[str, bytes | Range | Concat]:
        """The layout's entries, keys relative to its node; raise Rejected for an input it cannot present."""
```

`refkv.zarrify.layouts` is new, and holds `Base`, `ConventionInfo`, `Layout`,
`copy` and the error classes below. A layout's **key**, the name of its
convention metadata object (CMO), is its name with `.` and `-` as `_`:
`acme_ome_ome06`. refkv writes the CMO from the `ConventionInfo` with the five members the
framework allows: `uuid`, `schema_url`, `spec_url`, `name` (the key) and
`description`. The version is not one of them. refkv writes it under the
plugin's key on the layout node, `{"version": 0, ...}` with any members the
plugin wrote there, so that a reader that knows only the plugin's convention
finds it, and in the layout property's `layout.version` (below); `schema_url`
SHOULD differ from one version to the next, as the framework advises. A
plugin that writes its key writes an object without `version`. Two names that
map to one key are refused together, though only the UUID identifies a layout,
and a convention whose UUID is a refkv convention's is refused when the plugin
is loaded.

### What it is given: `Base`

`Base` is a read-only view of the built-in profile's output, with the image
layout and `refkv_source` and nothing that depends on the rest of the call:
no dataset layout, and a root without `layouts` or provenance. Two calls that
differ only in the built-in layouts they ask for give a plugin the same `Base`.

- `base.profile`, `base.version` and `base.uuid`: the profile that read the
  input, as the root's `refkv_zarrified` names it in `profile` (a TIFF the
  parser found to be NDPI is `ndpi`), and its convention's version and UUID.
- `base.ir`: the IR, as a public `refkv.ir.Ir`, with the accessors of
  `refkv._core.Ir` today (each element's path, kind, type, extent, value and
  children) and `at(path)`, an element's index by its path. For a file read as
  a store it is the multi-source IR of
  [conventions §8.9](../../spec/conventions.md#89-stores) (the store run
  returns it today, and `zarrify` drops it).
- `base.keys()`, and `base.document(key)`, a JSON document (a `zarr.json`) as
  a dict: the attributes as the base wrote them. An HDF5 attribute that JSON
  cannot hold is written as [conventions §6](../../spec/conventions.md#6-source-metadata-as-json) says (`"NaN"`, a hex float, an
  integer beyond 2^53 as its digits); `refkv.zarr` decodes HDF5's floats, and
  a plugin that copies attributes decodes what it needs.
- `base.array(path)`: an array's document, as a dict, and its chunks, a
  mapping from chunk key (relative to the array) to value, each a `Range`,
  `Concat` or bytes of the base.

`Base` has no way to read a chunk, and **Structure only**
([zarrify §1.2](../../spec/zarrify.md#12-input)) binds a plugin with no
exception. An earlier draft gave plugins `base.values`, a bounded read of
small metadata arrays (an NWB `ImagingPlane`'s `grid_spacing`); review dropped
it, and the options give such values instead
([Not in this proposal](#not-in-this-proposal)).

The facts are not in `Base`. They are the built-in projection's private input
and change whenever it does.

### What it returns

`write` returns the layout's entries, keys relative to its node (`zarr.json`,
`s0/zarr.json`, `s0/c/0/0/0`, ...): documents and bytes entries the plugin
makes, and reference values copied from the base.
`copy(base, path, order=None, attributes=None, dimension_names=None)` does the
copying, by rules spec/layout.md states, so that two producers copy alike: it
returns an array's entries, keys relative to the array, its `zarr.json` among
them. Its document is the
base's with `attributes` as given (`{}` by default: the base's attributes are
written in [conventions §6](../../spec/conventions.md#6-source-metadata-as-json)'s encodings, which only the base's declarations
decode, so a plugin that keeps some decodes them first) and
`dimension_names` as given (else the base's). Given `order`, a permutation of
the array's axes as integers, as `numpy.transpose` takes it (the copy's axis
`i` is the base's axis `order[i]`: `[0, 3, 1, 2]` makes `(t, y, x, c)`
`(t, c, y, x)`), the array is transposed: the shape, the chunk
grid (each axis's list, for a `rectilinear` grid), `dimension_names` and every
chunk key are permuted (in the array's chunk key encoding, `default` or `v2`,
with its separator), and a `transpose` codec of the inverse order `q` goes
first, composed with a `transpose` of order `t` the base already starts with
into one of order `[q[t[i]] for each i]`, and left out when that is the
identity. A sharded
array keeps `sharding_indexed` first, with its shard shape permuted, its inner
`chunk_shape` permuted, `transpose(inverse)` first in its inner codecs and
`transpose(inverse + [n])` first in its index codecs, `n` the rank. A
channel-last HDF5 image then becomes an OME-Zarr image in time, channel, space
order and decodes to the same values. A plugin that writes an array's document
itself owns its correctness: refkv checks the references, not whether the
documents decode them as the plugin meant.

`write` is a plain function. refkv runs it, as it runs a plugin's loading and
`options()`, on a thread of its own, one plugin at a time, so it does not hold
up the caller's event loop and may call refkv's blocking API, `zarrify`
included, without exhausting a shared pool. Cancelling `zarrify_async` does
not stop a `write` already running. Whatever a plugin raises, `SystemExit` and
`KeyboardInterrupt` included, ends the call with one of the errors below.

### What refkv does with it

1. **Checks the entries.** A failed check is the plugin's error,
   `LayoutFailed`.
   - Every key is a valid key ([spec/refkv.md §2](../../spec/refkv.md#2-data-model)),
     relative, valid UTF-8, with no empty, `.` or `..` segment, no segment
     that starts with `__`, which Zarr v3 reserves, and `zarr.json` only last;
     with its prefix it is at most 65535 bytes, as a ZIP archive's keys are.
   - Every document is strict JSON: no byte order mark, `NaN`, infinity, member
     twice in one object or lone surrogate, no integer of more than 4300
     digits, and nesting at most 256 deep. Its `zarr_format` is the integer 3
     and its `node_type` `group` or `array`; an array's holds the members Zarr
     v3 requires, and `attributes`, where present, is an object.
   - The node's `zarr.json` is a group. Every other key is a node's
     `zarr.json`, whose parent node's `zarr.json` is a group of the layout, or
     a chunk below an array's `zarr.json`; no node is inside an array.
   - Every reference value is **equal to a reference value of `Base`**,
     ranges compared as [zarrify §1.1](../../spec/zarrify.md#11-output-and-equivalence)
     compares them (a checksum is no part of it), and written as the base
     holds it, checksums included. A plugin reuses the base's references and makes none, so its layout reads only
     bytes the base reads: under the same pins and reader policy, within the
     file and the payload limit
     ([zarrify §1.2](../../spec/zarrify.md#12-input), **References stay in the
     file**), with the base's checksums when the call asked for them. Its
     references are not counted against
     [the references' budget](../../spec/zarrify.md#12-input), as a dataset
     layout's copies are not.
   - No document holds `consolidated_metadata`, the attribute
     `refkv_zarrified`, `refkv_layout` or `refkv_provenance`, or a CMO with the
     UUID or the name of a refkv convention (a profile's, a dataset layout's,
     provenance's, `refkv_layout`'s), its UUID compared with case, hyphens,
     braces and a `urn:uuid:` prefix aside. The plugin's key, as an object without `version`, and its
     CMO may be on any node of the layout, the CMO equal to the one refkv makes
     from its `ConventionInfo`; refkv writes both on the layout node, once, the
     key with `version` added.
2. **Places the node** at `refkv_layouts/<path>`, `path` the plugin's or the
   caller's, below the group `refkv_layouts/zarr.json`, which refkv writes
   declaring `refkv_layout` (its CMO, and `{"refkv_layout": {"version": 0}}`):
   a reader knows the group by that CMO, not by its name. No base of a
   file input holds anything there: the bases that present the source's own
   names at the root (HDF5, its external-storage form, NumPy `.npz`,
   safetensors files) leave out a source object named `refkv_layouts`, with
   the reason `reserved`, as they leave out one named `refkv_source`. Store
   inputs take no plugin layouts, so their bases are unchanged.
3. **Declares the layout.** The layout node's `zarr_conventions` is the
   `refkv_layout` CMO, then the plugin's, then any the plugin wrote; its
   attributes hold, under `refkv_layout`, the **layout property**:

   ```json
   {"version": 0,
    "layout": {"name": "acme-ome.ome06", "uuid": "5f0c2a3e-...", "version": 0},
    "source": {"url": "<U>"}, "base": {"uuid": "<the base convention's UUID>", "version": 0},
    "options": {}}
   ```

   `version` is `refkv_layout`'s own, and `layout` names the plugin's
   convention. `refkv_layout` is a convention of refkv's own, with its own UUID,
   schema and document (`spec/layout.md`), versioned and tagged as refkv's
   conventions are, so the property's shape is refkv's to version, and the
   plugin's schema need not describe it. The layout's other nodes carry what
   the plugin writes, and no refkv declaration. `options` are the
   canonical ones `options()` returned, which must survive a JSON round trip
   (no `NaN`, no tuples); that they hold no local path or anything else
   private, as archives are published, is the plugin's responsibility.
4. **Records it on the root.** The root's `layouts` (after `source`, its names
   in code point order) names it, `"acme-ome.ome06": "refkv_layouts/ome06"`, beside
   `"dataset"` when the dataset layout is written. A reader identifies a node
   that `layouts` names by its CMOs, never by the name. The rewritten root is
   equal, as a JSON value, to the built-in root with that member set; refkv's
   `info["layouts"]` lists the names the same way.
5. **Joins it to the base.** The output is the built-in output the call asked
   for, unchanged but for the root's `layouts`, with `refkv_layouts/zarr.json`
   and the layout's entries added, after the base's sources are numbered and
   its checksums recorded: the layout copies the base's references, checksums
   included, and the numbering of url sources by first use
   ([zarrify §1.9](../../spec/zarrify.md#19-sources-beside-the-input)) counts
   no key under `refkv_layouts`. Its entries are in the order outputs have
   today, reference entries by key and then bytes entries by key, across the
   whole output. It keeps the base's source table, in the base's order, its
   `compressed`, `page_size`, `url`, reader policy and concurrency, and its
   `read_at_open` with the layout's documents and `refkv_layouts/zarr.json`
   added; the layout's other bytes entries are not compressed, and the bounds
   are derived again. The checks and the join run on a thread, as the
   projection does today.

A reader of a base convention skips `refkv_layouts`, as it skips a dataset
layout: no source metadata is declared there.

The checks guard against a plugin's mistakes, not against a hostile plugin: a
plugin is Python code running in the caller's process, and can do anything the
caller can.

### Choosing a plugin

A layout is chosen by name, never by sniffing, through a new argument,
`plugins`, beside `layouts`, which keeps its meaning (the built-in layouts;
`None`, every one the format has):

```python
from refkv.zarrify import zarrify
from refkv.zarrify.layouts import Layout

zarrify("image.ome.tif", plugins=[Layout("acme-ome.ome06")])
zarrify("ophys.nwb", plugins=[Layout("acme-ome.ome06", options={"images": [...]})])
```

`Layout(name, options=None, path=None)` names a plugin, its options and its
path (else the plugin's). `python -m refkv.zarrify` gains `--layout NAME`,
`--layout-options NAME=JSON`, `--layout-path NAME=PATH` and `--no-dataset`; the
image layout is always written, and the dataset layout unless `--no-dataset`
is given. A plugin is loaded, and its options checked, when it is named,
before the input is opened; `options()` cannot know the input's profile, so a
plugin whose options matter for one format only makes them optional, and
rejects an input that needs them from `write`.

| what goes wrong | error | CLI exit |
|---|---|---|
| no entry point of the name, installed entry points that cannot be read, a plugin that fails to load or whose attributes break the rules below (its `name` other than the entry point's key, say), options the plugin refuses, two layouts of one name, path or key, a layout on a store input | `LayoutUsageError` (a `ValueError`) | 2 |
| a base profile or version the plugin does not read, a `Rejected` from `write` | `LayoutRejected` (a `Rejected`) | 3 |
| a failed check, or any other exception from `write`, `SystemExit` included (its cause chained) | `LayoutFailed` | 1 |

A layout on a store input (a directory, or a URL ending in `/`) is a usage
error for now; a file read as a store (VSI, NRRD, GRIB, an HDF5 file with
external storage) is a file input and takes layouts. Nothing a plugin does can
change which profile reads a file, what the profile accepts, or what the
built-in layouts hold. A `LayoutRejected` refuses the layout request, which
depends on the input (a sniffed file's profile is not known in advance), so it
is a rejection, and like every rejection writes no output; asking for
`Dataset()` of a format that has none is, in Python, a `ValueError` today,
which [zarrify §1.7](../../spec/zarrify.md#17-named-inputs-and-layouts) calls rejecting the request too.

### Names, identity, versions and equivalence

- **Names.** `<distribution>.<layout>`: segments of `[a-z0-9-]+` joined by
  `.`, the first the normalized name ([PEP 503](https://peps.python.org/pep-0503/))
  of the distribution that provides the entry point, which refkv checks. Names
  without a dot, and those whose first segment starts with `refkv`, are
  refkv's. A path is one key segment of `[a-z0-9_-]+`, not starting with
  `__`.
- **Formats.** A key of `formats` is a file profile's name exactly (`ndpi` is
  not covered by `tiff`), and its value the versions of that profile's
  convention the plugin reads. Names this refkv's registry does not have as a
  file profile are ignored, so a plugin written for a later refkv loads in an
  earlier one. A profile a later refkv adds is read by no plugin until the
  plugin names it.
- **Identity.** A layout is its convention's UUID, as the framework requires;
  names and keys are for people. A reader that finds a key whose UUID it does
  not know ignores it.
- **Versions.** A plugin's convention states what its version numbers mean,
  as the framework asks, and its version increases whenever its layout changes
  for some input. `formats` lists the base convention versions each plugin
  version reads. Until refkv 1.0, though, every built-in convention is at
  version 0, which promises nothing
  ([conventions §1](../../spec/conventions.md#1-conventions)): a refkv release can
  change a base's output, and with it a plugin's layout, with no version to
  tell them apart. `formats` protects nothing before 1.0, and a plugin's
  documentation should say which refkv releases it was tested with. No refkv
  output records the plugin's release today; provenance's `plugins` member
  (below) will, once refkv writes provenance.
- **Options.** A plugin's specification MUST keep its options free of local
  paths, user or host names and anything else private, as provenance's rules
  do: they are written in the output, and archives are published. It also
  fixes the exact bytes of every bytes entry its layout writes, compressed
  ones included, since outputs are compared byte for byte.
- **Determinism.** A layout is a function of `Base` and its canonical options
  alone: two runs over the same input, with the same options, write the same
  layout. refkv cannot check this; the plugin's specification promises it.
- **Equivalence.** Two outputs of one input are equivalent
  ([zarrify §1.1](../../spec/zarrify.md#11-output-and-equivalence)) only if they
  hold the same layouts (the same built-in layouts, and plugin layouts of the
  same name, path, UUID, version and canonical options; a plugin's convention
  document specifies its options and their canonical form, so that another
  implementation of it can reproduce them), and then entry for entry, as
  every output is compared, a layout's subtree included. Whether a layout's
  subtree is what its convention specifies is that convention's to say.
- **Readers.** A copied array keeps its codecs: an OME-Zarr layout of a TIFF
  in LZW reads with `refkv.tiff_lzw`, and one of an HDF5 file with its filters'
  codecs, as the image layout does. A TIFF level whose last strip is short has
  a `rectilinear` chunk grid, which older zarr-python releases open only with
  an experimental flag; a plugin cannot change the grid of a reference it
  copies. A plugin layout is a subtree: a reader opens the path the root's
  `layouts` names, `refkv_layouts/ome06`, not the root, and a viewer is given
  that subpath's URL.
- **HDF5 paths.** An HDF5 object reachable by several links is presented once,
  at its canonical path ([hdf5.md](../../spec/zarrify/hdf5.md)), so a plugin's
  options name canonical paths.

### The specification changes

The implementation adds, as normative text:

- **spec/layout.md**, the `refkv_layout` convention: its CMO, UUID and tag
  scheme, the layout property, where a plugin layout's node is, the content of
  `refkv_layouts/zarr.json`, the rules of `copy`, and its own table of
  entries, which every format's table points to for `refkv_layouts/**`.
- **[zarrify §1.7](../../spec/zarrify.md#17-named-inputs-and-layouts)**: a
  zarrifier may be told a plugin's layout, by name and options, and the rules
  of [What refkv does with it](#what-refkv-does-with-it); a refused layout
  request is not a change to what the profile accepts. The sentence saying the
  root has "no `layouts` member" without the dataset layout becomes "no
  `dataset` member".
- **[zarrify §1.2](../../spec/zarrify.md#12-input)**: **Structure only** binds a
  plugin layout, with no exception; references under `refkv_layouts/` are not
  counted against the budget.
- **[zarrify §1.1](../../spec/zarrify.md#11-output-and-equivalence) and
  [Appendix A](../../spec/zarrify.md#appendix-a-conformance)**: equivalence as
  above; an implementation that cannot load plugins (the browser, the Rust
  CLI) conforms without them, and refuses a plugin layout's name.
- **[conventions §1](../../spec/conventions.md#1-conventions)**: not every
  convention nests under `refkv_zarrified` (`refkv_layout`, and a plugin's,
  do not), and the table of UUIDs gains `refkv_layout`'s.
- **[conventions §2](../../spec/conventions.md#2-attributes)**: the root's
  `layouts` may name a plugin layout, whose node a reader identifies by its
  CMOs, not by the name or a `refkv_zarrified` it does not have; the root's
  child `refkv_layouts` is refkv's for a file input, and a reader of a base
  convention skips it when its `zarr.json` declares `refkv_layout` (a store's
  source group of that name is the source's); the rule that outside
  `refkv_zarrified` a node holds only members its target formats define does
  not apply at or below `refkv_layouts`, which `refkv_layout` and each
  layout's conventions govern.
- **[conventions §9](../../spec/conventions.md#9-dataset-layouts) and
  [§11.5](../../spec/conventions.md#115-layout-tables)**: a plugin layout is a
  second layout like a dataset layout, but below `refkv_layouts` and under its
  own conventions; [§11.5](../../spec/conventions.md#115-layout-tables) says, once for every format, that `refkv_layouts/**`
  is the `subtree` of spec/layout.md's table, so no format's own table
  changes.
- **[zarrify §1.9](../../spec/zarrify.md#19-sources-beside-the-input)**: the
  numbering of url sources by first use counts no key under `refkv_layouts`.
- **hdf5.md, npy.md, safetensors.md**: `refkv_layouts` joins `refkv_source`
  among the reserved names a root object may not have, with a changes entry in
  each, since it changes the base output for such a file whether or not a
  plugin is used.
- **[provenance](../../spec/provenance.md)**: an optional `plugins` member of
  `Provenance` (a plugin is not the program that wrote the root, which
  `Producer` names), each plugin layout's name, distribution and distribution
  version, written, like the rest of provenance, only when asked for.
- `python/tests/test_zarrify_conventions.py`, which treats every node `layouts`
  names as declaring `refkv_zarrified`, tells a plugin layout by its
  `refkv_layout` CMO.

### The test case: OME-Zarr 0.6 from TIFF and HDF5

For a TIFF (and an NDPI, QPTIFF or IMS file), the base's root is OME-Zarr 0.5:
it holds the `multiscales`, and its levels are arrays `0`, `1`, .... The plugin
copies each level and writes 0.6 metadata over the copies, keeping each
level's scale, the image's name and its `omero` channels. refkv's translation
places a level's upper-left corner in y and x (and z, for IMS; an NDPI file's
z is a plane's own position), where 0.6 places a pixel's center, so each level
moves by half its own pixel on those axes:

```python
def write(self, base: Base, options: Mapping[str, object]) -> dict[str, bytes | Range | Concat]:
    if base.profile == "hdf5":           # an HDF5 base mirrors the file's own groups
        return self.write_hdf5(base, options)
    ome = base.document("zarr.json")["attributes"]["ome"]
    ms = ome["multiscales"][0]
    cornered = {"y", "x", *({"z"} if base.profile == "ims" else ())}
    space = [a.get("name") in cornered for a in ms["axes"]]
    out: dict[str, bytes | Range | Concat] = {}
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
    out["zarr.json"] = json.dumps({"zarr_format": 3, "node_type": "group", "attributes": {"ome": out_ome}}).encode()
    return out
```

This targets the 0.6 `image` schema; the strict schema's `type` and
`metadata` members are the plugin's to add.

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
with `copy(base, array, order=[0, 2, 1])` (the positions in `dims` of the
names in `order`) into time, `y`, `x` order, at `<name>/0` below the layout
node, writes the 0.6 group `<name>/zarr.json` over it, and the layout node's
own group `zarr.json`. Missing `images`, an option that names no array of the
base, or an array whose rank does not match its dimensions is `Rejected`.
Neither case needs the facts, a codec table, or a read of the source. The
base holds an NWB file's attributes, `neurodata_type` and an `ImageSeries`'s
`starting_time` `rate` among them, so a plugin could find the image series
without being told. The implementation holds this plugin in full, about 170
lines with its option checks, and its tests run it over the fixtures.

## Not in this proposal

- **Reading the source**: a plugin reads no chunk, not even a small metadata
  array (an NWB `ImagingPlane`'s `grid_spacing`, a pyramid's `resolutions`),
  whose values its options give today. A bounded read would have to go
  through the run's own transport and pins, and a file read as a store has
  closed its files by the time a plugin runs.
- **Parser plugins**, for formats no built-in parser reads. Each would need a
  place in the signature order of [zarrify §1.2](../../spec/zarrify.md#12-input)
  (on `H` or `L`, before or after the gzip retry, and what a tie between two
  plugins is: a failure, since it is a property of the installation and not
  of the input), the reader policy, pins and sibling rules over its own reads,
  and a mirror with no built-in profile to declare it.
- **Reading a file without the files it names**, such as one file of an
  OME-TIFF set: a new mode in each parser that stops at its siblings (TIFF,
  CZI, HDF5, LIF, NIfTI pairs), specified in each format's document.
- **A source's other images.** A plugin presents what the base presents: the
  TIFF base has one image at the root ([tiff.md §4](../../spec/zarrify/tiff.md#4-the-image)),
  so an OME-TIFF's other series cannot become 0.6 images, since a plugin makes
  no references.
- **A plugin layout at the root**, in place of the image layout, whose `ome`
  member and `refkv_zarrified` declaration are the image layout's.
- **Layouts on store inputs**, whose output is joined in more than one place
  (the DICOM store form among them).
- **Rust plugins.** An installed wheel cannot load a profile written in Rust.

## Alternatives considered

- **Projections of the raw IR.** See [Why a layout](#why-a-layout-not-a-projection-of-the-raw-ir).
- **Specifying the facts per format.** It would make them a second,
  unversioned description of each format beside its convention. The base
  output is that description already.
- **Plugins under refkv's key** (`refkv_zarrified`, with the plugin's UUID). A
  key would then promise what the plugin does not keep, such as a mirror on its
  node, and readers that dispatch on the key would treat a plugin's layout as
  refkv's. For the same reason a plugin's subtree carries no refkv
  declaration, not even a copied one.
- **The layout property under the plugin's key.** The plugin's schema would
  then describe an object refkv defines, and break when refkv added a member.
- **A plugin node at the root's own level.** The HDF5 and NumPy bases put the
  source's own names there, so a reader rebuilding the source would see the
  plugin's node as one of them; `refkv_layouts`, which no base presents, keeps
  the two apart.
- **Plugins that make references.** Checking a made reference means checking
  its range against the file's size and the payload limit, recomputing its
  checksum, and knowing which source an offset of a store IR is in. Reusing the
  base's references needs none of that, and is enough for every layout that
  presents the same chunks differently.
- **VirtualiZarr parsers.** They produce an xarray dataset, not an arbitrary
  Zarr hierarchy, and keep no record of the source's other bytes.

## Open questions

1. **Large inputs.** Copying an array builds a Python mapping of its chunks,
   which for a file of millions of chunks is slow, and so is checking that
   each copied value is one of the base's; a lazier form can come later
   without changing the interface.
2. **Several plugin layouts in one call** are allowed, each at its own path;
   whether one may build on another's is left open, and today none can: each
   sees the same `Base`.
