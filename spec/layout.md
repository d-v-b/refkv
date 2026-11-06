# Layouts written by plugins

A **plugin layout** is the whole output of a zarrify call, written by a program other than
refkv's profiles: a Python package's plugin, named by the call
([spec/zarrify.md §1.7](zarrify.md#17-named-inputs-and-layouts)), builds it from what a built-in
profile read, that profile's output (the **base**) and the IR it was projected from
([conventions §8](conventions.md#8-the-ir-mirror)). The plugin has total control over the
output's hierarchy: refkv writes exactly the entries the plugin returns, once it has checked that
every reference reads bytes of a source refkv opened, within it, and that the archive can hold
the output. This document is not a convention: nothing it specifies is declared in an output, and
an output a plugin wrote declares whatever the plugin wrote.

Added 2026-11-06. The design and its reviews are in d-v-b/refkv#278 (design/proposals/profile-plugins.md).

The key words MUST, MUST NOT, SHOULD, SHOULD NOT and MAY are to be interpreted as described in
RFC 2119.

## 1. Plugins

A plugin has a **name**, `<distribution>.<layout>`: segments of `[a-z0-9-]+` joined by `.`. A
plugin found by its name (a Python entry point of the group `refkv.layouts`, its key the name) is
provided by the distribution whose normalized name ([PEP
503](https://peps.python.org/pep-0503/)) is the first segment; names whose first segment is
`refkv` are refkv's, for its examples. A plugin given as an object is checked against the pattern
alone. A name is for choosing a plugin and is not written in the output.

A plugin has **formats**: `"*"`, every file profile at every version of its convention, or a map
from file profile names (exactly: `ndpi` is not `tiff`) to the versions of that profile's
convention it reads. Names a refkv's registry has no file profile of are ignored, and at least
one of the names must be one it has. A call whose base is of a profile or version the plugin
does not read is a rejection of the layout request ([§6](#6-errors)).

A plugin has **options**: the call gives a JSON object (`{}` when it gives none); the plugin puts
it in canonical form, a JSON object as it is (no `NaN`, no lone surrogate, nothing but objects,
arrays, strings, numbers, booleans and null), or refuses it, before the input is opened. Each `write`
is given a copy of the canonical options of its own.

A plugin's **write** returns the output: its entries, keys from the output's root ([§3](#3-the-output)).

## 2. The base

The base is the built-in profile's output with every layout the format has (the image layout,
any dataset layout, and `refkv_source`), its root as refkv writes it, without checksums or
provenance. It depends on the input, the refkv release, the source URL and whether the call
asks for checksums, which changes how a profile joins a shard's ranges
([zarrify §2.1](zarrify.md#21-virtual-shards)), and on nothing else the call asks for. A plugin is
given, read only:

- the base's entries, documents and arrays, a node's attributes without refkv's declarations
  (`refkv_zarrified`, `refkv_provenance` and the CMOs of refkv's conventions), and its source
  metadata (the member of its `refkv_zarrified` named after the profile,
  [conventions §2](conventions.md#2-attributes));
- the base's source table, in its order, each url source with its size as refkv found it, and
  its zero data source ([zarrify §1.2](zarrify.md#12-input)), if any;
- the IR, its elements' paths, kinds, types, extents, values, forms
  ([conventions §8.5](conventions.md#85-forms-and-recipes)), targets and data members;
- the **reference** of an IR element (or of a data member of one): a range of the source that
  holds its bytes (source 0 for a file IR; for a store IR the source of the object that holds it,
  [conventions §8.9](conventions.md#89-stores)), or, when its form has a recipe, the parts of the
  recipe, a part that names a shared data source being the base's data source of those bytes when
  its table has one and those bytes as a literal otherwise. An alias is read by its own extent and
  form, and its data members are its target's. An element in a derived space, a derived element,
  an element of a store IR's object the base does not reference, and one whose form changes how
  it decodes from its siblings' (an HDF5 chunk whose filter mask skips a filter) have none;
- for a byte of a source, the innermost data element that holds it, or the struct whose data
  member holds it; for each chunk of a base array, the elements its parts run across and the
  bytes of each they read, as offsets into the element's **span**, the range of a source its own
  bytes take, no recipe applied. For a store IR, an element's extent is in its object ([conventions
  §8.9](conventions.md#89-stores)), and its source is that object's;
- **zero bytes**: a range of a zero data source of any length, the base's when it is long
  enough, else one the output's table gains ([§4](#4-writing-the-output)).

**Structure only** ([zarrify §1.2](zarrify.md#12-input)) binds a plugin, with no exception beyond
the checksums [§1.2](zarrify.md#12-input) already allows: the base gives no way to read a source's bytes, and the values
the IR holds are the structure the parser read. The parser's facts are not given.

## 3. The output

The output is a map from keys to values. A value is bytes, or a **reference value**: a `Range`
([spec/refkv.md §4.2](refkv.md#42-range)) or a `Concat` of them ([§4.3](refkv.md#43-concat)). The
output has exactly these entries: refkv writes no node, path or attribute the plugin did not
return. A plugin may write any hierarchy, Zarr v3 or v2 or neither.

A plugin may also say which bytes entries a vzip reader is given at open (`read_at_open`) and
which are kept uncompressed (`stored`); each such key MUST be a bytes entry of the output. These
are placement choices of the vzip binding ([vzip §14.2](bindings/vzip.md#142-recommended-layout-informative));
a phkv store places entries by its own size threshold and ignores both.

## 4. Writing the output

A zarrifier writes a plugin's output in these steps; a check that fails is a failure of the call
([§6](#6-errors)), and no output is written.

1. **Values.** A reference value equal to one of the base's, checksums aside, is replaced by the
   base's own value, checksums included. Every other range has its checksum dropped, and a
   `Concat` of one part is written as that part's `Range`
   ([spec/refkv.md §4.4](refkv.md#44-reference-payloads)).
2. **Keys.** Every key MUST be a string, non-empty, valid UTF-8, of at most 65535 bytes, and MUST
   NOT begin with `__vz__/` ([spec/refkv.md §2.1](refkv.md#21-keys),
   [vzip §10.3](bindings/vzip.md#103-file-names), [§11.4](bindings/vzip.md#114-hidden-entries)).
   Keys are otherwise the plugin's: `a/../b` is an ordinary key.
3. **References.** Every range MUST be a literal (bytes, with source, offset and length 0 and no
   checksum) or a range of a source in the base's table, with natural numbers for source, offset
   and length, `offset + length` at most 2^64 − 1 and within the source: within a url source's
   size, or within a data source's bytes. A range of the zero data source the plugin asked for
   ([§2](#2-the-base)) lies within it. A `key` source is never referenced (the base's table has
   none). A value's ranges total at most 2^64 − 1 bytes. Every reference's payload, as written (its `crc32c` fields included, when the call asks for checksums), MUST be at most 65519
   bytes ([zarrify §1.2](zarrify.md#12-input), [vzip §11.3](bindings/vzip.md#113-reference-entries)).
   A reference that is not the base's MUST read at most 1024 url ranges, ranges of one source at
   most 64 KiB apart counted as one ([spec/refkv.md §6.7](refkv.md#67-reader-policy) rule 5's
   default; the base's own values are not held to it). The references that are not the base's are
   given checksums, when the call asks for them, by reading their ranges as the base's are read.
4. **Placement.** The entries read at open are those `read_at_open` names; without it, a bytes
   entry equal to the base's at the same key is read at open when the base's is, and any other
   bytes entry when its key's last segment is `zarr.json`, `.zgroup`, `.zarray`, `.zattrs` or
   `.zmetadata`. A bytes entry is compressed unless it is read at open, is in `stored`, or its
   DEFLATE stream could reach 0xFFFFFFFF bytes (zlib's worst-case bound,
   [vzip §14.1](bindings/vzip.md#141-requirements)). The bytes entries read at open total at most
   256 MiB. The references come first, by key, then the bytes entries, by key.
5. **The archive.** The archive's source table is the base's, in its order, sources the output
   does not use included, with nothing added but one zero data source after them, as long as the
   most zero bytes the plugin asked for, when the base has none that long and the output
   references it. Its structure MUST be one the writer accepts, and its
   source table and page index each at most 256 MiB, the default `max_format_entry` of
   [spec/refkv.md §6.7](refkv.md#67-reader-policy) ([vzip §13.1](bindings/vzip.md#131-opening)). The output's
   keys total at most 256 MiB, as a phkv store's page index can hold every key and a reader reads
   it at open ([phkv](bindings/phkv.md)).

These checks guard against a plugin's mistakes, not against a hostile plugin, which runs in the
caller's process and can do anything the caller can.

## 5. Helpers

**Copying an array.** An array of the base is copied by these rules, so that two producers copy
one alike. The copy's document is the base's, its `attributes` those the plugin gives (`{}` by
default), its `dimension_names` those the plugin gives (each a string or null), in the copy's axis
order and not permuted, or else the base's; its chunks are the base's entries, as they are.

A copy may be **transposed** by a permutation `order` of the array's `n` axes, as
`numpy.transpose` takes one: the copy's axis `i` is the base's axis `order[i]`. Then its shape,
its chunk grid's `chunk_shape` (a `rectilinear` grid's `chunk_shapes`), its `dimension_names`
when the plugin gives none, and each chunk key's indexes (in the array's chunk key encoding,
`default` or `v2`, with its separator) are permuted by `order`. With `q` the inverse of `order`:

- an array whose first codec is not `sharding_indexed` gets a `transpose` codec of order `q`
  first, composed with a `transpose` of order `t` it already starts with into one of order
  `[q[t[i]] for each i]`, and left out when that is the identity;
- a sharded array keeps `sharding_indexed` first; its inner `chunk_shape` is permuted, its inner
  codecs get the transpose of order `q` first and its index codecs one of order `q + [n]`, each
  composed with a transpose they start with in the same way.

**An array by hand** is a Zarr v3 array document with a `regular` chunk grid, or a `rectilinear`
one (`{"kind": "inline", "chunk_shapes": ...}`) when an axis's chunk lengths are given as a list
(each a length or a `[length, count]` run), the `default` chunk key encoding with the separator
`/`, and the fill value 0 of its data type unless given; its chunks are at the keys that encoding
gives their coordinates. **A group by hand** is a Zarr v3 group
document.

**Coding routines.** These give the codecs, after any `transpose`, and the data types the
built-in profiles write, and add no rule: each is the table or rule of the section it names, with
the checks the profile applies to those parameters (its source model's rule roles), and refuses
parameters the section rejects.

| routine | section |
|---|---|
| a TIFF IFD's data type, from SampleFormat and BitsPerSample | [tiff.md §4.3](zarrify/tiff.md#43-data-types-and-codecs) (the `format` and `image` roles) |
| a TIFF IFD's codecs | [tiff.md §4.3](zarrify/tiff.md#43-data-types-and-codecs) (`format`, `image`, `samples`, `predictor`); EER's for a chunk shape the plugin gives, as its codec decodes one |
| a TIFF IFD's byte order, its own file's header's | [tiff.md §5.1](zarrify/tiff.md#51-elements) |
| a CZI subblock form's codecs | [czi.md §3.1](zarrify/czi.md#31-pixel-types-and-codecs) |
| a VSI tile file's codec | [vsi.md §4.1](zarrify/vsi.md#41-the-pyramid) |
| a DICOM frame's codec, by its transfer syntax | [dicom.md §2.1](zarrify/dicom.md#21-transfer-syntax) |
| gzip, after `refkv.skip` of an inflated stream's first bytes (the codecs after `bytes`) | [npy.md §4.4](zarrify/npy.md#44-the-refkvskip-codec) |
| an HDF5 node's attributes, the encodings JSON needed undone by their described datatypes | [hdf5.md §6](zarrify/hdf5.md#6-attributes), [§7](zarrify/hdf5.md#7-source-metadata) |

## 6. Errors

A layout request that is wrong whatever the input (a name no plugin has, a plugin that fails to
load or whose attributes break [§1](#1-plugins), options the plugin refuses, a plugin layout beside built-in
layouts, a store input) is a **usage error**. A base the plugin does not read, or a refusal the
plugin's `write` raises for its input, is a **rejection** of the layout request: the input is one
the profile accepts, but the call writes no output. Anything else a plugin raises, and an output
that breaks [§3](#3-the-output) or [§4](#4-writing-the-output), is a **failure**. None writes an output.

## 7. Determinism

A plugin's output MUST be a function of its base, its canonical options and the plugin's code
(its release, and the versions of what it imports), and not of set or dict iteration order that
hashing changes; the plugin fixes the exact bytes of every bytes entry it writes. Then two runs
with the same refkv release, input, source URL, checksums choice, plugin code and options write
the same output, compared entry for entry ([zarrify §1.1](zarrify.md#11-output-and-equivalence)).
A zarrifier cannot check this; the plugin promises it.
