# Layouts written by plugins

A **plugin layout** is the whole output of a zarrify call, written by a program other than
refkv's profiles: a Python package's plugin, named by the call
([spec/zarrify.md §1.7](zarrify.md#17-named-inputs-and-layouts)), builds it from the input as a
built-in profile's parser read it, its IR ([conventions §8](conventions.md#8-the-ir-mirror)). A
plugin layout is a projection of the IR, as the profile's own output is: neither is built from
the other, and a plugin is given no output of refkv's. The plugin has total control over the
output's hierarchy: refkv writes exactly the entries the plugin returns, once it has checked that
every reference reads bytes of a source refkv opened, within it, and that the archive can hold
the output. This document is not a convention: nothing it specifies is declared in an output, and
an output a plugin wrote declares whatever the plugin wrote.

Added 2026-11-06. The design and its reviews are in d-v-b/refkv#278 (design/proposals/profile-plugins.md).
Changed 2026-11-20: a plugin is given the parsed inputs, not the built-in output (the **base** of
this document's first version), which it asks for where it wants it; and what refkv checks of a
plugin's output is the seal of [§4](#4-sealing-the-output), for any number of inputs.

The key words MUST, MUST NOT, SHOULD, SHOULD NOT and MAY are to be interpreted as described in
RFC 2119.

## 1. Plugins

A plugin has a **name**, `<distribution>.<layout>`: segments of `[a-z0-9-]+` joined by `.`. A
plugin found by its name (a Python entry point of the group `refkv.layouts`, its key the name) is
provided by the distribution whose normalized name ([PEP
503](https://peps.python.org/pep-0503/)) is the first segment; names whose first segment is
`refkv` are refkv's, for its examples. A plugin given as an object is checked against the pattern
alone. A name is for choosing a plugin and is not written in the output.

A plugin has **formats**: `"*"`, every file and store profile at every version of its
convention, or a map from profile names, file or store profiles (exactly: `ndpi` is not `tiff`,
nor `ome-zarr` `zarr2`), to the versions of that profile's convention it reads. Names a refkv's
registry has no file or store profile of are ignored, and at least one of the names must be one
it has. A store input's IR is a store IR
([conventions §8.9](conventions.md#89-stores)); the DICOM store form
([zarrify/dicom.md §9](zarrify/dicom.md#9-the-store-form)) keeps no IR, and takes no plugin. A call whose input is of a profile or version the plugin
does not read is a rejection of the layout request ([§6](#6-errors)), made once the input is
parsed and before anything is built of it.

A plugin has **options**: the call gives a JSON object (`{}` when it gives none); the plugin puts
it in canonical form, a JSON object as it is (no `NaN`, no lone surrogate, nothing but objects,
arrays, strings, numbers, booleans and null), or refuses it, before the input is opened. Each `write`
is given a copy of the canonical options of its own.

A plugin's **write** returns the output: its entries, keys from the output's root ([§3](#3-the-output)).

## 2. The inputs

A plugin's `write` is given the **inputs** of the call, each as its profile's parser read it, and
the plugin's options. A zarrify call has one input; the same rules hold for an output built of
several. An input gives, read only:

- its profile and the version of that profile's convention;
- its sources, in the IR's order, each url source with its size and pins as refkv found them
  ([zarrify §1.2](zarrify.md#12-input)): one for a file, one for each object of a store or of a
  file read with those beside it ([conventions §8.9](conventions.md#89-stores));
- the IR, its elements' paths, kinds, types, extents, values, forms
  ([conventions §8.5](conventions.md#85-forms-and-recipes)), targets and data members, and its
  stored arrays and chunk map ([stored-array.md](stored-array.md));
- the **reference** of an IR element (or of a data member of one): a range of the source that
  holds its bytes, or, when its form has a recipe, the parts of the recipe. An alias is read by
  its own extent and form, and its data members are its target's. An element in a derived space,
  a derived element, and one whose form changes how it decodes from its siblings' (an HDF5 chunk
  whose filter mask skips a filter) have none;
- an element's **span**, the range of a source its own bytes take, no recipe applied; and for a
  byte of a source, the innermost data element that holds it, or the struct whose data member
  holds it;
- the bytes of a source, read through the input as refkv read them, within a bound the caller
  sets.

A reference is a **value**: a sequence of parts, each naming what it reads: a range of one of an
input's sources; bytes many references share, which become a data source; a range of a data
source of the plugin's own; zero bytes, of the zero data source
([zarrify §1.2](zarrify.md#12-input)); or bytes in the reference itself. A value names its
sources by the input they are of, not by a number: the source table is numbered when the output
is sealed ([§4](#4-sealing-the-output)).

**The built-in output.** An input's profile has its own projection of the IR, the output a
zarrify call without a plugin writes (with every layout the format has, and `refkv_source`). A
plugin that wants it asks for it, and is given its entries, documents, arrays and attributes
(a node's attributes without refkv's declarations, and its source metadata,
[conventions §2](conventions.md#2-attributes)), its references as values of the input, and for
each chunk of one of its arrays the IR elements its parts run across. It is made when first
asked for, with the reads that output alone makes, and not otherwise: a plugin that does not ask
pays nothing for it, and does not depend on what it presents. It depends on the input, the refkv
release, and whether the call asks for checksums, which changes how a profile joins a shard's
ranges ([zarrify §2.1](zarrify.md#21-virtual-shards)).

**Structure only** ([zarrify §1.2](zarrify.md#12-input)) binds refkv's conventions: an output
that keeps refkv's declarations claims to follow them, Structure only with them. A plugin's
output that keeps none claims nothing of them, whatever it read.

## 3. The output

The output is a map from keys to values. A value is bytes, or a reference
([§2](#2-the-inputs)), which is written as a `Range`
([spec/refkv.md §4.2](refkv.md#42-range)) or a `Concat` of them ([§4.3](refkv.md#43-concat)). The
output has exactly these entries: refkv writes no node, path or attribute the plugin did not
return. A plugin may write any hierarchy, Zarr v3 or v2 or neither.

A plugin may also say, of a bytes entry, whether a vzip reader is given it at open (its
**placement**) and whether it is kept uncompressed (`stored`); each such key MUST be a bytes entry
of the output. These are placement choices of the vzip binding
([vzip §14.2](bindings/vzip.md#142-recommended-layout-informative)); a phkv store places entries
by its own size threshold and ignores both. An entry the plugin takes from the built-in output
as it is comes with that output's placement, which the plugin keeps by keeping the entry in the
map it was given in; refkv infers no placement from an entry's bytes.

## 4. Sealing the output

A zarrifier **seals** an output built of parsed inputs in these steps; a check that fails is a
failure of the call ([§6](#6-errors)), and no output is written. The built-in outputs are sealed
by the same steps.

1. **Keys.** Every key MUST be a string, non-empty, valid UTF-8, of at most 65535 bytes, and MUST
   NOT begin with `__vz__/` ([spec/refkv.md §2.1](refkv.md#21-keys),
   [vzip §10.3](bindings/vzip.md#103-file-names), [§11.4](bindings/vzip.md#114-hidden-entries)).
   Keys are otherwise the plugin's: `a/../b` is an ordinary key.
2. **The source table** is numbered by first use, over the references in the order of their
   keys' UTF-8 bytes and, within a reference, of its parts: the url sources first, then the data
   sources (each distinct byte string references share or the plugin gave, and one zero data
   source as long as the longest run of zero bytes any value holds, at most 256 MiB). One input
   that is a plain file (not a store, with one source) is source 0 whether or not a value uses
   it, as [zarrify §1.2](zarrify.md#12-input) says; otherwise a source no value uses is not in
   the table. Equal url sources (URL and pins) are one; one URL with different pins is refused,
   and so is a part that names an input the output was not sealed with. A `Concat` of one part
   is written as that part's `Range` ([spec/refkv.md §4.4](refkv.md#44-reference-payloads)).
3. **References.** Every range MUST have natural numbers for offset and length, `offset +
   length` at most 2^64 − 1 and within its source: within a url source's size as the input's
   parser found it, or within a data source's bytes. A value's ranges total at most 2^64 − 1
   bytes. Every reference's payload, as written, MUST be at most 65519 bytes
   ([zarrify §1.2](zarrify.md#12-input), [vzip §11.3](bindings/vzip.md#113-reference-entries));
   when the inputs were read for checksums, with each url range's `crc32c` field at its largest
   (6 bytes), so that whether an output is sealed never depends on its bytes. How many reads or
   bytes a reference takes is its reader's limit
   ([spec/refkv.md §6.7](refkv.md#67-reader-policy) rule 5), not the output's.
4. **Placement.** A bytes entry is read at open as its placement says; one with none, when its
   key's last segment is `zarr.json`, `.zgroup`, `.zarray`, `.zattrs` or `.zmetadata`. A bytes
   entry is compressed unless it is read at open, is `stored`, or its DEFLATE stream could reach
   0xFFFFFFFF bytes (zlib's worst-case bound, [vzip §14.1](bindings/vzip.md#141-requirements)).
   The bytes entries read at open total at most 256 MiB. The references come first, by key, then
   the bytes entries, by key.
5. **The archive.** Its structure MUST be one the writer accepts, and its source table and page
   index each at most 256 MiB, the default `max_format_entry` of
   [spec/refkv.md §6.7](refkv.md#67-reader-policy) ([vzip §13.1](bindings/vzip.md#131-opening)),
   or the store's reader policy's, when that is less. The output's keys, each with 16 bytes for
   its page entry, total at most 256 MiB, as a phkv store's page index can hold every key and a
   reader reads it at open ([phkv](bindings/phkv.md)).
6. **Checksums.** When the inputs were read for checksums, every range of a url source is given
   the CRC-32C of its bytes, read through the inputs, which must still be open; inputs read with
   and without checksums are refused together. Without checksums, sealing reads nothing.

These checks guard against a plugin's mistakes, not against a hostile plugin, which runs in the
caller's process and can do anything the caller can.

## 5. Helpers

**Copying an array.** An array of the built-in output ([§2](#2-the-inputs)) is copied by these
rules, so that two producers copy one alike. The copy's document is that array's, its
`attributes` those the plugin gives (`{}` by default), its `dimension_names` those the plugin
gives (each a string or null), in the copy's axis order and not permuted, or else the array's;
its chunks are the array's entries, as they are.

A copy may be **transposed** by a permutation `order` of the array's `n` axes, as
`numpy.transpose` takes one: the copy's axis `i` is the array's axis `order[i]`. Then its shape,
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
layouts) is a **usage error**. An input of a profile or version the plugin does not read, an
input that keeps no IR (the DICOM store form), or a refusal the
plugin's `write` raises for its input, is a **rejection** of the layout request: the input is one
the profile's parser accepts, but the call writes no output. A refusal of the input by the
built-in projection, which a plugin meets only if it asks for the built-in output, is such a
rejection too. Anything else a plugin raises, and an output
that breaks [§3](#3-the-output) or [§4](#4-sealing-the-output), is a **failure**. None writes an output.
A read of the input that fails is none of these, whether the parser, the built-in output a
plugin asked for or the seal's checksums made it: the call fails as it fails without a plugin.

## 7. Determinism

A plugin's output MUST be a function of its inputs (their IRs, every byte it read through
them, and their built-in outputs where it asked for them), its canonical options and the
plugin's code (its release, and the versions of what it imports), and not of set or dict
iteration order that hashing changes; the plugin fixes the exact bytes of every bytes entry it writes. Then two runs
with the same refkv release, input, source URL, checksums choice, plugin code and options write
the same output, compared entry for entry ([zarrify §1.1](zarrify.md#11-output-and-equivalence)).
A zarrifier cannot check this; the plugin promises it.
