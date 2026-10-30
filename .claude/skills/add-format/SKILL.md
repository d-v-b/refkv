---
name: add-format
description: Use when adding, extending or fixing a file-format profile in refkv (making refkv zarrify a new format, supporting a new codec, track or variant of an existing one), or when asked whether a format can be zarrified ("can we do X?"). Gives the procedure and the rules that earlier formats learned the hard way, and points into docs/adding-a-format.md.
---

# Adding a file format to refkv

The full guide is [docs/adding-a-format.md](../../../docs/adding-a-format.md).
Read the section for each step before doing it. This file is only the
procedure and the rules that are easy to miss; the guide is the source of
truth.

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
5. **Implement.** See [§5](../../../docs/adding-a-format.md#5-implement-it).
   - The registry file, then `just profiles`.
   - The Rust module, its projection and its source model.
   - The hosts, only for a file read as a store.
   - A codec, only if none fits.
6. **Fixtures, tests and verification.** See
   [§6](../../../docs/adding-a-format.md#6-fixtures-tests-and-verification).
7. **Docs and the demo.** See [§7](../../../docs/adding-a-format.md#7-docs-and-the-demo).
8. **Run the commands** in [§8](../../../docs/adding-a-format.md#8-before-opening-the-pull-request), then open the PR.

## Rules that are easy to miss

- **Real files over the standard.** Headers and tags lie, and writers deviate
  from standards. Derive facts from the coded stream. Follow de facto
  readers where they differ from the standard, and record that with a dated
  change note.
- **Pick the oracle.** Reference readers disagree with each other: name the
  one you compare against, and the cases it gets wrong. Use third-party
  readers to check the output, never to produce it.
- **Record, don't reject.** A track, level or object the profile can't
  present is listed with a reason. Rejecting it loses science.
- **Never panic.** The wasm build must reject malformed input. Read with
  `.get()` and checked arithmetic, and give every loop and read a stated
  bound.
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
- **Stale builds give false results.** Rebuild after `just profiles` or any
  Rust change. Run `just js::wasm` after a rebase, and in a fresh worktree
  also `npm ci` in `js/`.
- **Pull requests** go against `main`, never stacked. Name the fork PR and
  the merge order in the description.
