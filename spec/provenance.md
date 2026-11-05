# refkv's provenance convention

A convention that records which program wrote a Zarr hierarchy, its version, and
optionally when. It is separate from the format conventions
([spec/conventions.md](conventions.md)) on purpose. Their property holds nothing
about the program that made the hierarchy, so that two conforming producers
make equivalent hierarchies from the same source
([conventions §2](conventions.md#2-attributes)). This convention holds exactly
that, and is therefore not part of a zarrifier's output
([§3](#3-not-part-of-the-output)).

What it is for: finding the hierarchies a given release wrote, for example
after a fix to a profile changes its output, and reporting which program wrote
a hierarchy that reads wrongly.

Convention version: 0 (draft) · UUID: `7ffd0ef7-df19-408c-a4df-c91623b4c3db` ·
Schema: [schema.json](provenance/schema.json)

Added 2026-11-04.

The key words MUST, MUST NOT, SHOULD, SHOULD NOT and MAY are to be interpreted
as described in RFC 2119.

## 1. Declaration

The convention follows the [Zarr conventions
framework](https://github.com/zarr-conventions/zarr-conventions-spec) and
nests its property under one key, `refkv_provenance`, which is also its
`name`. At version `N` it is declared by the Convention Metadata Object (CMO)

```json
{
  "uuid": "7ffd0ef7-df19-408c-a4df-c91623b4c3db",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/tags/provenance-v<N>/spec/provenance/schema.json",
  "spec_url": "https://github.com/d-v-b/refkv/blob/provenance-v<N>/spec/provenance.md",
  "name": "refkv_provenance",
  "description": "The program that wrote a Zarr hierarchy, and its version"
}
```

with no other members. Versions follow
[conventions §1](conventions.md#1-conventions), tagged `provenance-v<N>`: at
version 0, which has no tag, the URLs name the development branch,
`.../refs/heads/main/spec/provenance/schema.json` and
`.../blob/main/spec/provenance.md`.

## 2. The property

Only the root node (the document `zarr.json`) declares the convention. Its
attributes then hold the CMO in `zarr_conventions`, beside any other
convention's, and the member `refkv_provenance`, a
<a id="type-provenance"></a>**`Provenance`**:

| member | type | presence | value |
|---|---|---|---|
| `producer` | [`Producer`](#type-producer) | required | the program that wrote the root |
| `operation` | string | optional | what the program did to make the hierarchy: `"zarrify"` (one input, [spec/zarrify.md](zarrify.md)), `"compose"` (a combination of stores), `"virtualizarr"` (a VirtualiZarr dataset), or another value the program documents |
| `created` | string | optional | when, in UTC, as RFC 3339 with a `Z` offset and whole seconds: `2026-11-04T09:30:00Z` |
| `plugins` | array of [`Plugin`](#type-plugin) | optional | the plugins that wrote the hierarchy's plugin layouts ([spec/layout.md](layout.md)), one each, in the order of the root's `layouts` |

<a id="type-producer"></a>**`Producer`**:

| member | type | presence | value |
|---|---|---|---|
| `name` | string | required | the program's name: `"refkv"` for refkv |
| `version` | string | required | the program's version as it reports it: for refkv's Python package, `refkv.__version__` (`"0.2.0"`) |
| `implementation` | string | optional | which of the program's implementations wrote it, when it has several: for refkv, `"python"`, `"javascript"` or `"rust"` |

<a id="type-plugin"></a>**`Plugin`**:

| member | type | presence | value |
|---|---|---|---|
| `layout` | string | required | the layout's name |
| `distribution` | string | required | the Python distribution that provides the plugin, its name normalized as PEP 503 says |
| `version` | string | required | that distribution's version |

There are no other members, in any of these objects. (Changed 2026-11-06: `plugins`, a plugin
being no program that wrote the root, which `Producer` names.)

Example: the root of a TIFF zarrified by refkv 0.2.0's Python package.

```json
{
  "zarr_conventions": [
    {"uuid": "48e9ac4e-1156-4a62-955e-20467d9c2700", "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/zarrify/tiff/schema.json", "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/zarrify/tiff.md", "name": "refkv_zarrified", "description": "The Zarr layout of a TIFF source zarrified by refkv, and the source's metadata"},
    {"uuid": "7ffd0ef7-df19-408c-a4df-c91623b4c3db", "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/provenance/schema.json", "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/provenance.md", "name": "refkv_provenance", "description": "The program that wrote a Zarr hierarchy, and its version"}
  ],
  "refkv_zarrified": {"profile": "tiff", "version": 0, "source": {"url": "https://example.org/plane.tif"}},
  "refkv_provenance": {
    "producer": {"name": "refkv", "version": "0.2.0", "implementation": "python"},
    "operation": "zarrify"
  }
}
```

## 3. Not part of the output

The member `refkv_provenance` and its CMO are not part of a zarrifier's output
([spec/zarrify.md §1.1](zarrify.md#11-output-and-equivalence)): two outputs
are equivalent if they are equivalent once both are removed from the root, as
an `etag` pin and the ranges' checksums are left out of the comparison there.
A conformance check that compares outputs (the Python, browser and Rust
zarrifiers against each other, or a profile's fixtures) removes them first.

## 4. Rules

1. **Opt-in.** A producer MAY declare the convention, and SHOULD do so only
   when its user asks. Left out, a hierarchy's documents are the same
   whichever producer, release or machine wrote them, and two outputs compare
   with nothing stripped. refkv declares it only when asked, through an option
   such as `provenance=True`.
2. **Root only.** No node other than the root declares it. A program that
   writes a new root, such as a combination of stores with its own header,
   writes its own `refkv_provenance` or none; it MUST NOT copy an operand's,
   which describes another hierarchy.
3. **Nothing private.** The property holds only the members of
   [§2](#2-the-property). A producer MUST NOT put in it what identifies the
   machine or the user that ran it, or the inputs beyond what the format's
   convention already records: no local paths, user or host names, options,
   environment variables or credentials. Archives are published, and few
   users read their attributes before sharing them.
4. **Informational.** A reader MUST NOT change how it reads a hierarchy's
   data by the property. It MAY use it to warn about a hierarchy written by a
   version with a known defect, naming the defect.
5. **Beside a format's convention.** A format's convention admits other
   conventions' members ([conventions §2](conventions.md#2-attributes)), and
   its JSON Schema accepts them unchecked; this convention's
   [schema](provenance/schema.json) checks `refkv_provenance`.

## 5. Versions and the format conventions

A producer's version does not stand in for a format convention's: a reader
that relies on a layout checks `refkv_zarrified.version`
([conventions §1](conventions.md#1-conventions)), which changes when the
layout does, and two releases of refkv can write the same layout. The
producer's version narrows a search ("every hierarchy refkv 0.2.x wrote of an
ND2 file") that the convention's version alone cannot, since at version 0 a
convention's layout may change without its version changing.

## 6. Open questions

- **Where it lives.** A binding could record the writer instead, as a format
  entry under its reserved prefix
  ([spec/bindings/vzip.md §11.4](bindings/vzip.md#114-hidden-entries)): a ZIP
  tool and a Zarr reader would not see it, and it would describe the archive
  rather than the hierarchy. A Zarr attribute survives a copy of the
  hierarchy into another store, which the binding's entry does not.
- **Operands.** A combination could keep its operands' `Provenance` objects,
  in a member such as `operands`, to say which releases wrote its parts.
- **Dependencies.** Whether to record the versions of other libraries, for
  a producer whose output depends on them.
