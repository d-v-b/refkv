# Proposal: profiles defined outside refkv

Every profile refkv has is compiled into its Rust core. Someone with a format
refkv does not read, or with another reading of a format it does, cannot add a
profile without forking the repository and building their own core, wheel and
WebAssembly module. This note proposes a way to define a profile in a Python
package of one's own, found the way zarr finds codecs, and the changes to the
conventions that such a profile's output needs. It is a design note, not
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
name, it declares no convention, and it does not read under the rules refkv
applies to every input (the reader policy, the rules for
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

## The proposal

Three changes, each useful without the next:

1. **Profile plugins in Python**, found through an entry-point group.
2. **Namespaced conventions**, so that a plugin's output declares itself
   without borrowing refkv's identity.
3. **Explicit choice among readings**: name any profile, list the ones whose
   signatures match, and give a built-in profile options for its other
   readings.

A Rust plugin API is left for later ([Not now: Rust plugins](#not-now-rust-plugins)).

### 1. Profile plugins

A package registers a profile under the entry-point group `refkv.profiles`, its
key the profile's name:

```toml
[project.entry-points."refkv.profiles"]
"acme.scope" = "acme_refkv:ScopeProfile"
```

The object it names provides:

```python
class Profile(Protocol):
    name: str      # "acme.scope": the entry point's key, with a dot (below)
    title: str     # "Acme Scope", as a registry entry's
    kind: Literal["file", "store"]
    convention: ConventionInfo  # its uuid, version, schema_url, spec_url (§2 below)

    def detect(self, head: bytes) -> bool:
        """For a file profile: whether a file's first bytes (up to 552 of them) are its
        signature. For a store profile: whether a listing's root keys are its."""

    async def zarrify(self, source: Input) -> ReferenceStore:
        """The input zarrified, or `refkv.zarrify.Rejected`."""
```

`Input` is what refkv hands every profile, and the only way a plugin reads:

- `get(start, end)` and `fetch(spans)` over the input, through the transport
  `refkv.ir.planner.open_transport` opened under the run's reader policy, and
  `size`, `url` and the ETag pin that transport saw;
- `beside(name)`, a file next to the input, found, probed and bounded by the
  rules of [§1.9](../../spec/zarrify.md#19-sources-beside-the-input) and read
  under the same policy (today's `refkv.zarrify.store.beside`);
- for a store, its listing and object reads;
- `checksums`, and the `layouts` asked for.

A plugin that reads only through `Input` gets the reader policy, the pins and
the sibling rules for free. One that opens its own connections is outside them,
which the documentation for plugin authors would say plainly.

`declare`, `root_json` and `convention` (`refkv.zarrify.common`) take a `ConventionInfo`
in place of a registry name, so a plugin writes the same `Declaration` a
built-in profile does. The DICOM store form already shows that a profile can
live in Python alone: [dicom_series.py](../../python/src/refkv/zarrify/dicom_series.py)
joins per-file outputs with `refkv.compose` and declares its own root, with no
change to the core.

**Choosing a plugin.** Plugins are loaded on first use, not at import.

- **By name:** `zarrify(location, profile="acme.scope")`, `zarrify_async` the
  same, `--profile acme.scope` on `python -m refkv.zarrify`, and
  `refkv.zarrify.ProfileSource(location, "acme.scope")` beside the generated
  classes. Any built-in name works the same way.
- **By signature:** built-in profiles are tested first, in their order, and
  plugins only when none matches. Two plugins that both match reject the input,
  naming both: `several profiles match acme.scope, other.scope: name one`.

Installing a package never changes what an existing call writes, since a
plugin is never chosen over a built-in profile without being named, and a
second plugin that matches the same file makes the call fail loudly, not
silently differently.

**Names.** A built-in profile's name has no dot, a plugin's has at least one:
`<distribution or org>.<format>`. A plugin whose name has no dot, or the name of
another loaded plugin, is refused when loaded. Codecs follow the same rule as
refkv's own `refkv.<codec>`: a plugin's codecs are named under its prefix, and
it defers to imagecodecs' where one exists.

### 2. Namespaced conventions

[Conventions §1](../../spec/conventions.md#1-conventions) would allow a
convention that refkv does not define, under the same key:

- The CMO's `name` stays `refkv_zarrified`, which the Zarr conventions framework
  requires to be the key. Its `uuid`, `schema_url`, `spec_url` and
  `description` are the plugin's own, in its own repository.
- The root's `profile` is the plugin's dotted name, and its source metadata is
  under that name: `{"profile": "acme.scope", "version": 0, "source": {"url":
  ...}, "acme.scope": S}`.
- A reader that knows a convention by its UUID keeps working. A reader that
  only reads `source.url` and `profile`, such as a tool that finds where a
  hierarchy came from, works for any profile.

The one place a refkv reader looks at a profile's own metadata,
`refkv.zarr`'s fix-up of HDF5 attributes that JSON cannot hold, keys on the
built-in name `hdf5`, which a dotted plugin name can never be.

The provenance convention ([spec/provenance.md](../../spec/provenance.md)) would
also record the plugin's distribution and version, so a hierarchy says which
code wrote it.

### 3. Explicit choice among readings

- **`refkv.zarrify.candidates(location)`**: the names of every profile whose
  signature matches the input, built-in and plugin, in the order sniffing tries
  them. It reads the first bytes only, so a TIFF file lists `tiff`, not the
  variant its parser would find; the documentation says so.
- **Named profiles win.** `profile=` (or a source class) chooses the profile;
  only a built-in profile's own rules can still reject the input.
- **A built-in profile's other readings are options on its source class**, not
  new profiles. For the OME-TIFF case, `TiffSource(location, files="named")`
  would read the named file's own IFDs and leave out what its OME-XML says of
  other files, where `files="all"` (the default) is today's reading. What such
  an output's OME metadata holds is a question for
  [spec/zarrify/tiff.md](../../spec/zarrify/tiff.md); this note only proposes
  where the option lives.

## Not now: Rust plugins

Opening the core would let a downstream crate add a profile with the IR mirror
and the read planner: make `Format` a registry of `&'static dyn FileProfile`
filled at startup, and make `Parser`, `parser!` and `AnyParser::new` public.
That helps only someone who builds and ships their own core: an installed wheel
cannot load a profile written in Rust. It is deferred until a format outside
refkv needs the planner or the mirror, and the Python interface above is
designed so that such a profile could be offered to Python through it later.

## Alternatives considered

- **VirtualiZarr parsers as the plugin interface.** They already reach refkv
  through `from_virtualizarr`, and a plugin may still use one inside its
  `zarrify`. As the interface itself they fall short: a VirtualiZarr parser
  produces an xarray dataset, not an arbitrary Zarr hierarchy with OME
  metadata, and it has no signature to choose it by.
- **Plugins that outrank built-in profiles.** A precedence of a plugin's choice
  would let a package take over files refkv reads today, so installing it would
  change existing calls' output. Naming the plugin gives the same result
  explicitly.
- **Plugins declared in JSON only**, like the registry. A profile is code; the
  registry entry is what the Python side reads to describe one, and a plugin's
  `name`, `title`, `kind` and `convention` are that description.

## Open questions

1. **Which `Input` is stable.** The transport and the sibling rules are
   internal today. The plugin interface fixes a part of them; it needs a
   version of its own, separate from the conventions'.
2. **Conformance for plugins.** `conformance/zarrify/` compares implementations
   over refkv's fixtures. A plugin could run the same comparison over its own
   fixtures if the runner took a fixture directory and a profile name.
3. **Store plugins.** A store's root keys are known only after it is listed, and
   listing a large store costs. Store plugins could be tested only when no
   built-in store profile matches, as file plugins are, or wait until a store
   format outside refkv asks for one.
4. **The browser and the Rust CLI** cannot load Python plugins. They would
   refuse a plugin's name with a message saying it needs the Python package.
