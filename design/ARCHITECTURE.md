# refkv architecture: review and design

Status: built, reviewed against the code on 2026-11-03. Every profile of the
registry now runs through the intermediate representation (IR) proposed here, in
the Rust core [rust/refkv-ir](../rust/refkv-ir/).

**Normative text.** This document adds no rules. The IR mirror is specified by
[spec/conventions.md §8](../spec/conventions.md#8-the-ir-mirror), each format's
source model by its document in [spec/zarrify/](../spec/zarrify/), and the rest
by [spec/zarrify.md](../spec/zarrify.md) and [spec/refkv.md](../spec/refkv.md).
Where this document and those differ, they are right.

**How to read it.** It began as a proposal of 2026-10-08, when the project was
called vzip: an adversarial design review of the whole (five independent reviews:
the data model, the archive format, resource bounds, the two implementations, and
remote I/O), and one structural change that addresses most of what they found.
[§1](#1-verdict), [§2](#2-what-the-reviews-found), [§4](#4-do-now-independent-of-the-ir)
and [§5](#5-how-to-test-the-proposal) are that review as it stood then.
[§3](#3-proposal-an-intermediate-representation) and
[§6](#6-open-questions) describe the design as built, and say where it departs
from the proposal. [IR_NOTES.md](IR_NOTES.md) is the log of how it got there.

## 1. Verdict

vzip worked: eleven formats zarrified into Zarr v3 (the
[registry](../spec/zarrify/profiles/) now has 25 profiles and one dataset layout),
two implementations agreed on 667 fixtures and the public corpora, and independent
readers confirmed the pixels. Four rounds of per-format review made every format lossless and
bounded on the inputs reviewers could think of.

But the per-format rounds kept finding the same kinds of problem in each
format in turn, and each was fixed locally. The five architectural reviews
agree on why: vzip has no shared model of the source, no shared model of
resources, and no shared model of I/O. Each profile parses, translates,
budgets, spills, escapes and reads in its own way, twice. That is where the
recurring bugs come from, and it is what makes a twelfth format expensive.

## 2. What the reviews found

### 2.1 Two incompatible ideas of what the record is

Six formats (TIFF, NDPI, ND2, DICOM, NIfTI, IMS) treat translated JSON as the
record of the source. Making JSON lossless forced a growing vocabulary of
about twenty tagged forms (`{"latin1"}`, `{"utf16"}`, `{"int"}`,
`{"float"}`, `{"bits"}`, `[name, value]` pairs, `\0`+base64 names, `named`,
`Gathered`/`Structures`, hex fills, base64 data forms, ...), several of which
collide with user keys in copied attributes. Even so, some information is
still lost: ND2's LV record types (so LV chunks cannot be rebuilt, as the ND2
convention [§5.1](../spec/zarrify/nd2.md#51-decoded-chunks) admits), ND2 XML attributes and text, DICOM number spelling, IMS object
comments, and every colliding name under the shared [conventions §6](../spec/conventions.md#6-source-metadata-as-json) rule.

SAFE and CZI took the other view: the source bytes are the record, kept by
reference, and JSON is a derived convenience. That view is simpler and loses
nothing.

Consequences: reconstruction depends on eleven hand-written inverses that no
one has written down as code and no test exercises (only SAFE and CZI rebuild
files in conformance); "layout" means three different things across
conventions; there are about twelve budget numbers, five spill-to-text forms,
two path roots, four escaping schemes, and one reserved name with two meanings
(`vzip_source/objects`).

### 2.2 No resource model

About 95 per-site constants bound intermediate quantities. Nothing bounds
total output, bytes read, requests, wall time or browser memory. The class
that matters most is reproduced in three formats: references multiply when a
logical index maps onto a shared physical structure (an OME-TIFF whose 3000
planes name one IFD gives 12 M references from 176 KB and exhausts the
browser; HDF5 hard-linked time points; a server that lies about the size).
Each instance was patched separately ("share repeated tables", "scale the IMS
object cap") and the class remains open.

### 2.3 No I/O model

Each profile walks its format one round trip at a time. Measured: an ND2
frame-header prefetch that misses on every real frame (names are padded to
4072 bytes), DICOM fragment walks of 912 serial 64 KiB reads over one 57 MB
run, SAFE tile-part chains of 1849 12-byte reads per band, an IDR plate of
10 k sequential document reads. Python and the browser differ in
concurrency, retry, caching and request counts; the browser has no retry, no
timeout and unbounded fan-out, and downloads a whole object per block when a
server ignores `Range`.

### 2.4 The archive format

- Readers resolve any scheme an archive names, with ambient credentials: a
  remote archive can read `file:///etc/hosts` or private buckets, and one
  chunk can fan out to about 10,000 requests. [SPEC §8](../spec/refkv.md#8-security-considerations) says SHOULD; no reader
  does.
- No staleness detection: the profiles forbid pins, so a changed
  uncompressed source silently yields wrong pixels.
- About 115 bytes of ZIP framing per chunk (40x kerchunk Parquet); a store
  archive repeats a full URL per chunk and decodes it all at open; payloads
  are mirrored into entry bodies (up to 49% of an archive); the 65,519-byte
  payload cap rejects real padded ND2 frames.
- Little interoperability without vzip code: `zlib` and the imagecodecs
  codecs are not registered, rectilinear grids are experimental, and the
  browser path needs the service worker.

### 2.5 Two implementations that no longer prove the spec

The TypeScript code is a function-by-function port of the Python code. Spec-
only implementation rounds stopped at revision 4; nine profiles have never
been built from the spec alone. So agreement between the twins shows a
faithful port, not a complete spec, while every fix costs two edits.
`compare.py` cannot see the spec's own exact-integer rule (it compares
numbers as binary64), changed reference bodies, extra hidden entries, or
duplicate members. There is no CI. The spec has absorbed ECMAScript artifacts:
budgets defined as `JSON.stringify` output length, −0 allowed to become 0,
member order declared insignificant because JavaScript reorders keys.

### 2.6 Versioning is off

Every convention still says version 1 after about thirty breaking changes;
the `virtualize-*` tags do not exist, so every `schema_url` and `spec_url` in
every archive is a 404. An archive from revision 14 and one from revision 18
are indistinguishable.

## 3. Proposal: an intermediate representation

This was built. The text below describes the core as it is, and says where that
differs from what was proposed.

### 3.1 Two stages instead of one

Before the IR, each profile went from bytes to Zarr in one step. The core splits it:

1. A **parser** reads the source into an IR: a typed tree of the format's own
   elements, each with its extent in the source.
2. **Projections** turn an IR into a Zarr hierarchy. A projection never reads
   the source.

The same TIFF IR can then feed an OME-NGFF image, a raw per-IFD view, a
GeoZarr layout or a sharded layout. Shared machinery (the mirror, the reference
counts, spill, escaping, output bounds) lives in the core, once.

### 3.2 The IR

A tree of elements, held in memory as columns
([rust/refkv-ir/src/ir.rs](../rust/refkv-ir/src/ir.rs)): dense columns for every
element (kind, parent, name, name index, type, space, extent), and sparse side
tables for what few elements carry (decoded values, forms, alias targets, runs).
The proposal was a stream of records, so that a million-frame source would never
sit in memory. Runs do that job instead. Names come from the format's own
specification: TIFF tag numbers and IFD paths, DICOM `(gggg,eeee)` and item
indexes, HDF5 object paths, ND2 chunk names, CZI segment ids, JPEG 2000
marker names, ZIP entry names.

| Kind | Meaning | Carries |
|---|---|---|
| `struct` | A container: IFD, group, segment, item, box | Children |
| `value` | A field the layout or a reader needs | Its extent, its declared type, its decoded value (small) |
| `data` | A decodable unit: tile, strip, frame, subblock, codestream tile | Geometry (shape, dtype), codec, and a recipe: source ranges, literals and shared data sources |
| `derived` | Content that exists only after a transform: an inflated LV chunk, a deflated XML entry | The transform, its input extent, and the derived bytes' own sub-tree |
| `alias` | A second name for an element already described: two planes naming one IFD, a hard link | A reference to the first |
| `gap` | Bytes no element claims: padding, dead space, unparsed regions | Its extent |
| `object` | An object of a store, a source of its own (only in a store's IR) | Its extent, `(0, size)` of that object, which its descendants' extents index |

**Runs.** A run is one subtree that stands for `count` identically shaped subtrees:
member `j` is member 0 with every extent shifted by `j × stride`, and its root's name
index increased by `j`. A parser emits subtrees one by one and calls `Ir::fold` after
each, which folds the new one into its previous sibling when the two have the same
shape at a constant stride, so a run costs one subtree whatever its count. Runs do
not nest. The mirror's canonical table
([conventions §8.8](../spec/conventions.md#88-canonical-form)) expands them, then
folds rows into column runs of its own.

**Spaces.** An element's extent is in the source (space 0) or, under a `derived`
element `d`, in `d`'s derived bytes (space `d`).

**Stores** ([conventions §8.9](../spec/conventions.md#89-stores)). A store's IR has
many sources, one per `object`, under a tree of directories that is the store's
listing. An object is one of three things:

- **held**: it has children, and the mirror holds its bytes (`refkv_source/ir/held`),
  so the table alone rebuilds it, as for a node's document;
- **described in place**: it has children, and its bytes stay at its own URL, as for
  a SAFE band file or a safetensors shard;
- **referenced**: it has no children, and is one leaf, its whole source.

The checks run on **virtual offsets** ([rust/refkv-ir/src/check.rs](../rust/refkv-ir/src/check.rs)):
the objects end to end, in id order, each byte of object `o` at `x` being at
`base(o) + x`.

The framing repairs vzip already does (JPEG tables and restart segments,
J2K headers and empty packets, padded rows) are recipes of `data` elements:
they belong to the parse, not the projection.

### 3.3 Invariants

- **Coverage.** The extents of the leaves (`value`, `data`, `derived` and `gap`
  elements with a length, in space 0) partition `[0, size)` of the source, or of
  each object a store's IR describes. Checked by one shared function on every IR.
- **Injectivity.** No byte is claimed twice. Sharing is explicit, as `alias`.
- **Bounded parse.** The IR's rows and elements are charged as the parser emits
  them, and its requests and bytes as the planner makes them, against separate
  limits; see [§3.5](#35-resources-and-io).

These invariants give, for every format at once:

- **A reconstruction contract.** Concatenate the leaves in extent order: the
  source rebuilds byte for byte with no format knowledge. "Complete
  reconstruction minus layout" stops being a per-format judgment; layout is
  in the IR as `gap` or `value` elements, kept by reference.
- **The end of the amplification class**, as proposed: a projection that emits
  references is checked against the IR, every pixel byte referenced at most once
  unless the IR says `alias`, and the projection decides what an alias becomes (one
  array, or a link), instead of silently multiplying. That check was not built, so
  the class is bounded rather than ended. `Refs`
  ([rust/refkv-ir/src/refs.rs](../rust/refkv-ir/src/refs.rs)) counts references, up
  to 2^22 + 4 × size, and exact repeats, up to 2^20: a repeat has the source ranges
  of an earlier reference. It does not consult aliases, and a
  reference that overlaps another without equaling it is not a repeat. The
  projections of ND2, SAFE, GRIB, N5, Zarr v2, OME-Zarr and Neuroglancer precomputed
  do not use `Refs`: they write their references to the output directly.

### 3.4 Projections

- **Image** (OME-NGFF 0.5): from `data` elements and the `value` elements
  that give geometry, scale and channels. Today's main output.
- **Geo** (GeoZarr): for SAFE.
- **Source mirror** ([conventions §8](../spec/conventions.md#8-the-ir-mirror)): one
  generic projection that writes the IR itself under `refkv_source`, as a table
  (`refkv_source/ir`) and a view (`refkv_source/tree`), with one vocabulary: `value`
  elements as JSON with one tag namespace that cannot collide with user keys
  (`{"$vz": kind, ...}`), a long value left in the source and pointed to, paths
  relative to `refkv_source`, and one escape rule. It replaced the bespoke translators
  under that node. A format's root still has source metadata of its own
  ([conventions §6](../spec/conventions.md#6-source-metadata-as-json)).
- Variants: [dataset layouts](../spec/conventions.md#9-dataset-layouts) and
  [virtual shards](../spec/zarrify.md#21-virtual-shards) exist. Alternate chunkings,
  and a raw view next to a stitched one, have not been built.

JSON in the mirror becomes a view of the IR, not the record: it need not be
lossless, because the record is the IR's extents. The tagged-form arms race
ends at "readable".

### 3.5 Resources and I/O

- **Limits.** The proposal was one budget per run, threaded through the reader and
  the output, in one table generated into both languages. As built there is no such
  budget or table: each limit is a constant where its quantity is counted, and passing
  one rejects the input. The core is one implementation, so they need no generating.

  | limit | at most | where |
  |---|---|---|
  | IR rows | 2^22 + size / 4 | `Budget.records`, [ir.rs](../rust/refkv-ir/src/ir.rs) |
  | IR elements, with runs expanded | 2^30 + size | `Budget.elements`, ir.rs |
  | a file's requests | 2^20 + size / 64 | [plan.rs](../rust/refkv-ir/src/plan.rs) |
  | a file's bytes read | 2^24 + 2 × size | plan.rs |
  | a store's requests | 2^20 + 4 × objects | [store.rs](../rust/refkv-ir/src/store.rs) |
  | a store's bytes read | 2^24 + 2 × the listed sizes' sum | store.rs |
  | a store's objects | 2^24 | `MAX_OBJECTS`, ir.rs |
  | references, and exact repeats | 2^22 + 4 × size, and 2^20 | [refs.rs](../rust/refkv-ir/src/refs.rs), for the projections that use it ([§3.3](#33-invariants)) |
  | the mirror's interned names | 2^24 bytes | `NAME_BYTES`, [mirror.rs](../rust/refkv-ir/src/mirror.rs) |
  | the view's documents | 2^24 bytes | `TOTAL`, mirror.rs |
  | a format's own loops and tables | by format | the `limits` of `rust/refkv-ir/schema/<p>.json` |

  A store IR's rows and elements grow with each object, by 4 and one per 4 of its
  bytes. `Budget` also has the fields `read` and `reads`, which nothing charges: the
  planners keep their own counts. Nothing bounds wall time or a run's output entries
  as such.
- One **read planner**, sans-IO ([plan.rs](../rust/refkv-ir/src/plan.rs)): a parser
  gives the ranges of each step, never single reads in loops, and the planner
  coalesces them with a gap cost model calibrated from the first requests, detects
  sequential walks and reads ahead, and counts its requests and bytes. Each host does
  the I/O ([planner.py](../python/src/refkv/ir/planner.py),
  [source.ts](../js/src/zarrify/ir/source.ts),
  [transport.rs](../rust/refkv-cli/src/transport.rs)): it limits concurrency (by
  default 8 in Python, 6 in browsers), and retries with backoff and `Retry-After`.
  The proposal's estimate of its effect: ND2 18 k rounds to about 560, DICOM 912 to
  about 15, SAFE L1C 1700 to 1–130, the IDR plate 10 k to about 160.
  [IR_NOTES §37](IR_NOTES.md#37-connections-the-cost-of-bytes-small-files) has
  measurements.

### 3.6 One core, and the spec

The IR made the twin question tractable. Parsers stay format-specific;
projections, limits, the planner and the mirror are shared code. The options
considered for delivering both Python and the browser were:

| Option | Cost | Notes |
|---|---|---|
| Keep two hand-written implementations | Ongoing: every change twice | What we had |
| Pyodide in a browser worker, running the Python core | Low; drops about 14 k lines of TypeScript | Python is stdlib-only and synchronous; worker XHR allows sync reads. Needs a cold-start measurement (6–10 MB runtime) |
| Rust core, WASM and PyO3 | High: a rewrite | Fastest; `conformance/impls/zarrify/rust` is a seed. **Chosen** |
| TypeScript core called from Python | Low | Python users then need Node |

The recommendation at the time, to measure Pyodide's cold start and make Python the
single implementation, is superseded. The single implementation is
[rust/refkv-ir](../rust/refkv-ir/), which has no I/O and builds natively and for
wasm32. Python reaches it through PyO3 ([rust/refkv-python](../rust/refkv-python/),
the module `refkv._core`), the browser through wasm32, and the CLI
([rust/refkv-cli](../rust/refkv-cli/)) directly, each host adding its I/O and its
archive writer ([IR_NOTES §31](IR_NOTES.md#31-what-moved-to-rust)). No Pyodide
measurement was recorded. With one implementation, the check that the spec is
complete rests on the frozen reference and the independent readers
([HARNESS.md](../conformance/zarrify/HARNESS.md)).

The spec reorganized along the same line: per format, a normative **source model**
(the IR's elements, with canonical names, in `spec/zarrify/<p>.md`); shared, the
**IR invariants** and the mirror
([conventions §8](../spec/conventions.md#8-the-ir-mirror)). The proposal's budget
table and read planner did not become spec.

## 4. Do now, independent of the IR

These were small and right under any architecture. The list is as of 2026-10-08;
its items are not tracked here:

1. **Reader security policy** (SPEC): a source whose scheme differs from the
   archive's is refused unless the application allows it; readers take a URL
   prefix allowlist; caps on sources and requests per value; bounded
   inflation of format entries.
2. **Staleness**: let profiles pin `size` (always known) and `etag` when
   visible; add an optional per-range `crc32c` that readers check after the
   fetch, which works across CORS.
3. **Conformance**: make [spec/zarrify.md §1.1](../spec/zarrify.md#11-output-and-equivalence) compare integer literals exactly (adopt
   `same()`); run SPEC's strict reader on every archive inside `compare.py`;
   fail the TypeScript reader when `JSON.parse` source text is unavailable;
   add CI (pytest, node tests, sharded fixture compare) and a nightly corpus
   and mutant run.
4. **Versioning**: write `version: 0` and the revision until release; at
   release, tag and bump on every breaking change; pin source sizes.
5. **Bugs found on the way**: the ND2 frame-header prefetch misses on every
   real frame (read `min(name_length, 64)` or prefetch 16 + 4096); the
   browser ND2 path reads headers one at a time; Python reads multi-block
   spans one block at a time; the Python LRU is shared across threads
   without a lock.
6. **Fixtures**: pin timestamps (h5py `track_times=False`, gzip `mtime=0`),
   add generators for the two hand-committed fixtures, pin corpus URLs to
   commits and record their hashes.
7. **Rebase** the work onto `origin/main`, which already has concurrent store
   document reads (#37). Done: merged, the reads ahead going through the
   store's per-thread kept-alive connections, which each reading thread
   closes as it ends.
8. **Registry**: register `zlib` and the imagecodecs codecs, or prefer core
   codecs where the source allows.

## 5. How to test the proposal

This was done, and is recorded in [IR_NOTES.md](IR_NOTES.md). Before touching nine
formats, prototype on two:

- **TIFF** (simplest): parser to IR; the image projection and the mirror.
- **CZI or ND2** (hardest: derived content, sparse indexes, shared
  structures).

Success means: the image projection reproduces today's outputs (equivalent by
[spec/zarrify.md §1.1](../spec/zarrify.md#11-output-and-equivalence)); the mirror projection rebuilds every corpus file byte for byte from
the IR's extents with no format code; the amplification probes (shared IFDs,
hard links) are refused or aliased by the shared check, not by format code;
and the IR plus projections are smaller than today's profile.

## 6. Open questions

All four are decided.

1. Is the IR normative (part of each convention, versioned) or an implementation
   detail with only the projections normative? **Normative as stored.** The mirror
   and each format's source model are spec
   ([conventions §8](../spec/conventions.md#8-the-ir-mirror) and
   [§11.3](../spec/conventions.md#113-the-ir-mirror)), and the mirror is canonical, so
   two producers write the same one
   ([§8.8](../spec/conventions.md#88-canonical-form)). The columns and runs a parser
   holds in memory are not.
2. Should the mirror projection be on by default, or opt-in? **Always on.** Every
   convention's `refkv_source` is its mirror, and the public `zarrify` has no option
   to leave it out.
3. Single core: measure Pyodide first, or decide on principle? **Rust**, without that
   measurement; see [§3.6](#36-one-core-and-the-spec).
4. Do the stores (N5, Zarr v2, OME-Zarr) fit the IR, or stay as they are? **They fit**,
   as store IRs of objects ([conventions §8.9](../spec/conventions.md#89-stores)),
   since revision 26 ([IR_NOTES §48](IR_NOTES.md#48-revision-26-stores-many-sources)).
   An object need not be referenced whole, as the question assumed: it may be held or
   described in place too ([§3.2](#32-the-ir)).
