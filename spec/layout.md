# refkv's layout convention

A **plugin layout** is a presentation of a zarrified source that a program other than refkv's
profiles writes: a Python package's plugin adds it to the hierarchy a built-in profile wrote
([spec/zarrify.md §1.7](zarrify.md#17-named-inputs-and-layouts)), beside the image layout and
any dataset layout ([conventions §9](conventions.md#9-dataset-layouts)), and built from that
profile's output. This convention, `refkv_layout`, says where such a layout is, what it may hold,
and what it was written from. Each plugin layout also follows a convention of the plugin's own,
which says what the layout is.

Convention version: 0 (draft) · UUID: `372e9a9f-06b1-4ea1-8829-e206c05e03e6` ·
Schema: [schema.json](layout/schema.json)

Added 2026-11-06. The design and its reviews are in d-v-b/refkv#278.

The key words MUST, MUST NOT, SHOULD, SHOULD NOT and MAY are to be interpreted as described in
RFC 2119.

## 1. Declaration

The convention follows the [Zarr conventions
framework](https://github.com/zarr-conventions/zarr-conventions-spec) and nests its property
under one key, `refkv_layout`, which is also its `name`. At version `N` it is declared by the
Convention Metadata Object (CMO)

```json
{
  "uuid": "372e9a9f-06b1-4ea1-8829-e206c05e03e6",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/tags/layout-v<N>/spec/layout/schema.json",
  "spec_url": "https://github.com/d-v-b/refkv/blob/layout-v<N>/spec/layout.md",
  "name": "refkv_layout",
  "description": "A layout of a zarrified source that a plugin wrote, and what it was written from"
}
```

with no other members. Versions follow [conventions §1](conventions.md#1-conventions), tagged
`layout-v<N>`: at version 0, which has no tag, the URLs name the development branch,
`.../refs/heads/main/spec/layout/schema.json` and `.../blob/main/spec/layout.md`.

Two kinds of node declare it:

- **The layouts' group**, the root's child `refkv_layouts`, a group whose attributes are this
  CMO in `zarr_conventions` and <a id="type-layoutsgroup"></a>**`LayoutsGroup`** under
  `refkv_layout`:

  | member | type | presence | value |
  |---|---|---|---|
  | `version` | integer | required | `N`, this convention's version |

  and nothing else. A reader knows the group by this CMO, not by its name.
- **A layout's node**, a child group `refkv_layouts/<path>` of it, whose attributes hold, in
  `zarr_conventions`, this CMO first and the plugin's convention's CMO second, and under
  `refkv_layout` a <a id="type-layoutproperty"></a>**`LayoutProperty`**:

  | member | type | presence | value |
  |---|---|---|---|
  | `version` | integer | required | `N`, this convention's version |
  | `layout` | [`LayoutName`](#type-layoutname) | required | the plugin's layout |
  | `source` | object | required | the root's `refkv_zarrified.source`, as it is |
  | `base` | [`LayoutBase`](#type-layoutbase) | required | the built-in convention the layout was written from |
  | `options` | object | required | the layout's options, in the canonical form its convention gives |

  <a id="type-layoutname"></a>**`LayoutName`**:

  | member | type | presence | value |
  |---|---|---|---|
  | `name` | string | required | the layout's name ([§5](#5-names-identity-and-versions)) |
  | `uuid` | string | required | its convention's UUID |
  | `version` | integer | required | its convention's version |

  <a id="type-layoutbase"></a>**`LayoutBase`**:

  | member | type | presence | value |
  |---|---|---|---|
  | `uuid` | string | required | the UUID of the convention the root declares (the profile's) |
  | `version` | integer | required | that convention's version |

A reader tells the two apart by the member `layout`, which only a layout's node has. The
layout's node also holds, under the plugin's key ([§5](#5-names-identity-and-versions)), an
object whose member `version` is the plugin's convention's version, so that a reader that knows
only the plugin's convention finds its version without this one; the plugin's convention gives
its other members.

## 2. Plugins

A plugin is given the built-in output as a read-only **base**: the image layout and
`refkv_source`, without a dataset layout, its root without `layouts`, and its ranges without
checksums (which are of the source's bytes), so that what it writes does not depend on which
built-in layouts the call asked for, or on the checksums' values. (A profile may cut a reference
into more ranges when the call asks for checksums,
[spec/zarrify.md §2.1](zarrify.md#21-virtual-shards); a plugin sees the ranges as they are.) It
is given the base's IR, and the options the caller gave (a JSON object, its members named by
strings at every depth), in its canonical form, which nests at
most 253 deep, so that the layout's node, which holds it, nests at most 256. It returns the
layout's entries, keys relative to its node, and refkv writes them as [§3](#3-the-entries) says.

A plugin provides the layout's name, its default path, the base profiles it reads, and its
convention's description: its version, a natural number, and the members of its CMO, each a
non-empty string, but for `uuid` and `name`, which [§5](#5-names-identity-and-versions) fixes. A call that asks for the layout gives
its name, its options and, optionally, a path, which replaces the plugin's.

A plugin names the base profiles it reads, each with the natural numbers of the convention
versions it reads (a profile mapped to none reads no version of it), and a base that is not one
of them refuses the layout request. Every name's versions are natural numbers; names that are no
file profile of the implementation are otherwise ignored, so that a plugin written for a later one
loads, but a plugin that names none is refused when it is loaded, as is one whose attributes break
[§5](#5-names-identity-and-versions)'s rules.

**Structure only.** A plugin's layout is a function of the base's structure (its documents and
bytes entries, and its IR) and its options: it MUST NOT depend on the bytes the base's
references name, so [spec/zarrify.md §1.2](zarrify.md#12-input)'s **Structure only** holds for
it, with no exception.

## 3. The entries

A plugin layout's entries, keys relative to its node, are checked; a failed check is the plugin's
failure, and no output is written:

- Every key is a valid key ([spec/refkv.md §2](refkv.md#2-data-model)), valid UTF-8, with no
  empty, `.` or `..` segment, no segment that starts with `__`, and `zarr.json` only as its last
  segment; with `refkv_layouts/<path>/` before it, it is at most 65535 bytes, as a ZIP archive's
  keys are.
- Every document is strict JSON in UTF-8 (RFC 8259): no byte order mark, no `NaN` or infinity,
  no number with a fraction or an exponent whose binary64 value is infinite, no integer of more
  than 4300 digits (an integer is kept exactly, as
  [spec/zarrify.md §1.6](zarrify.md#16-json-documents) keeps it), no member twice in one object,
  no lone surrogate, and no nesting deeper than 256, [§1.6](zarrify.md#16-json-documents)'s
  bound. Its `zarr_format` is 3, written as an integer (no fraction, no exponent), and its
  `node_type` `group` or `array`; an array's
  document holds `shape`, `data_type`, `chunk_grid`, `chunk_key_encoding`, `codecs` and
  `fill_value`, its `shape` a list of natural numbers, its `data_type` a string or an object,
  its `chunk_grid` and `chunk_key_encoding` objects with a string `name` and, where present, a
  `configuration` object, a `regular` grid's `chunk_shape` one positive integer per axis, its
  `codecs` a non-empty list of objects with a string `name`, its `dimension_names`, where present, one string or null per
  axis, and its `attributes`, where present, an object. A document has no top-level member
  but Zarr v3's for its node type (a group's `zarr_format`, `node_type` and `attributes`, an
  array's also `dimension_names` and `storage_transformers`), not even an object whose
  `must_understand` is `false`, which zarr-python refuses, so that a reader that walks the whole
  archive can parse every node's document.
- `zarr.json` is a group. Every other key is a node's `zarr.json` whose parent node's `zarr.json`
  is a group of the layout, or a key below an array's `zarr.json`; no node is inside an array.
- Every reference entry's ranges are, as [spec/zarrify.md §1.1](zarrify.md#11-output-and-equivalence)
  compares them (checksums apart), those of a reference entry of the base, in the same form (one
  range, or a concatenation of the same ranges, as the base holds it): a layout copies the
  base's references and makes none, so it reads only bytes the base reads, under the base's
  pins and reader policy. Each is written as the base holds it, its checksums included,
  whatever checksums the plugin's value has. It is a chunk of a copy
  ([§4](#4-copying-an-array)) of a base array that holds it: the layout's array above it has the
  document [§4](#4-copying-an-array) gives that array for some `order`, attributes and dimension
  names aside, and each reference below that array is at the key [§4](#4-copying-an-array) gives
  it there, so that each chunk it copies decodes as the base's does, transposed. A copy MAY
  leave chunks out, which read as its fill value, and MAY hold bytes chunks of the plugin's
  own; a layout's array that holds no reference is the plugin's own, its chunks bytes. refkv
  does not decode a layout's bytes chunks: that they fit their array is the plugin's to ensure.
- No document holds the top-level member `consolidated_metadata`, the top-level attribute
  `refkv_zarrified`, `refkv_layout` or `refkv_provenance`, or, in `zarr_conventions` (a list of
  objects), a CMO with the UUID or the `name` of a refkv convention: a profile's, a dataset
  layout's, provenance's or this one. A CMO's UUID is read, ASCII lower-cased, with at most one
  `urn:uuid:` or `uuid:` prefix and one pair of enclosing braces (in either order) aside, and its
  hyphens aside, as the 32 hexadecimal digits of a UUID, both to tell refkv's and the plugin's. A
  CMO that has a `name` has a string there. A CMO with the plugin's
  convention's UUID, or its key as `name`, equals the one the plugin's description makes. Where a document holds the plugin's key, it is an
  object without `version`.

The layout's node is `refkv_layouts/<path>`. The output is then the built-in output the call asked
for, its entries unchanged but the root's, with:

1. `refkv_layouts/zarr.json`, [§1](#1-declaration)'s group;
2. the layout's entries under `refkv_layouts/<path>/`, its node's document declared as
   [§1](#1-declaration) says: `zarr_conventions` this CMO, the plugin's, then any other the
   plugin wrote, and the plugin's key holding `version` with the members the plugin wrote;
3. the root's `refkv_zarrified.layouts` ([conventions §2](conventions.md#2-attributes)), after
   `source`, naming each plugin layout, `"<name>": "refkv_layouts/<path>"`, beside `"dataset"`
   when a dataset layout is written, in code point order of the names.

The references a layout copies are not counted against
[the references' budget](zarrify.md#12-input), and they come after the base's sources are
numbered: a reference under `refkv_layouts` names a source the base already uses. The layout's
documents, and `refkv_layouts/zarr.json`, are read when the archive opens; its other bytes entries
are compressed.

A base that holds a key under `refkv_layouts/` refuses the layout request; the bases of file inputs
that present the source's own names at the root (HDF5, NumPy `.npz` and safetensors) leave out a
source object of that name, with the reason `reserved`, so that none does.

## 4. Copying an array

An array of the base is copied by these rules, so that two producers copy one alike. The copy's
document is the base's, its `attributes` those the plugin gives (`{}` by default: the base's are
in [conventions §6](conventions.md#6-source-metadata-as-json)'s encodings, which the base's
declarations decode), its `dimension_names` those the plugin gives (each a string or null), in
the copy's axis order, or
else the base's; its chunks are the base's entries, as they are.

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

## 5. Names, identity and versions

- **Names.** A layout's name is `<distribution>.<layout>`: segments of `[a-z0-9-]+` joined by `.`,
  the first the normalized name (PEP 503) of the Python distribution that provides it. A name
  whose first segment starts with `refkv` is refkv's. A layout's path is one key segment of
  `[a-z0-9_-]+`, not starting with `__`, short enough that `refkv_layouts/<path>/zarr.json` is
  at most 65535 bytes.
- **Keys.** A layout's key, the `name` of its convention's CMO, is its name with `.` and `-` as
  `_`; it is never `zarr_conventions`, `refkv_zarrified`, `refkv_layout` or `refkv_provenance`,
  and a name whose key would be one is refused.
- **One of each.** Two plugin layouts of one call never share a name, a path, a key or a
  convention's UUID; such a call is refused before the input is read.
- **Identity.** A layout is its convention's UUID, written in RFC 9562's form, lower-case
  hexadecimal digits with hyphens; names and keys are for people. It is never the UUID of a
  refkv convention, and a plugin whose convention has one is refused when it is loaded. A reader that
  finds a key whose UUID it does not know ignores it.
- **Versions.** A plugin's convention says what its version numbers mean and increases its
  version whenever its layout changes for some input; its `schema_url` SHOULD differ from one
  version to the next. Until refkv 1.0 every built-in convention is at version 0, which
  promises nothing, so a refkv release can change a base, and the layouts written from it, with
  no version to tell them apart.
- **Determinism.** A layout is a function of the base and its canonical options alone, and its
  convention fixes the exact bytes of every bytes entry it writes. Its options MUST NOT hold
  local paths, user or host names, or anything else private: they are written in the output.
- **Equivalence.** Two outputs of one input are equivalent
  ([spec/zarrify.md §1.1](zarrify.md#11-output-and-equivalence)) only if they hold the same
  plugin layouts (the same names, paths, UUIDs, versions and canonical options), and then entry
  for entry, a layout's subtree included.

## 6. Readers

A reader of a base convention skips the root's child whose `zarr.json` declares this convention,
as it skips a dataset layout. A reader of a plugin layout opens the path the root's `layouts`
names, not the root, and knows the layout by its node's CMOs. A copied array keeps its codecs:
it reads with whatever codecs the base's array needs.

<a id="table-layout"></a>**The layouts' entries** ([conventions §11.5](conventions.md#115-layout-tables)
says how to read a table of entries):

| key | type | structure | description |
|---|---|---|---|
| `refkv_layouts/zarr.json` | group | attributes: `zarr_conventions` and [`LayoutsGroup`](#type-layoutsgroup) | the layouts' group ([§1](#1-declaration)) |
| `refkv_layouts/<path>/zarr.json` | group | attributes: `zarr_conventions`, the plugin's key, [`LayoutProperty`](#type-layoutproperty), and the plugin convention's members | a layout's node ([§1](#1-declaration), [§3](#3-the-entries)) |
| `refkv_layouts/<path>/**` | subtree | the plugin's convention | the layout's other entries ([§3](#3-the-entries)) |
