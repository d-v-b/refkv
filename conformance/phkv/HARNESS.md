# phkv conformance harness interface

For format version 0 of the standalone profile of the phkv binding
([spec/bindings/phkv.md](../../spec/bindings/phkv.md), draft). The suite is the vzip suite's
([../vzip/HARNESS.md](../vzip/HARNESS.md)) for a second binding, with the same two commands,
the same queries and results, and the same checks. This document gives what differs. Read the
vzip one first; everything it says about `read` (the queries, the results, the error classes, the
malformed queries files), about `write` (the strict description rules, the nulls, the hex) and
about the reader policy applies here, with these changes.

**What the suite tests.** The hybrid profile ([spec §21](../../spec/bindings/phkv.md#21-hybrid-profile))
has no cases: the plan in [design/plans/split-bindings.md](../../design/plans/split-bindings.md) drops it.
The suite checks the container (the superblock, the section directory, the records, the page index),
the perfect hash and slot table (every key of every store is looked up, through every implementation),
bytes values (stored, inline and out of line, DEFLATE), references (every kind of source: `url`,
`key`, `data`), and the operations ([spec §22.2](../../spec/bindings/phkv.md#222-operations)).

## Differences from the vzip interface

- **`read` opens a phkv store**, a local file. Opening a file that is not one, or whose superblock,
  directory or sections are malformed, is an archive error (the suite's `crafted/` stores).
- **No reserved names.** A phkv store reserves none ([spec §16](../../spec/bindings/phkv.md#16-what-the-phkv-binding-defines)),
  so keys such as `__vz__/hdr`, `__vz__/sources` and `__vz__/index` are ordinary keys: `classify`,
  `get` and `list` see them, a `key` source may name them, and a description may have them.
- **`get_raw`** ([spec §22.2](../../spec/bindings/phkv.md#222-operations)) returns a bytes value's
  *stored* bytes, not decoded, and a reference's payload. For an entry with `compress: true` the
  stored bytes are the value itself (a writer may store a value as it is) or a raw DEFLATE
  stream of it (RFC 1951, ending exactly at the end of the stored bytes); the suite accepts either.
  For one without, they are the value. There is no mirror: a reference's record is its payload.
- **Descriptions** have no `mirror` and no `pinned` (they are ignored if given), and two members of
  their own, both hints that a writer follows as far as it can:
  - `page_size`: about how many bytes of records each page of the page index holds
    ([spec §19.4](../../spec/bindings/phkv.md#194-page-index)); an integer of at least 1, or invalid.
    It is not a request for a page index: every phkv store has one.
  - `inline_max`: a value whose stored bytes are at most this long is kept in its record
    ([spec §19.2](../../spec/bindings/phkv.md#192-bytes-records)); an integer of at least 0, or invalid.
  A writer that has fixed pages and inlining ignores both (the JavaScript one does). `compress: true`
  means a DEFLATE value ([spec §17.5](../../spec/bindings/phkv.md#175-codecs), codec 1).
- **Reference payloads** may be as long as 2^24 bytes ([spec §19.3](../../spec/bindings/phkv.md#193-reference-records)),
  not vzip's 65,519 (the `large_payloads` description).
- **The writer's own sections** (the source table, the page index, the perfect hash) may be stored
  with any codec ([spec §17.5](../../spec/bindings/phkv.md#175-codecs)): a reader reads all three.
  The reference writer's CLI takes `--section-codec none|deflate|zstd` before `write`, which the runs of
  `just conformance-phkv` use to test readers with fewer codecs and with Zstandard apart.

## What the runner checks

As the vzip runner, in three parts (`run.py`):

1. **read**: the stores of the reference writer, and the crafted ones: every query result against the
   oracle ([phkv_model.py](phkv_model.py), which is the vzip model's with the differences above), the
   request accounting of the HTTP profile, the malformed queries files, and the stores that must fail to
   open.
2. **write**: every description written by every implementation; the store validated structurally by
   [phkv_validate.py](phkv_validate.py), which has its own parser (superblock and directory with their
   CRC-32Cs, sections, the records in key order, the page index, the source table) and shares no code with
   an implementation; and read back by every implementation (a cross-read matrix).
3. **reject**: every invalid description must be rejected, without leaving a file.

**`file:` sources.** The oracle's `file:` URLs are POSIX's, and some readers resolve only HTTP: the
descriptions that need a file are `files_*`, the others are self-contained (`key` and `data` sources,
and the HTTP sources the runner serves). `--no-file-sources NAME` exempts one implementation from the
`files_*` descriptions, and `--no-files` every implementation (on Windows).

**Known gaps.** `--known-gaps FILE` is a JSON object of `fnmatch` patterns of failing case ids, each with
its reason ([known_gaps.json](known_gaps.json)). A case that fails and matches one does not fail the run.
A pattern that matches nothing that fails does: the gap is closed, so remove it. The case ids are
`read:<impl>:<vector>`, `cross:<writer>-><reader>:<description>`, `write:<impl>:<description>` and
`reject:<impl>:<case>`.

## Implementations

| name | what | notes |
|---|---|---|
| `ref` | `python -m refkv.phkv.cli` (the Rust core's reader and writer) | the oracle's reference |
| `js` | [impls/js/cli.ts](impls/js/cli.ts), over `js/src/phkv.ts` | an independent container reader and writer; reads only HTTP sources |

`just conformance-phkv` runs the suite.
