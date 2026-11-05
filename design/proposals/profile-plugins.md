# Proposal: profiles defined outside refkv

Every profile refkv has is compiled into its Rust core. Someone with a format
refkv does not read, or with another reading of a format it does, cannot add a
profile without forking the repository and building their own core, wheel and
WebAssembly module. This note proposes a way to define a profile in a Python
package of one's own, found the way zarr finds codecs. The center of it is the
core's IR: every format's parser already produces one, and Python should see
all of it, for every format, so that a profile written in Python can start
from a parsed source instead of from bytes. It is a design note, not
normative.

## What fixes the set of profiles today

- **The Rust core.** [build.rs](../../rust/refkv-ir/build.rs) generates the
  `Format` enum, the sniff order and the module list from the registry's copies
  in `rust/refkv-ir/profiles/`, inside the crate. The per-profile code already
  has the shape of a plugin: a profile is a
  [`FileProfile`](../../rust/refkv-ir/src/run.rs) (its signature, its parser,
  how the planner answers it, its projection), and its parser a sans-IO
  `Parser`. But `parser!` is private to the crate, so is the trait object behind
  `AnyParser`, and `Format` is closed. The Python wheel and the browser code
  ship a built core, so even an open Rust API would mean rebuilding both.
- **Python.** `zarrify()` passes the profile's name to the core
  (`refkv.ir.zarrify_async(fmt=...)`), and `refkv.ir.sniffed` asks the core.
  The `Source` and `Store` classes of `refkv.zarrify` are generated from the
  registry, one per profile (`profile_classes.py`); a subclass with a new
  `profile` reaches the core, which refuses the name. A store's profile is
  chosen by `choose_profile`, a fixed test of its root keys.
- **The command lines and the browser.** Neither `python -m refkv.zarrify` nor
  the Rust CLI can name a profile (the Rust CLI's `--format` chooses the
  container). The browser's `zarrifyImage` always sniffs.
