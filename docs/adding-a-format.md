# Adding a file format

This guide is for whoever adds the next format to refkv's zarrifiers, a
person or an agent. It says what to do, in order, and what went wrong the
last twenty times. It is drawn from the profiles refkv has, from TIFF and ND2
to HDF5 and HEVC: their design plans, the pull requests that added and later
fixed them, and the working notes of the agents who wrote most of them.

A format becomes a **profile**: the rules that turn one input into one Zarr v3
hierarchy of references, and the **convention** that hierarchy follows. Both
live in one document, `spec/zarrify/<p>.md`, where `<p>` is the profile's
name. The work has seven parts:

1. [Check that the format fits](#1-check-that-the-format-fits).
2. [Research the format and its files](#2-research-the-format-and-its-files).
3. [Decide the layout](#3-decide-the-layout).
4. [Write the document](#4-write-the-document).
5. [Implement it](#5-implement-it).
6. [Fixtures, tests and verification](#6-fixtures-tests-and-verification).
7. [Docs and the demo](#7-docs-and-the-demo).

[§8](#8-before-opening-the-pull-request) lists the commands to run before a
pull request, and [§9](#9-the-mistakes-that-recurred) the mistakes that
recurred across formats.

The rules themselves are in [spec/zarrify.md](../spec/zarrify.md) (what every
profile does) and [spec/conventions.md](../spec/conventions.md) (what every
hierarchy looks like). [spec/TECHNIQUES.md](../spec/TECHNIQUES.md) is the best
single account of the techniques. This guide links to them rather than
restating them.

## 1. Check that the format fits

A profile references the file's bytes; it never copies or decodes the data.
That works when:

- **The data is in units that decode on their own**: tiles, strips, frames,
  chunks, planes, or contiguous runs that can be cut anywhere. Each unit
  becomes a chunk ([TECHNIQUES §2](../spec/TECHNIQUES.md#2-chunks-from-the-sources-own-units)).
  "A format whose units can't be made to decode independently … is rejected
  rather than approximated."
- **The layout follows from the structure alone.** The output must not depend
  on the pixel data ([zarrify §1.2](../spec/zarrify.md#12-input), "Structure
  only").
- **Some existing codec decodes each chunk**, or a new one can be written
  once and reach Python, the browser and the Neuroglancer fork
  ([§5.4](#54-a-new-codec-only-if-none-fits)).
- **An independent reader exists** to check the output against. Without one,
  there is no ground truth. The Arivis SIS investigation stopped at
  feasibility for this reason: the header said 164×498 tiles, each record
  held three quarters of a tile, and nothing open could confirm which was
  right.

## 2. Research the format and its files

Every profile that went smoothly started with research, and most bugs that
reached `main` came from skipping part of it.

**Read the standard and the reference implementations' source.** Read the
virtual-dataset tools too (kerchunk, VirtualiZarr), and the readers people
use. Prefer source code over secondary notes: a FITS research note said the
dither index wraps at 500, and cfitsio's `unquantize.c` says 10000.

**Survey real public files, over range reads, before designing.** Write small
scripts in a scratch directory that read headers only, and mark each claim
you checked against real bytes **[inspected]**, as
[design/plans/fits.md](../design/plans/fits.md) does. The plans did this at
scale:

- FITS: about 15 archives;
- CZI: 30 public files;
- SAFE: 7 processing baselines;
- NWB: 5 DANDI files, with h5py.

Real files found bugs that synthetic fixtures missed, in nearly every format:

- NDPI: offsets past 4 GiB, a frame header a row short, ASCII values stored
  out of line;
- ND2: padded rows;
- QPTIFF: the scanner's buggy LZW encoder;
- MP4: open groups of pictures in camera recordings, and Apple's signed `ctts`
  offsets;
- HDF5: a NaN attribute in DANDI 000728;
- IMS: fixed-array chunk indexes, soft links and LZ4, which only real Imaris
  files have.

**Expect headers and tags to lie.** Derive facts from the coded stream when
the two disagree:

- NDPI's tags say 8-bit YCbCr with 3 samples on fluorescence slides whose
  JPEG XR streams hold 16-bit gray. The profile reads the data type from each
  level's first stream header.
- A VSI level's size had to follow Bio-Formats' rule for the arrays to match
  what users see. The layout itself still comes from the bytes, not from the
  looser reader.

**Expect writers to deviate from the standard.** Decide whether to follow the
de facto readers, and record the decision with a dated change note:

- ISO 14496-12 makes a version 0 `ctts` offset unsigned. Apple writes −3003
  there, and QuickTime and ffmpeg read it as signed. Reading it unsigned
  dropped the video of all 88 DANDI `.mov` files
  ([#153](https://github.com/d-v-b/refkv/pull/153)).
- MRC went the other way: it refuses mrcfile's permissive fallback to little
  endian ([#83](https://github.com/d-v-b/refkv/pull/83)). IMOD's unsigned-byte
  flag, which mrcfile ignores, decides int8 against uint8.

**Expect reference readers to disagree, and choose the oracle explicitly:**

- CZI's JPEG XR sample order is RGB in imagecodecs and BGR in libCZI.
- libtiff and tifffile disagree on an uncompressed Predictor 2, so that case
  stays rejected.
- nibabel treats a NIfTI `vox_offset` of 0 as 352. The profile rejects it.
- liffile flips BGR to RGB.

**Use third-party readers as oracles, not as the implementation.** When
[#135](https://github.com/d-v-b/refkv/pull/135) compared them with the core:

- tifffile opened 28 of the 40 TIFFs the core rejects, and 6 of those came
  out as wrong references with no error;
- the packages read 1.4 to 250 times more bytes;
- none runs in wasm or the CLI.

Close a gap in the Rust core instead.

**Decide what one input is**: one file, a store, or a file read as a store
(NRRD's detached headers, VSI's tile files). FITS gives four reasons for a
single file, and leaves collections to a later combine step. SAFE takes a
store, because only the product's XML relates its JPEG 2000 files.

**For a large format, write a plan first**, in `design/plans/<p>.md`.
[fits.md](../design/plans/fits.md), [czi.md](../design/plans/czi.md),
[safe.md](../design/plans/safe.md) and [nwb.md](../design/plans/nwb.md) share
a shape:

- a research summary, ending in "findings that shaped the design";
- the decisions;
- an order of work in which each step already gives a working subset (FITS:
  plain files, then tile compression);
- numbered open questions, each with a recommended answer.

A plan is not normative: "the convention and the profile state the
decisions". Amend it when the code decides otherwise, as FITS did for empty
HDUs and NWB did in its status section.

## 3. Decide the layout

Each decision below is governed by the section linked in its first line.
Settle them in the plan or the document before writing code.

**What is rejected, and what fails.** See
[zarrify §1.2](../spec/zarrify.md#12-input) and
[§1.4](../spec/zarrify.md#14-store-inputs).
- An input is rejected, with a message, when it is malformed or unsupported.
  It fails on network errors, a changed ETag, or a stated resource limit.
- Whether an input is rejected must never depend on the order of reads.
- Source metadata never rejects ([TECHNIQUES §6](../spec/TECHNIQUES.md#6-repeatable-translation)).

**Record what you can't present; don't reject it.** A refused track or level
loses science. Several follow-up fixes turned a rejection into an accepted
input:

- open groups ([#110](https://github.com/d-v-b/refkv/pull/110)): a camera
  recording kept only its audio;
- HEVC ([#151](https://github.com/d-v-b/refkv/pull/151)): an iPhone clip, the
  same;
- signed `ctts` ([#153](https://github.com/d-v-b/refkv/pull/153)).

HDF5 lists unpresented objects with a reason instead of rejecting the file.

**The signature.** See [zarrify §1.2](../spec/zarrify.md#12-input), "Choosing
the profile".
- A test on the first `min(552, size)` bytes, with a precedence (`sniff`)
  among the file profiles.
- Or no signature, with `chosen_by` naming the profile whose reading chooses
  it, as TIFF chooses NDPI and QPTIFF.
- A new parent profile can change a child's outputs: HDF5 had to revise IMS.

**Chunks: the source's own units.** See
[TECHNIQUES §2](../spec/TECHNIQUES.md#2-chunks-from-the-sources-own-units)
and [conventions §3](../spec/conventions.md#3-arrays).
- Contiguous data is cut with a chunk limit: MRC, FITS and HDF5 use 2^20
  bytes ([conventions §7](../spec/conventions.md#7-source-values-as-arrays)).
- Size chunks for the domain. Video chunks are cut at keyframes and bounded
  by bytes of luma, not pictures, so 10-bit HEVC holds half as many pictures.

**Edge chunks.** The same fork came up in CZI, QPTIFF, SAFE, NIfTI, MRC, FITS
and NRRD.
- Prefer a regular grid with padding. Rectilinear grids broke
  `dask.array.from_zarr`, zarr-http-server and the Neuroglancer fork. QPTIFF
  moved off them ([#84](https://github.com/d-v-b/refkv/pull/84)).
- Pad with the one shared zero data source, not a literal per chunk
  ([#120](https://github.com/d-v-b/refkv/pull/120): 102 MB to 7.1 MB on an
  fMRI file). An LZW stream can take a literal tail.
- Deflate, zstd, JPEG and JPEG 2000 short strips can't be padded, so those
  inputs are rejected ([#137](https://github.com/d-v-b/refkv/pull/137)).
- A rectilinear grid is the last resort, as MP4's keyframe chunks are.

**Bytes that no chunk owns.** See [zarrify §1.2](../spec/zarrify.md#12-input).
- Shared bytes (JPEG tables, the zero source) become data sources, in order
  of first use.
- Every range lies within the file, and a reference payload is at most 65519
  bytes.

**Codecs.** See [conventions §3](../spec/conventions.md#3-arrays) and
[§10](../spec/conventions.md#10-coded-streams).
- The step order is transpose, delta, bytes or an image codec, then zlib or
  zstd.
- Audio and video use `refkv.stream`, extending its `format` table.
- A new codec only "where none of the others decodes the chunks"
  ([§5.4](#54-a-new-codec-only-if-none-fits)).

**Data type, byte order and scaling.** See
[conventions §3](../spec/conventions.md#3-arrays).
- Byte order goes in the `bytes` codec.
- Scaling (FITS `BZERO`/`BSCALE`, NIfTI's scale) is recorded, not applied.

**OME-NGFF, or not.** See [conventions §4](../spec/conventions.md#4-images)
and [§5](../spec/conventions.md#5-units).
- An image gets axes, scale and translation, and `omero` channels with
  windows. Without windows images open black, and ome-zarr-py rejected float
  images ([#98](https://github.com/d-v-b/refkv/pull/98)).
- Multi-series files are a bioformats2raw collection (LIF, CZI, ND2).
- FITS, SAFE (GeoZarr), HDF5 (hdmf-zarr) and the audio formats have no
  OME-NGFF.
- Defaults are not data: a unit the file never states is not written
  ([TECHNIQUES §3](../spec/TECHNIQUES.md#3-axes-grids-and-coordinates)).

**Source metadata and the IR mirror.** See
[conventions §6](../spec/conventions.md#6-source-metadata-as-json),
[§7](../spec/conventions.md#7-source-values-as-arrays) and
[§8](../spec/conventions.md#8-the-ir-mirror).
- The hierarchy must let a reader "recover all the information of the
  source": every header field, every key and every comment, as written.
- The IR mirror rebuilds the file's non-data bytes exactly. NRRD's detached
  header had to go into the mirror because no chunk references it, which a CI
  crash revealed.
- Metadata goes wrong silently. IMS channel colors came out white until the
  lookup table was read ([#46](https://github.com/d-v-b/refkv/pull/46)).
  [#131](https://github.com/d-v-b/refkv/pull/131) found integers past 2^53
  written as JSON numbers, FITS `TNULLn` dropped, and duplicate MP4 track IDs
  overwriting each other.

**A dataset layout (`<p>-dataset`), or not.** See
[conventions §9](../spec/conventions.md#9-dataset-layouts).

**Virtual shards, or not.** See
[zarrify §2.1](../spec/zarrify.md#21-virtual-shards). FLAC shards its frames;
NWB declined.

**Determinism.** See
[zarrify §1.1](../spec/zarrify.md#11-output-and-equivalence) and
[§1.3](../spec/zarrify.md#13-arithmetic).
- Two conforming zarrifiers make equivalent output.
- Arithmetic is binary64, in the order written.
- Nothing time- or tool-dependent goes in the output.

## 4. Write the document

`spec/zarrify/<p>.md` has two parts ([conventions §11](../spec/conventions.md#11-format-documents)).
[nrrd.md](../spec/zarrify/nrrd.md) and [mrc.md](../spec/zarrify/mrc.md) are
good models.

- **The header line**: `Convention version: 0 · UUID: … · Schema: …`, then
  dated "Changed" notes, including changes "with no change to any output".
  Version 0 until release. A draft names what it covers and links its plan.
- **Part 1, the convention:**
  - the unnumbered `## Layout` table of entries;
  - `## 1. Declaration`, with the exact convention metadata object, which
    `spec/tools/profiles.py check` compares with the registry;
  - the source's fields (a table of field, offset, type and meaning);
  - data types;
  - the arrays;
  - source metadata and the IR mirror;
  - an annotated example;
  - "What the layout does not express" (informative). On MRC, writing this
    section surfaced cell angles, symmetry and per-section tilt records, and
    led to a second layout, `mrc-dataset`.
- **Part 2, the profile:**
  - choosing the profile;
  - reading (its rounds and window sizes);
  - the checks, "in this order", each with its message;
  - chunks and the source table;
  - the summary line both implementations print.

Name type tables `<P>…` (`NrrdRoot`, `FitsHdu`), each with an
`<a id="type-…">` anchor. Every section reference is a link, never a bare
"§n" ([conventions §11](../spec/conventions.md#11-format-documents)); `just
sections` fails CI on one. Cite other standards with their name, as in "ITU-T
H.265 §7.4.2".

Write the schema module, `spec/source_metadata/<p>.py`, alongside the
document. `uv run python spec/generate_schemas.py` writes
`spec/zarrify/<p>/schema.json` from it.

## 5. Implement it

### 5.1 The registry

Copy an existing entry, such as
[profiles/nrrd.json](../spec/zarrify/profiles/nrrd.json), to
`spec/zarrify/profiles/<p>.json`:
- Keep its twelve members in `FIELDS` order (`spec/tools/profiles.py`).
- Give it a fresh UUID and version 0.
- Set `reference` to `null`, since there is no frozen twin.

Then run `just profiles`. It regenerates:
- the core's copy, `rust/refkv-ir/profiles/<p>.json`;
- the profile tables of `spec/zarrify.md`, `spec/conventions.md` and
  `docs/zarrify/README.md`;
- `python/src/refkv/zarrify/profile_classes.py`.

Commit them all. When two format PRs conflict in a generated block, rerun
`just profiles` instead of merging by hand.

This registry exists because adding a format used to touch about fifteen
shared files and renumber spec sections. FITS was renumbered twice
through merge conflicts ([#85](https://github.com/d-v-b/refkv/pull/85),
[#102](https://github.com/d-v-b/refkv/pull/102)). A plain file profile now
adds its own files and edits no shared list.

### 5.2 The Rust core

All parsing is in `rust/refkv-ir`, which runs natively for Python and the CLI
and as wasm in the browser. `build.rs` finds the new profile through the
registry. Add:

- `src/<m>.rs` (where `<m>` is the name with `-` as `_`):
  - a `pub struct Profile` implementing `FileProfile` (`src/run.rs`: `detect`,
    `parser`, `project`, and optionally `reads`, `mirrored`, `whole`,
    `store_run`);
  - a sans-IO parser with `new(size)`, `step`, `feed` and `finish`;
- `src/project/<m>.rs`, the projection to the Zarr layout;
- `schema/<p>.json`, the source model;
- `tests/<m>.rs`, which includes the reject tests that `build.rs` generates
  from the reject table ([§6](#6-fixtures-tests-and-verification)).

**Never panic.** The wasm build must reject malformed input, not abort.
Panics on fixed-offset slices and short records were fixed after review or
after merge in DICOM, NDPI, NIfTI and MP4
([#57](https://github.com/d-v-b/refkv/pull/57),
[#58](https://github.com/d-v-b/refkv/pull/58),
[#59](https://github.com/d-v-b/refkv/pull/59),
[#132](https://github.com/d-v-b/refkv/pull/132),
[#133](https://github.com/d-v-b/refkv/pull/133)).
- Read with `.get()` and checked arithmetic.
- When fixing one panic, audit every other fixed-offset read in the parser.

**Bound every loop and every read**, with stated limits, since a hostile file
can loop forever or ask for a whole file:
- the DICOM batch retry;
- the LIF walk on long block IDs;
- the IMS object walk (`100,000 + 5 × N` objects, depth 64);
- the VSI file (256 MiB);
- the LZW tail.

**Plan the reads.** Count requests, bytes and rounds against a model of 20 to
80 ms per request.
- Coalesce nearby ranges. An 877-row ND2 frame once cost 877 requests, and
  Zenodo answered 429 ([#10](https://github.com/d-v-b/refkv/pull/10)).
- Predict header offsets where the format allows it. LIF reads a 3 GB file in
  14 requests and 5.66 MB.
- Read in bounded windows, as FLAC does with 64 KiB and 16 MiB.
- Watch the bytes per chunk in the archive. Varint frame headers halved the
  MP3 archive ([#99](https://github.com/d-v-b/refkv/pull/99)).

### 5.3 The hosts

A plain file profile needs no change in Python, the browser code or the CLI:
each reads the registry. Python needs only the generated classes.

A file read as a store run (`store_run()`, as NRRD's detached headers and
VSI's tiles are) is still registered by hand in several places:
- `StoreFormat` in `rust/refkv-ir/src/store.rs`;
- `rust/refkv-cli/src/{run.rs,main.rs}`;
- `js/src/zarrify/{index.ts,ir/run.ts,ir/store.ts}`;
- `python/src/refkv/zarrify/__init__.py`;
- `conformance/zarrify/{compare.py,proxy.py}`.

[#119](https://github.com/d-v-b/refkv/pull/119) is the model. Every read of
the extra files goes through the reader policy
([#41](https://github.com/d-v-b/refkv/pull/41)). Test paths on Windows too:
CI caught a runner that split paths on `/`.

### 5.4 A new codec, only if none fits

Check imagecodecs and numcodecs first, and check their exact semantics.
`imagecodecs_delta` fits TIFF's Predictor 2 for every integer type and byte
order. `numcodecs.delta` does not, because it differences the flattened chunk
([#139](https://github.com/d-v-b/refkv/pull/139)).

A codec of refkv's own is named `refkv.<codec>`. Same-named codecs from refkv
and imagecodecs-zarr made zarr pick one arbitrarily, and hid a difference in
behavior ([#136](https://github.com/d-v-b/refkv/pull/136)). FITS's
`refkv.fits_tiles` ([#88](https://github.com/d-v-b/refkv/pull/88)) and FLAC
([#121](https://github.com/d-v-b/refkv/pull/121)) show every place a codec
reaches:

- `rust/refkv-ir/src/<codec>.rs` and its `pub mod`;
- the bindings in `rust/refkv-python/src/lib.rs` and
  `python/src/refkv/_core.pyi`;
- `python/src/refkv/zarr/codecs/<codec>.py`, and its `zarr.codecs` entry point
  in `python/pyproject.toml`;
- the wasm export, for the browser;
- a section in `spec/conventions.md`.

Make each chunk self-describing. FITS puts every tile's parameters in a
literal prefix, so "the decoder never needs the chunk's position". For a
dependent stream, decode with bounded memory: MP4's streaming decode cut peak
memory from 819 MB to 105 MB ([#107](https://github.com/d-v-b/refkv/pull/107)).

**The Neuroglancer fork needs its own pull request for every new codec.** It
is a separate repository (d-v-b/neuroglancer, branch `vzip`). Give the work a
session of its own, and state the merge order in both PRs. It has lagged on:
- `n5_default`, float16 and bool;
- LZW;
- `imagecodecs_delta`;
- the codec renames;
- HEVC.

Where the browser can't decode exactly, refuse with an error rather than
show wrong pixels. WebKit and Chromium convert HEVC chroma nonlinearly, 57 to
72 levels off, so the fork refuses those pictures.

## 6. Fixtures, tests and verification

### Fixtures

`fixtures/generators/<p>/write_fixtures.py` writes `fixtures/<p>/`, and
`just fixtures-of <p>` runs it.

**Keep fixtures small.** Use one chunk per array, except where a fixture tests
chunking, and make each rejection the smallest input that still triggers its
rule ([#29](https://github.com/d-v-b/refkv/pull/29)). A fixture that must be
large is written only on request, as CZI's over-16-MiB ones are (`--big`).

**Make generators deterministic on every platform.** CI's
`fixtures-reproducible` job reruns them on Linux, and macOS output has failed
it three times:
- The gzip header's OS byte is 19 on macOS and 3 on Linux. Pin it.
- Python's zlib output differs between builds. FITS writes stored deflate
  blocks.
- astropy's quantization depends on floating-point arithmetic. FITS quantizes
  by hand.
- x265 embeds a version string in its stream. Pass `info=0`.

A generator that needs ffmpeg, lame or the network says so in a
`# fixtures: on demand (…)` line, and `just fixtures` skips it. Add edge
fixtures that need no such tool.

**Hand-build what writers can't make.**
- astropy's random-groups writer has a bug.
- pylibCZIrw can't write pyramids, JPEG XR or attachments.
- Bio-Formats needed EXIF dimensions in the VSI fixtures.

A struct-based writer can also make the malformed files that rejections need.

**Name rejected inputs `<p>_reject_<case>`**, or `unsupported_*` or
`edge_reject_*` ([HARNESS.md](../conformance/zarrify/HARNESS.md)). List each
in `conformance/zarrify/rejects/<p>.json` with its exact message. The table
lives outside `fixtures/` because generators rewrite their whole directory.
`build.rs` makes a Rust test of each case, and `python/tests/ir/test_rejects.py`
checks each one, including that every rejected fixture is listed. A test
must check why the input was rejected, not only that it was: roborev found a
probe test that passed when both sides rejected
([#58](https://github.com/d-v-b/refkv/pull/58)).

### Tests

Follow the house style in `python/tests/ir/test_<p>.py`: one test over the
valid inputs, comparing every value, and one test per error case.

- **Test the projected chunk bytes, not only the parser's facts.** The NDPI
  fix that read in-line values said this would have caught its first finding.
- **Check that a new test fails on the old code.** Several fixes say so: "run
  against the unfixed parser, both tests fail."

### Verification against an independent reader

Every profile compares its output with the format's own library, value for
value. `js/test/<p>/verify.py` serves the fixtures over HTTP, zarrifies them
with the browser code, reads them back through zarr-python, and compares them
with the reader. `just verify-of <p>` runs it; CI does not, so run it before
the PR. The readers so far:

| format | independent reader |
|---|---|
| TIFF, QPTIFF | tifffile |
| ND2 | nd2 |
| DICOM, NIfTI | pydicom, nibabel |
| IMS, HDF5 | h5py |
| MRC | mrcfile |
| VSI | Bio-Formats (needs a JVM) |
| FITS | astropy |
| LIF | liffile |
| NRRD | pynrrd |
| FLAC | libsndfile, and the file's own MD5 |
| MP3, MP4 | ffmpeg through PyAV |
| CZI | czifile, pylibCZIrw |

Add the reader to the root `pyproject.toml`'s `dev` group, then run `uv lock`.

- **Test with the real consumer too.** Pixel equality was not enough for NWB:
  pynwb's `NWBZarrIO` failed on a NaN attribute that the arrays never showed
  ([#149](https://github.com/d-v-b/refkv/pull/149)). QPTIFF also had to open
  in napari.
- **Choose a decoder that is exact chunk by chunk.** ffmpeg's fixed-point MP3
  decoder carries rounding forward, and miniaudio drops a lead-in frame.
  ffmpeg's float decoder is exact.
- **Where exactness is impossible, state the tolerance in the spec.** JPEG
  2000 decoders round within 1. astropy's arm64 build fuses multiply and add.
  AAC noise substitution is never exact.
- **Compare refkv with itself.**
  - `compare.py` compares Python with the browser code over every fixture.
  - `just cli-identical` checks that the CLI writes the same archive, byte for
    byte, over thousands of conversions.
  - A refactor proves itself by hashing every fixture's archive before and
    after.

### The public corpus

List public inputs in `conformance/zarrify/corpus_<m>.txt`, one
`url|name|no-ref|ends-sha256=…` line each.
- Record the fingerprints with
  `uv run python conformance/zarrify/corpus_hash.py record`. 31 inputs added
  with seven profiles had none, and the nightly's fingerprint check failed on
  every run until [#138](https://github.com/d-v-b/refkv/pull/138).
- Pin URLs to a commit or version.
- Check each host for range support and license. Reference licensed files
  rather than copying them.
- Expect hosts to throttle or disappear:
  - Zenodo answers 429;
  - IRSA stopped serving ranges;
  - an OpenOrganelle store was deleted;
  - Wikimedia returns 403 to a client without a `User-Agent`.

## 7. Docs and the demo

Write `docs/zarrify/<p>/README.md` on the template the other pages share
([nrrd](zarrify/nrrd/README.md), [mrc](zarrify/mrc/README.md)):
- who it is for;
- what you get;
- try it;
- viewing;
- supported and not supported;
- performance;
- how it's verified.

Add `docs/zarrify/<p>/example.py`, a self-contained script (PEP 723) that the
nightly runs; `just examples` runs them locally.

Two hand-written rows are easy to forget, since nothing checks them:
- the format's bullet on source metadata in
  [docs/zarrify/README.md](zarrify/README.md);
- its row in [spec/TECHNIQUES.md](../spec/TECHNIQUES.md).

A demo example is `demo/examples/<p>.html`, with a `<!-- credit: … -->`
line. Check these first:
- **The host must send CORS headers.** downloads.openmicroscopy.org and NASA
  don't; DANDI's S3 and raw.githubusercontent.com do. GitHub caps files at
  100 MB.
- **The example should display well in its default view**, and its label
  should match the file's source metadata.
- **Run it in headless Chromium** (`demo/e2e_examples.mjs`) and probe pixel
  values against Python.

## 8. Before opening the pull request

Run these in this order, which is close to CI's:

```bash
just profiles
```

```bash
uv run python spec/generate_schemas.py
```

```bash
just fixtures-of <p>
```

```bash
just fixtures-check
```

```bash
just sections
```

```bash
just lint
```

```bash
just typecheck
```

```bash
just test
```

```bash
just ir-rust-test
```

```bash
just ir-wasm
```

```bash
just js::test
```

```bash
just cli-test
```

```bash
just cli-identical
```

```bash
just verify-of <p>
```

Stale builds gave agents false results more than once:
- The core embeds the registry at compile time, so rebuild after
  `just profiles`.
- A rebase can leave the wasm stale, and `just test`'s conventions test runs
  it through Node. Run `just js::wasm`.
- A fresh worktree has no wasm build and no `js/node_modules`. Run
  `just js::wasm` and `npm ci` in `js/` before trusting a failure.
- Leave `CARGO_TARGET_DIR` unset; don't set it to an empty string.

Open the PR against `main`, not stacked on another PR: PRs merged into a
branch that was then squash-merged never reached `main`
([#20](https://github.com/d-v-b/refkv/pull/20),
[#30](https://github.com/d-v-b/refkv/pull/30)). Put the Neuroglancer fork's
PR and the merge order in the description.

## 9. The mistakes that recurred

1. **Designing from the standard instead of real files.** Survey public files
   first. Headers and tags lie, and writers deviate.
2. **Treating a reference reader as the truth.** They disagree with each
   other. Pick the oracle, and record the cases it gets wrong.
3. **Rejecting what could have been recorded.** A skipped track or level is
   lost science.
4. **Panics and unbounded loops on hostile input.** These are bugs in wasm.
5. **Edge chunks.** Pad with the zero source on a regular grid, and reject
   what can't be padded.
6. **Fixtures that differ between macOS and Linux.**
7. **Testing a stale build.**
8. **Forgetting the browser.** A new codec or grid means work in the
   Neuroglancer fork, and the demo's host needs CORS.
9. **Silent metadata loss**: colors, large integers, null markers, duplicate
   IDs. Compare every metadata value with the reader, not only the pixels.
