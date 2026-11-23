---
name: add-format
description: Use when adding, extending or fixing a file-format profile in refkv (making refkv zarrify a new format, supporting a new codec, track or variant of an existing one), or when asked whether a format can be zarrified ("can we do X?"). Gives the procedure and the rules that earlier formats learned the hard way, and points into docs/adding-a-format.md.
---

# Adding a file format to refkv

The full guide is [docs/adding-a-format.md](../../../docs/adding-a-format.md).
Read the section for each step before doing it. This file is only the
procedure and the rules that are easy to miss; the guide is the source of
truth.

## The IR

A profile works in two stages. The parser reads the file into an **IR**, and a
projection builds the output from it. The IR describes the source, and the
projection decides the output. Read these before designing a parser:

- [conventions §8](../../../spec/conventions.md#8-the-ir-mirror), the IR mirror: the
  normative rules for the IR's elements, its table and its canonical form.
- [spec/stored-array.md](../../../spec/stored-array.md): how the IR describes each array
  the source stores, at the byte level. [§6](../../../spec/stored-array.md#6-in-the-ir)
  says when a parser builds the descriptions, and
  [§7](../../../spec/stored-array.md#7-the-formats) says each format specifies its stored
  arrays in its own `spec/zarrify/<p>/stored-arrays.md`.
- [design/ARCHITECTURE.md §3](../../../design/ARCHITECTURE.md#3-proposal-an-intermediate-representation):
  why the IR exists and what its invariants are.
- [docs/build.md](../../../docs/build.md): `refkv.build`, which builds outputs other than
  the profile's own from the IR. Anything the IR leaves out, no such output can have.

## If the question is "can we do X?"

Answer it: whether the format fits, what a profile would cover, and what it
would leave out. Don't write code until someone says go. Check the four
conditions in [§1](../../../docs/adding-a-format.md#1-check-that-the-format-fits). If no
independent reader exists to check the output against, say so, and stop at
feasibility.

## The steps

1. **Check the format fits.** See [§1](../../../docs/adding-a-format.md#1-check-that-the-format-fits).
2. **Research.** See [§2](../../../docs/adding-a-format.md#2-research-the-format-and-its-files).
   - Read the reference implementations' source.
   - Survey real public files over range reads, with scripts in a scratch
     directory, before designing anything.
   - For a large format, write `design/plans/<p>.md` first.
3. **Decide the layout.** See [§3](../../../docs/adding-a-format.md#3-decide-the-layout).
   Settle each decision there, with the spec section that governs it.
4. **Write `spec/zarrify/<p>.md`** and `spec/source_metadata/<p>.py`. See
   [§4](../../../docs/adding-a-format.md#4-write-the-document).
   - Write `spec/zarrify/<p>/stored-arrays.md`
     ([stored-array §7](../../../spec/stored-array.md#7-the-formats)): the structures that
     are its stored arrays, how each is keyed, what its declarations are, and
     what is not described yet.
   - Edit only the format's own files. Never add the format to a list of formats in a
     shared file, in prose or in code: state a rule that names no format, and let the
     registry or a test enumerate them. `just independence origin/main --strict` (a
     warning in `just preflight`) lists any file the branch changes outside the
     format's paths and the files shared on purpose; see
     [§8](../../../docs/adding-a-format.md#8-before-opening-the-pull-request).
5. **Implement.** See [§5](../../../docs/adding-a-format.md#5-implement-it).
   - The registry file, then `just profiles`.
   - The Rust module, its projection and its source model.
   - The parser builds a stored-array description of every array it reaches
     when its IR is finished ([stored-array §6](../../../spec/stored-array.md#6-in-the-ir)).
   - For a file read as a store, or a store input: the registry's `store_run`,
     `store_parser` and `store_project`. The hosts read the rest from the registry.
   - A codec, only if none fits. Every codec the profile writes, even another
     package's, needs a row in the codec names table of
     [conventions §3](../../../spec/conventions.md#3-arrays).
6. **Fixtures, tests and verification.** See
   [§6](../../../docs/adding-a-format.md#6-fixtures-tests-and-verification).
7. **Docs and the demo.** See [§7](../../../docs/adding-a-format.md#7-docs-and-the-demo).
8. **Run the commands** in [§8](../../../docs/adding-a-format.md#8-before-opening-the-pull-request), then open the PR.

## Rules that are easy to miss

- **A stored-array description holds the source, not the output.** No data
  types, byte order names, output chunking, shards, padding or bytes the source
  doesn't hold ([stored-array §1](../../../spec/stored-array.md#1-stored-arrays)).
  Those are the projection's decisions. A description borrows from Zarr's array
  model where that fits, but it isn't Zarr. An element's form is the older
  place for a unit's geometry, and it still names a data type
  ([conventions §8.5](../../../spec/conventions.md#85-forms-and-recipes)).
- **Fall back instead of guessing.** Where a description can't interpret something, keep
  it general: bytes, or where the source holds them
  ([§2.2](../../../spec/stored-array.md#22-places-and-parameters)), a codec under the
  source's own id and parameters ([§3.2](../../../spec/stored-array.md#32-the-list)), or
  a description with no grid ([§2.3](../../../spec/stored-array.md#23-the-grid)).
- **Describe every array the parser reaches,** whether or not the profile
  presents it ([stored-array §1](../../../spec/stored-array.md#1-stored-arrays)): an
  output built with `refkv.build` may present an array the profile leaves out.
  The descriptions are not in the IR mirror, which only has to rebuild the file's
  non-data bytes exactly ([conventions §8](../../../spec/conventions.md#8-the-ir-mirror)).

- **Real files over the standard.** Headers and tags lie, and writers deviate
  from standards. Derive facts from the coded stream. Follow de facto
  readers where they differ from the standard, and record that with a dated
  change note, a new file `spec/zarrify/changes/<p>/<date>-<slug>.md`, never a
  paragraph in the document.
- **Pick the oracle.** Reference readers disagree with each other: name the
  one you compare against, and the cases it gets wrong. Use third-party
  readers to check the output, never to produce it.
- **Record, don't reject.** A track, level or object the profile can't
  present is listed with a reason. Rejecting it loses science.
- **Fixtures a frozen reference no longer gives.** List them in
  `conformance/zarrify/revised/<p>.txt`, sorted, with the reason in its top comment.
- **Never panic.** The wasm build must reject malformed input. Read with
  `.get()` and checked arithmetic, and give every loop and read a stated
  bound.
- **Many small units: consider a virtual shard first.** Frames, strips or small
  tiles on a regular grid can be the inner chunks of the stock `sharding_indexed`
  codec: call `Out::shard(path, shape)` from the projection, and
  `rust/refkv-ir/src/shard.rs` writes the reference and its literal index. No new
  codec, nothing decoded, compressed units allowed, and partial reads stay exact.
  Only zarr-python makes fewer requests from it, so measure before promising speed.
  See [§3](../../../docs/adding-a-format.md#3-decide-the-layout).
- **Edge chunks.** Use a regular grid, padded with the shared zero data
  source. Reject a short chunk whose stream can't be padded. A rectilinear
  grid is the last resort.
- **Codecs.**
  - Reuse an existing codec when its semantics fit exactly.
  - Name a codec of refkv's own `refkv.<codec>`.
  - Every new codec or grid needs its own PR in the Neuroglancer fork
    (`~/dev/neuroglancer`, branch `vzip`). Do that in a separate session.
  - In the browser, refuse to show pixels it can't decode exactly.
- **Fixtures** must be byte-identical on macOS and Linux: pin the gzip OS
  byte, and use no platform zlib and no floating-point quantization.
- **Tests:** one test over the valid inputs, comparing every value, and one
  test per rejection, each checking its exact message. Test the projected
  chunk bytes, not only the parser's facts.
- **Verify** with `just verify-of <p>` against the independent reader, and
  with the real downstream consumer where there is one. CI doesn't run it.
- **Docs and spec:** every section reference is a link, never a bare "§n"
  (`just sections`). Write American spelling. Say "zarrify".
- **One statement, one place.** A code comment that cites a rule of the spec
  links to the rule's section and doesn't restate it: a restated rule goes
  stale when the spec changes.
- **A proposal's status.** A PR that lands a step of a proposal in
  `design/proposals/` updates that proposal's `Status (YYYY-MM-DD):` line and
  its date. `just sections` checks that the line is there, and can't tell
  whether it is still true.
- **Stale builds give false results.** Rebuild after `just profiles` or any
  Rust change. Run `just js::wasm` after a rebase, and in a fresh worktree
  also `npm ci` in `js/`.
- **Pull requests** follow the "Pull requests" section of `CLAUDE.md`: one
  that depends on an unmerged PR targets its branch. Name the fork PR and
  the merge order in the description.