- **The conventions.** Every convention's metadata object is named
  `refkv_zarrified`, its `schema_url` and `spec_url` are in `d-v-b/refkv`, and
  the root's `profile` is a bare name such as `"tiff"`
  ([spec/conventions.md §1](../../spec/conventions.md#1-conventions),
  [§2](../../spec/conventions.md#2-attributes)). There is no namespace for a
  profile refkv does not define, and `refkv.zarrify.common.convention()` builds
  the object only for a registry entry.

What does work outside refkv is building a store by hand:
`refkv.ReferenceStore(mapping, sources)`, or a VirtualiZarr parser's dataset
through `ReferenceStore.from_virtualizarr(vds)`. The store is then a refkv
store like any other, and its readers need nothing from the code that made it
but its codecs, which zarr already finds through the `zarr.codecs` entry points
(refkv's own are registered that way). What the hand-built store lacks is
everything that makes it a zarrified input: it is never chosen by sniffing or by
name, it declares no convention, it has no IR mirror, and it does not read
under the rules refkv applies to every input (the reader policy, the rules for
[sources beside the input](../../spec/zarrify.md#19-sources-beside-the-input),
the pins of [§1.2](../../spec/zarrify.md#12-input)).

## One input, several readings

The same file can also have more than one reasonable reading, and today exactly
one is ever offered. A file's first bytes choose one profile, the first whose
signature matches in the registry's `sniff` order
([§1.2](../../spec/zarrify.md#12-input)). A TIFF variant is decided by the TIFF
parser, not by a signature (`chosen_by`: TIFF chooses NDPI and QPTIFF, HDF5
chooses IMS). Naming a profile skips the signature test, but not the variant:
a file named as `tiff` is read as the variant the TIFF parser finds
([§1.7](../../spec/zarrify.md#17-named-inputs-and-layouts)).

So an OME-TIFF that names other files is read across them, or not at all. With
`fixtures/tiff/ome_set_a.ome.tif` copied away from the rest of its set, both
`zarrify(path)` and `zarrify(TiffSource(path))` reject it, with
`the file "ome_set_b.ome.tif" does not exist`; no option reads only the named
file's own IFDs. A plugin could not offer that reading either, since a file the
TIFF profile claims never reaches it.

## What Python sees of the IR today

The IR is the right thing to build on. It is format-free and specified
([conventions §8](../../spec/conventions.md#8-the-ir-mirror)): a tree of
elements whose leaves partition the source's bytes, each typed, so that a
reader rebuilds the source from it byte for byte. Each format's document gives
its source model, what its elements are. A parser rejects what the format's
rules reject and stays within [the references'
budget](../../spec/zarrify.md#12-input), and the read planner reads only what
the parser asks for.

Python already reaches part of it, through internal names:

- `refkv.ir.parse(fmt, transport)` drives a file run and returns the
  `refkv._core.Ir`, with accessors for each element's path, kind, type, extent
  and value, and its children.
- `refkv._core.project(fmt, ir, facts, url)` is a built-in profile's
  projection, and `refkv._core.mirror(ir, profile)` the mirror of an IR under a
  registry profile's convention.

Run over the smallest fixture of each file format, `parse` gives the IR of
DICOM, FITS, FLAC, MP3, MP4, MRC, ND2, NIfTI, NumPy, SAFE (its zip form),
safetensors and TIFF. It gives none in three cases:

- **Files read as stores** (VSI, NRRD, GRIB) are refused (`a VSI file is
  zarrified as a store run, with its tile files`).
- **Files that name others** (an OME-TIFF set, a CZI file in parts, an HDF5
  file with external storage, a LIF `.xlcf`) stop with `a file that names
  siblings has no file run's output`, the lone `ome_set_a.ome.tif` among them.
- **Stores.** `refkv.ir.store` drives a store run and returns its output only:
  `StoreRun.finish()` gives the core's IR, which `run_store_async` drops.

The core's projections read a second input beside the IR, the parser's
**facts**: a JSON object of format-specific keys (TIFF's are `bigtiff`,
`bits`, `compression`, `levels` and so on), decisions the parser took while
reading. The facts are specified nowhere. A projection written outside the
core must not depend on them, so the question is whether the IR alone is
enough. For TIFF it is: a sketch of about forty lines that walks
`ifds/0/tags/<n>/value` (dimensions, sample format, and the tile or strip
offsets) and references each tile or strip as a chunk, using no facts,
reads back equal to the built-in profile's array for
`float32_uncompressed_be.ome.tif` (tiled, big-endian) and `strips_uint8.tif`.
It fails on `strips_uint16_short.tif`, whose last strip is shorter than a
chunk: the built-in profile pads it with a zero data source, which is the kind
of help a plugin should get from refkv rather than write again.

## The proposal

Four changes, each useful without the next:

1. **The full IR in Python, for every format**, as a public API.
2. **Profile plugins in Python**, found through an entry-point group: most of
   them projections of a built-in parser's IR, and some parsers of their own.
3. **Namespaced conventions**, so that a plugin's output declares itself
   without borrowing refkv's identity.
4. **Explicit choice among readings**: name any profile, list the ones that
   match an input, and read a file without its siblings.

A Rust plugin API is left for later ([Not now: Rust plugins](#not-now-rust-plugins)).

### 1. The full IR in Python

`refkv.ir.read(location, format, *, policy=None, siblings="follow")` reads an
input with a built-in parser and returns its IR, for every format: a file
run's, a file read as a store's, and a store's (a multi-source IR,
[conventions §8.9](../../spec/conventions.md#89-stores)). It reads through the
same planner and under the same reader policy as `zarrify`, and rejects what
the profile rejects.

- **`refkv.ir.Ir` is public**, with the accessors `_core.Ir` has, and
  `at(path)` for an element's index by its path. Its accessors and the element
  paths of each format's source model are the stable surface; the facts are
  not part of it.
- **The store runs' IR** is returned instead of dropped: `run_store_async`
  keeps what `StoreRun.finish()` gives.
- **`siblings="none"`** reads only the named file: a parser that would stop
  because the file names others gives that file's own IR, the names among its
  values, instead of `a file that names siblings has no file run's output`. A
  lone `ome_set_a.ome.tif` then has an IR, its OME-XML naming `ome_set_b`
  included, whatever a projection then does with it.
- **The mirror** takes a convention (`ConventionInfo`, below) instead of a
  registry name, so that a plugin's output keeps the mirror under
  `refkv_source`, and with it the whole source's metadata.
- **Shared pieces of projections** become public Python helpers: a reference
  padded by a zero data source, a virtual shard of chunks
  ([§2.1](../../spec/zarrify.md#21-virtual-shards)), the OME-NGFF image object
  (`image_ome` today).

### 2. Profile plugins

A package registers a profile under the entry-point group `refkv.profiles`, its
key the profile's name:

```toml
[project.entry-points."refkv.profiles"]
"acme.tiff-alone" = "acme_refkv:TiffAlone"
```

There are two kinds.

**A projection plugin** names the built-in parser it reads with, and turns that
parser's IR into a hierarchy. It writes no parser, and gets the parser's
rejections, its budget and the read planner for nothing. This is the kind for
another reading of a format refkv reads:

```python
class TiffAlone:
    name = "acme.tiff-alone"
    title = "TIFF, one file"
    parser = "tiff"              # a built-in profile's parser
    siblings = "none"            # read only the named file
    convention = ConventionInfo(uuid=..., version=0, schema_url=..., spec_url=...)

    def project(self, ir: Ir, source: Input) -> ReferenceStore:
        width = ir.value(ir.at("ifds/0/tags/256/value"))[0]
        ...
```

**A parser plugin** reads bytes itself, for a format no built-in parser reads:

```python
class ScopeProfile:
    name = "acme.scope"
    title = "Acme Scope"
    kind = "file"
    convention = ConventionInfo(...)

    def detect(self, head: bytes) -> bool:
        return head.startswith(b"ACMESCOP")

    async def zarrify(self, source: Input) -> ReferenceStore:
        (n,) = struct.unpack("<I", await source.get(8, 12))
        ...
```

A parser plugin has no IR, so its output has no mirror unless it builds one; a
plugin that wants the mirror, the planner and the budget is better written as
a Rust parser in refkv, or as a Rust plugin later.

`Input` is what refkv hands every plugin, and the only way a plugin reads:

- `await get(start, end)` and `await fetch(spans)` over the input, through the
  transport `refkv.ir.planner.open_transport` opened under the run's reader
  policy (its blocking reads run on refkv's threads), and `size`, `url` and
  the ETag pin that transport saw;
- `await beside(name)`, a file next to the input, found, probed and bounded by
  the rules of [§1.9](../../spec/zarrify.md#19-sources-beside-the-input) and
  read under the same policy (today's `refkv.zarrify.store.beside`);
- for a store, its listing and object reads;
- `checksums`, and the `layouts` asked for. refkv records the checksums
  itself, after the plugin returns, as it does for its own profiles.

A plugin that reads only through `Input` gets the reader policy, the pins and
the sibling rules for free. One that opens its own connections is outside them,
which the documentation for plugin authors would say plainly.

`declare`, `root_json` and `convention` (`refkv.zarrify.common`) take a
`ConventionInfo` in place of a registry name, so a plugin writes the same
`Declaration` a built-in profile does. The DICOM store form already shows that
a profile can live in Python alone:
[dicom_series.py](../../python/src/refkv/zarrify/dicom_series.py) joins
per-file outputs with `refkv.compose` and declares its own root, with no change
to the core.

**Choosing a plugin.** Plugins are loaded on first use, not at import.

- **By name:** `zarrify(location, profile="acme.tiff-alone")`, `zarrify_async`
  the same, `--profile acme.tiff-alone` on `python -m refkv.zarrify`, and
  `refkv.zarrify.ProfileSource(location, "acme.tiff-alone")` beside the
  generated classes. Any built-in name works the same way.
- **By signature:** built-in profiles are tested first, in their order, and
  parser plugins only when none matches. A projection plugin has its parser's
  signature, so it is only ever chosen by name. Two parser plugins that both
  match reject the input, naming both: `several profiles match acme.scope,
  other.scope: name one`.

Installing a package never changes what an existing call writes, since a
plugin is never chosen over a built-in profile without being named, and a
second plugin that matches the same file makes the call fail loudly, not
silently differently.

**Names.** A built-in profile's name has no dot, a plugin's has at least one:
`<distribution or org>.<format>`. A plugin whose name has no dot, or the name of
another loaded plugin, is refused when loaded. Codecs follow the same rule as
refkv's own `refkv.<codec>`: a plugin's codecs are named under its prefix, and
it defers to imagecodecs' where one exists.

### 3. Namespaced conventions

[Conventions §1](../../spec/conventions.md#1-conventions) would allow a
convention that refkv does not define, under the same key:

- The CMO's `name` stays `refkv_zarrified`, which the Zarr conventions framework
  requires to be the key. Its `uuid`, `schema_url`, `spec_url` and
  `description` are the plugin's own, in its own repository.
- The root's `profile` is the plugin's dotted name, and its source metadata is
  under that name: `{"profile": "acme.tiff-alone", "version": 0, "source":
  {"url": ...}, "acme.tiff-alone": S}`.
- A reader that knows a convention by its UUID keeps working. A reader that
  only reads `source.url` and `profile`, such as a tool that finds where a
  hierarchy came from, works for any profile.
- A projection plugin's mirror is its parser's source model, so a reader of
  the mirror needs nothing from the plugin: the mirror is format-free.

The one place a refkv reader looks at a profile's own metadata,
`refkv.zarr`'s fix-up of HDF5 attributes that JSON cannot hold, keys on the
built-in name `hdf5`, which a dotted plugin name can never be.

The provenance convention ([spec/provenance.md](../../spec/provenance.md)) would
also record the plugin's distribution and version, so a hierarchy says which
code wrote it.

### 4. Explicit choice among readings

- **`refkv.zarrify.candidates(location)`**: the names of every profile that
  could read the input, in the order sniffing tries them: the built-in profile
  whose signature matches, the projection plugins of its parser, and the parser
  plugins whose signatures match. It reads the first bytes only, so a TIFF
  file lists `tiff`, not the variant its parser would find; the documentation
  says so.
- **Named profiles win.** `profile=` (or a source class) chooses the profile;
  only that profile's own rules can still reject the input.
- **A built-in reading without siblings.** `siblings="none"` on a built-in
  profile's source class, such as `TiffSource(location, siblings="none")`,
  would read the named file alone. What such an output's OME metadata holds is
  a question for [spec/zarrify/tiff.md](../../spec/zarrify/tiff.md); with the
  IR of [§1](#1-the-full-ir-in-python), a plugin can answer it differently
  first.

## Not now: Rust plugins

Opening the core would let a downstream crate add a parser with the IR mirror
and the read planner: make `Format` a registry of `&'static dyn FileProfile`
filled at startup, and make `Parser`, `parser!` and `AnyParser::new` public.
That helps only someone who builds and ships their own core: an installed wheel
cannot load a profile written in Rust. It is deferred until a format outside
refkv needs the planner or the mirror; a parser added that way would reach
Python through the same `refkv.ir.read`.

## Alternatives considered

- **VirtualiZarr parsers as the plugin interface.** They already reach refkv
  through `from_virtualizarr`, and a parser plugin may still use one inside its
  `zarrify`. As the interface itself they fall short: a VirtualiZarr parser
  produces an xarray dataset, not an arbitrary Zarr hierarchy with OME
  metadata, it keeps no record of the source's other bytes, and it has no
  signature to choose it by.
- **Exposing the facts instead of the IR.** The facts are what the core's own
  projections read, so exposing them would be quicker. But they are the
  parser's decisions for one reading, not the source, and they change whenever
  that reading does; a plugin built on them breaks with the built-in profile.
- **Plugins that outrank built-in profiles.** A precedence of a plugin's choice
  would let a package take over files refkv reads today, so installing it would
  change existing calls' output. Naming the plugin gives the same result
  explicitly.
- **Plugins declared in JSON only**, like the registry. A profile is code; the
  registry entry is what the Python side reads to describe one, and a plugin's
  `name`, `title` and `convention` are that description.

## Open questions

1. **Which surface is stable.** The IR's accessors and each format's element
   paths would be; `Input` and the helpers need a version of their own,
   separate from the conventions'. Element paths are part of each source
   model already, so a change to one is already a change to that convention.
2. **The facts' future.** Every projection in the core reads facts. If each
   could be derived from the IR, the core's own projections could become
   examples of the plugin interface; if some cannot, the IR is missing
   something a source model should hold.
3. **Conformance for plugins.** `conformance/zarrify/` compares implementations
   over refkv's fixtures. A plugin could run the same comparison over its own
   fixtures if the runner took a fixture directory and a profile name.
4. **Store plugins.** A store's root keys are known only after it is listed,
   and listing a large store costs. Store parser plugins could be tested only
   when no built-in store profile matches, or wait until a store format
   outside refkv asks for one; store projection plugins need nothing new.
5. **The browser and the Rust CLI** cannot load Python plugins. They would
   refuse a plugin's name with a message saying it needs the Python package.
