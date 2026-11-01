# Plan: vzip and phkv as separate packages

This plan separates the two bindings so that each can change without the
other: the ZIP binding (vzip) stays a simple, single, ordinary ZIP file, and
the perfect-hash binding (phkv), a draft, is free to grow what
[#148](https://github.com/d-v-b/refkv/issues/148) discusses (generations,
tombstones, fingerprints) without any of it reaching vzip. It is a design
note, not normative; the specifications state the decisions once they are
made.

It covers four things: a crate for the value type that both bindings share
([§2](#2-a-crate-for-part-1-refkv)), the hybrid profile
([§3](#3-the-hybrid-profile-drop-it)), the specification
([§4](#4-the-specification)) and the Python package, with what
[#141](https://github.com/d-v-b/refkv/pull/141) should do
([§5](#5-python-and-141)). [§6](#6-the-cli-js-and-ci) covers the rest, and
[§7](#7-order-of-work) gives the order of the work.

## 1. Where the two are coupled today

The Rust crates are already separate, and vzip barely knows phkv exists (one
doc comment in `rust/refkv-vzip/src/writer.rs`). The coupling runs the other
way, and through the parts that are not ZIP at all:

| layer | coupling |
|---|---|
| Rust: value type | `refkv-phkv` imports `refkv_vzip::pb::{Range, Source}`, the varint helpers, `refkv_vzip::uri` and `refkv_vzip::deflate`. These are Part 1's messages, source table and URI rules, which live in the vzip crate only because it came first. [#81](https://github.com/d-v-b/refkv/pull/81) adds a shared `refkv_vzip::sources::SourceTable`, which deepens this. |
| Rust: hybrid profile | `refkv_phkv::HybridWriter` (`rust/refkv-phkv/src/hybrid.rs`) wraps `refkv_vzip::Writer` and uses its `Layout`, extra field IDs and reserved names, by design ([phkv §21](../../spec/bindings/phkv.md#21-hybrid-profile)). |
| Rust: CLI | `vzip convert --format phkv|hybrid` (`rust/refkv-cli/src/output.rs`) writes both through `refkv-phkv`. |
| Specification | [spec/refkv.md](../../spec/refkv.md) lists phkv as Part 3 of one specification, and the three documents share one section numbering: phkv starts at [§16](../../spec/bindings/phkv.md#16-what-the-phkv-binding-defines), after vzip's §15. |
| Python | none on `main`. [#141](https://github.com/d-v-b/refkv/pull/141) adds `refkv.phkv`, `refkv.open(binding="phkv")`, `ReferenceStore.save_phkv` and a phkv reader and writer in `refkv._core`. |
| JS | none: the JS library has no phkv. |

## 2. A crate for Part 1: `refkv`

A new crate, `rust/refkv`, named `refkv`, holds what
[Part 1](../../spec/refkv.md#part-1-reference-values) defines and every
binding uses. Both binding crates depend on it, and neither depends on the
other.

The name is `refkv`, not `refkv-core`, because "the Rust core" already means
`rust/refkv-ir` throughout the documentation, and the Python extension module
is `refkv._core`. The crate is Part 1, and Part 1 is what the name `refkv`
stands for.

What moves out of `refkv-vzip`:

| item | from | why it is Part 1 |
|---|---|---|
| `Range`, `encode_concat`, `decode_concat` | `pb.rs` | the reference payloads ([§4.2](../../spec/refkv.md#42-range), [§4.3](../../spec/refkv.md#43-concat)) |
| `Source`, `SourceKind`, `is_strong_etag`, `encode_source_table`, `decode_source_table` | `pb.rs` | the source table and its pins ([§5](../../spec/refkv.md#5-source-table), [§5.1](../../spec/refkv.md#51-pins)) |
| `put_varint`, `get_varint`, `DecodeError` | `pb.rs` | the encoding ([§4.1](../../spec/refkv.md#41-encoding)) |
| all of `uri.rs` | `uri.rs` | URI references and `file:` rules ([§5](../../spec/refkv.md#5-source-table)) |
| `SourceTable` (from #81) | `sources.rs` | the writer's checks on a source table, which both bindings apply ([§7](../../spec/refkv.md#7-writing)) |

What stays in `refkv-vzip`: `Page`, `Pinned` and `CdIndex` (the page index,
[vzip §12](../../spec/bindings/vzip.md#12-page-index-optional)), the writer,
`deflate`, `MAX_PAYLOAD` (a ZIP extra field's limit), the extra field IDs and
the reserved `__vz__/` names. `refkv-vzip` re-exports nothing from `refkv`;
its users import `refkv` directly, so the dependency stays visible.

`refkv-phkv` calls `flate2` for its codec 1 instead of `refkv_vzip::deflate`.
It already depends on `flate2` with the same features.

`refkv-ir` does not change: it depends on neither binding.

The repository has no Cargo workspace (each crate has its own `Cargo.lock`),
so the new crate is a path dependency like the others. A workspace would make
the three crates one build, which is worth doing, but separately.

## 3. The hybrid profile: drop it

A hybrid store is a vzip archive whose hidden entry `__vz__/phkv` holds a phkv
container indexing the archive's own entries
([phkv §21](../../spec/bindings/phkv.md#21-hybrid-profile)). It is the one
place where phkv depends on vzip by design, and this plan recommends removing
it.

**What it costs.** It ties phkv's format to vzip's:

- bytes values cannot use codec 2 (Zstandard), since a ZIP entry can only be
  stored or deflated;
- every record mirrors a ZIP entry, a reference's body must equal its payload,
  and the store's `n` counts vzip's hidden entries;
- the superblock carries a `hybrid` flag, and finding it
  ([phkv §17.2](../../spec/bindings/phkv.md#172-finding-the-superblock)) has a
  second path that reads the ZIP end of central directory record;
- anything [#148](https://github.com/d-v-b/refkv/issues/148) adds to phkv
  (generations, tombstones) either does not apply to hybrid stores or makes a
  hybrid file a ZIP archive whose phkv view and ZIP view disagree.

**What it buys.** One file that ZIP tools read and that a phkv reader looks
keys up in faster than in a paged vzip archive. The benchmarks
(`rust/refkv-phkv/bench/RESULTS.md`) measure that gain as modest: at 10^5 to
10^6 keys over HTTP with 20 ms per request, a later lookup takes 1.04 to 1.48
requests with phkv against 1.28 to 1.87 with paged vzip, and a hybrid archive
read as phkv opens in 4 to 7 requests against paged vzip's 4. A user who
wants phkv's lookups can write a standalone phkv store beside the vzip archive
from the same `ReferenceStore`.

**What goes.** `rust/refkv-phkv/src/hybrid.rs`, `HybridWriter`,
`rust/refkv-phkv/tests/hybrid.rs`, `refkv-phkv`'s dependency on `refkv-vzip`,
`--format hybrid` in the CLI, the hybrid branch of
[phkv §17.2](../../spec/bindings/phkv.md#172-finding-the-superblock), the
superblock's `hybrid` flag, [phkv §16.2](../../spec/bindings/phkv.md#162-profiles)
and [phkv §21](../../spec/bindings/phkv.md#21-hybrid-profile), and the hybrid
rows of the benchmarks. Nothing in vzip changes: a vzip reader never knew
about `__vz__/phkv`. No archive needs to keep working.

**The alternative**, if the hybrid profile is worth keeping: `refkv-phkv`
depends on `refkv-vzip` one way, and the hybrid profile is defined as
write-once, outside anything phkv later adds for appending. That keeps vzip
unaffected, but makes the phkv package depend on the vzip package, and gives
every later phkv feature a "not in hybrid stores" clause.

## 4. The specification

**phkv becomes its own specification.** `spec/bindings/phkv.md` stops being
Part 3 and becomes "phkv: a perfect-hash binding of refkv", a document with
its own format version (it already states "Format version: 0
(provisional)"), its own revisions and its own section numbering from §1. It
cites Part 1 by link, as it does today, and says which refkv format version's
messages it uses. Its draft status does not change.

With the hybrid profile dropped, the sections renumber as:

| now | after |
|---|---|
| §16 What the phkv binding defines | §1 |
| §17 Container | §2 |
| §18 Perfect hash | §3 |
| §19 Records | §4 |
| §20 Properties | §5 |
| §21 Hybrid profile | removed |
| §22 Reading a phkv store | §6 |
| §23 Writing a phkv store | §7 |
| §24 Security considerations for phkv | §8 |
| Appendix C | Appendix A |

About 45 links point into phkv.md from outside it, nearly all in
`rust/refkv-phkv`. `spec/tools/sections.py check` (the `sections` CI job)
fails on every link to a heading that no longer exists, so the renumbering
cannot leave a dead one behind.

**refkv.md and vzip.md keep their numbering.** The refkv specification stays
Part 1 and Part 2, which [refkv §1.3](../../spec/refkv.md#13-versioning)
already versions together ("A format version covers Part 1 and the ZIP binding
together"). The changes to [spec/refkv.md](../../spec/refkv.md) are:

- [§1](../../spec/refkv.md#1-introduction) lists Parts 1 and 2 only;
- [§3.1](../../spec/refkv.md#31-what-a-binding-defines) stops pointing at
  Part 3 as a second binding of this specification;
- [§3.2](../../spec/refkv.md#32-other-backends-informative) names phkv as a
  binding specified elsewhere, as an example of another backend.

vzip.md does not mention phkv and needs no change.

## 5. Python and #141

**`refkv` keeps vzip.** The `refkv` distribution stays Part 1 plus the ZIP
binding: `ReferenceStore`, the shared read path (`refkv.reader`), the
zarrifiers and `refkv.vzip`. vzip is the binding every zarrify run writes and
`refkv.open` reads by default, so separating it from `refkv` would give every
user two packages to install. The Python code is already factored this way:
`refkv.pb`, `refkv.uri` and `refkv.reader` are Part 1, and `refkv.vzip` is the
binding.

**phkv becomes a distribution of its own, `refkv-phkv`** (import name
`refkv_phkv`), a second member of the uv workspace beside `python/`:

- `python-phkv/`, with its own `pyproject.toml`, built by maturin from a crate
  `rust/refkv-phkv-python` that binds `rust/refkv-phkv`, as
  `rust/refkv-python` binds `rust/refkv-ir`. It depends on `refkv`.
- It provides `refkv_phkv.open(url)`, `PhkvStore`, `PhkvWriter` and
  `refkv_phkv.save(store, path)`, for a `ReferenceStore`.
- `refkv.open(url, binding=...)` finds bindings through an entry-point group
  `refkv.bindings`, where `refkv` registers `"vzip"` and `refkv-phkv` registers
  `"phkv"`. With `refkv-phkv` installed, `refkv.open(url, binding="phkv")` and
  `refkv.zarr.open(url, binding="phkv")` work as #141 has them; without it,
  the error names the package to install.
- `refkv` makes public what a binding's writer needs from a `ReferenceStore`:
  `mapping` (public already), and the source check that #141 factors out of
  `save` as `_sources_to_save`.

**What #141 should do.** Its reader and writer, the Rust bindings
(`rust/refkv-python/src/phkv.rs`), `Store::fetch` and `Store::key_bytes` in
`refkv-phkv`, and its tests carry over. What changes:

1. Move `python/src/refkv/phkv/` to `python-phkv/src/refkv_phkv/`, and
   `rust/refkv-python/src/phkv.rs` to `rust/refkv-phkv-python/`, so that
   `refkv._core` and `rust/refkv-python/Cargo.toml` do not depend on
   `refkv-phkv`.
2. Replace `ReferenceStore.save_phkv` with `refkv_phkv.save(store, path)`.
3. Replace the hard-coded `binding="phkv"` in `refkv.open` with the
   entry-point lookup, and make `_sources_to_save` public.
4. Move its README and docs sections to `python-phkv/README.md`, leaving one
   line in `README.md` that points to it.
5. Add the wheel and sdist jobs for `refkv-phkv` to the release workflow,
   copied from `refkv`'s with the working directory changed. The wheels are
   abi3, so one per platform as for `refkv`.

#141 can land in this form before or after the crate of [§2](#2-a-crate-for-part-1-refkv),
since it uses `refkv-phkv`'s public reader and writer, which the crate split
does not change.

## 6. The CLI, JS and CI

- **CLI.** `vzip convert` is an application, and may depend on both binding
  crates. It keeps `--format phkv`, loses `--format hybrid`, and imports the
  value types from `refkv`. Making the phkv output a default Cargo feature is
  possible but buys little while the CLI is not published.
- **JS.** Nothing changes. A JS phkv reader, if one is written, is its own npm
  package for the same reasons as in Python.
- **CI.** The `cli` job's `just` recipe already tests `refkv-vzip` and
  `refkv-phkv` separately; it gains `cargo test` for `refkv`, and rust-cache's
  `workspaces` list gains `rust/refkv`. The workspace's `dev` group gains
  `refkv-phkv`, so the `pytest` job's `uv sync` builds it, and `just test`
  runs its tests.

## 7. Order of work

Each step is one pull request against `main`, and each leaves `main` green.

1. **The `refkv` crate** ([§2](#2-a-crate-for-part-1-refkv)). Moves the
   Part 1 items and points `refkv-vzip`, `refkv-phkv` and the CLI at them. If
   #81 has not merged by then, it is rebased to put `SourceTable` in `refkv`.
2. **Drop the hybrid profile** ([§3](#3-the-hybrid-profile-drop-it)): code,
   CLI flag, tests, benchmark rows and the spec sections. `refkv-phkv` then
   no longer depends on `refkv-vzip`.
3. **Split the specification** ([§4](#4-the-specification)): renumber
   phkv.md, its version line, and refkv.md's three passages.
4. **#141 in its new form** ([§5](#5-python-and-141)): the `refkv-phkv`
   distribution, the `refkv.bindings` entry points and the release jobs.

Steps 1 to 3 touch different files except for `rust/refkv-phkv`, so 2 and 3
can go in either order once 1 has merged. Step 4 depends on none of them.

## 8. Open questions

- **The hybrid profile.** [§3](#3-the-hybrid-profile-drop-it) recommends
  dropping it. If it stays, step 2 becomes "make `refkv-phkv` depend on
  `refkv-vzip` only for `hybrid.rs`", and phkv's §21 stays, renumbered.
- **vzip's reserved names in `ReferenceStore`.** A `ReferenceStore` keeps
  values under vzip's reserved prefix `__vz__/` out of its mapping, and #141
  writes those it holds into a phkv store as ordinary keys. After the split,
  `refkv_phkv.save` should probably skip them, since they are the ZIP
  binding's bookkeeping, not the store's keys, but that needs a look at which
  hidden values a `ReferenceStore` can hold.
- **The directory name.** `python-phkv/` sits beside `python/`. The
  alternative is a `bindings/` or `phkv/` directory holding the phkv crate,
  its Python package and its specification together.
