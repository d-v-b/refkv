# Third-party readers as format parsers

*Evaluated 2026-10-27, against refkv at `209b0efc`.*

Could refkv depend on existing Python packages (tifffile, nd2, czifile, …) for its formats,
declared as extras such as `pip install refkv[tiff]`, instead of its own parsers? This note
measures what those packages would bring and what they would cost, format by format, and
recommends keeping the Rust parsers. It does not prototype a package-backed path: none is
clearly worthwhile (see [Recommendation](#recommendation)).

## What a parser has to do here

Any proposal answers to the current architecture
([ARCHITECTURE.md](ARCHITECTURE.md)):

- **One parser per format, in Rust.** Every format is parsed by a sans-IO parser in the Rust
  core (`rust/refkv-ir/src/<format>.rs`), projected to the convention's hierarchy
  (`src/project/`) and mirrored under `refkv_source`. A read planner
  (`python/src/refkv/ir/planner.py`, policy in `rust/refkv-ir/src/plan.rs`) drives it with
  bounded, batched, concurrent ranged reads.
- **Three hosts share that parser.** It is compiled into the Python package (`refkv._core`),
  the browser (wasm, `js/`) and the Rust CLI (`rust/refkv-cli`), and their archives are byte
  for byte the same (`just cli-identical`). Each profile's spec (`spec/zarrify/<format>.md`)
  defines the output exactly: the hierarchy, the references, the source metadata, the inputs
  it rejects, and which bytes it reads.
- **Structure only, never pixels, often over HTTP.** A parser reads directories, headers and
  tables, and never a chunk's bytes, so a multi-GB file takes a handful of requests.

So a package-backed path has to (1) give every chunk's byte range without reading pixels,
(2) do it over HTTP in few requests and few bytes, (3) accept what the profile accepts and
reject what it rejects, and (4) fit the hosts that cannot run Python.

## Measurements

[`experiments/third_party/measure.py`](experiments/third_party/measure.py) puts a local HTTP
server in front of public files from refkv's corpus. The server answers `HEAD` and ranged
`GET`s (single ranges, and several as `multipart/byteranges`, which the core asks for) from
64 KiB blocks it caches on disk, waits 20 ms before each answer (a wide-area round trip), and
counts the requests and the bytes of the ranges it answers. Each reader then finds every chunk:

- **core**: `refkv.zarrify.zarrify(url)`, the whole archive in memory, mirror included;
- **tifffile** 2026.9.20: `TiffFile` over an fsspec HTTP file, every series and level's
  chunk offsets and byte counts from its IFDs, then `write_fsspec` per level (kerchunk's path);
- **nd2** 0.12.0: `ND2File(url)` (fsspec inside), every frame's offset;
- **czifile** 2026.8.16: `CziFile` over fsspec, every subblock's data offset and size;
- **h5py** 3.16.0 (IMS): `h5py.File` over fsspec, `get_chunk_info` of every chunk.

fsspec 2026.9.0's HTTP file runs with its default cache (5 MiB blocks, read ahead) and with
64 KiB blocks. Requests and bytes are deterministic; wall times are end to end (the core's
includes building the archive and its mirror, which the packages do not do), measured on one
laptop while other jobs ran, so they only show orders of magnitude. The tables come from a
second run, on a warm cache: on a first run, the reader that goes first also waits for the
upstream servers.

| file | size | reader | requests | bytes read | wall time | chunks |
|---|---:|---|---:|---:|---:|---:|
| IDR idr0096 OME-TIFF | 487 MB | core | 9 | 457 kB | 1.3 s | 5,037 |
| | | tifffile, fsspec default | 4 | 5,616 kB | 1.8 s | 5,037 |
| | | tifffile, fsspec 64 KiB | 7 | 650 kB | 1.2 s | 5,037 |
| TCGA SVS | 538 MB | core | 16 | 720 kB | 3.5 s | 78,312 |
| | | tifffile, fsspec default | 10 | 36,099 kB | 2.9 s | 78,426 ¹ |
| | | tifffile, fsspec 64 KiB | 20 | 1,769 kB | 4.5 s | 78,426 ¹ |
| NDPI (ki67) | 221 MB | core | 19 | 428 kB | 2.0 s | 588 ² |
| | | tifffile, fsspec default | 30 | 107,573 kB | 5.6 s | 85,682 ² |
| | | tifffile, fsspec 64 KiB | 36 | 2,556 kB | 6.0 s | 85,682 ² |
| QPTIFF | 362 MB | core | 32 | 4,559 kB | 5.5 s | 268,860 |
| | | tifffile, fsspec default | 6 | 29,299 kB | 3.2 s | 268,291 |
| | | tifffile, fsspec 64 KiB | 60 | 7,908 kB | 11.6 s | 268,291 |
| ND2 (S-BIAD2077) | 263 MB | core | 26 | 192 kB | 1.5 s | 25 |
| | | nd2, its own fsspec file ³ | 5 | 532 kB | 1.1 s | 25 |
| | | nd2, fsspec default | 4 | 5,251 kB | 2.4 s | 25 |
| | | nd2, fsspec 64 KiB | 4 | 74 kB | 0.6 s | 25 |
| ND2 (pollen) | 138 MB | core | 24 | 127 kB | 1.6 s | 57 |
| | | nd2, its own fsspec file ³ | 5 | 532 kB | 0.8 s | 57 |
| | | nd2, fsspec default | 4 | 5,251 kB | 3.4 s | 57 |
| | | nd2, fsspec 64 KiB | 4 | 74 kB | 0.6 s | 57 |
| CZI (Axioscan) | 505 MB | core | 19 | 1,602 kB | 1.2 s | 481 |
| | | czifile, fsspec default | 131 | 660,224 kB ⁴ | 22.6 s | 481 |
| | | czifile, fsspec 64 KiB | 485 | 31,808 kB | 76.1 s | 481 |
| CZI (IDR idr0011) | 44 MB | core | 13 | 315 kB | 1.0 s | 63 |
| | | czifile, fsspec default | 11 | 52,125 kB ⁴ | 1.8 s | 63 |
| | | czifile, fsspec 64 KiB | 65 | 4,197 kB | 10.5 s | 63 |
| IMS (Convallaria) | 64 MB | core | 46 | 180 kB | 2.0 s | 240 |
| | | h5py, fsspec default | 164 | 558,115 kB ⁴ | 26.9 s | 240 |
| | | h5py, fsspec 64 KiB | 173 | 11,205 kB | 27.8 s | 240 |

1. tifffile also lists the SVS's stripped thumbnail, label and macro images, which the TIFF
   profile skips ([tiff.md §4.2](../spec/zarrify/tiff.md#42-pyramid-levels)); `write_fsspec`
   fails on those three levels ("incomplete chunks are not supported").
2. The two chunk an NDPI JPEG level differently: tifffile one chunk per MCU row of its
   McuStarts, the profile blocks of them ([ndpi.md §7](../spec/zarrify/ndpi.md#7-ndpi-profile)).
3. Given a URL, nd2 opens it with fsspec itself, in 512 KB blocks (and fetches a file of at
   most 32 MB whole); the other two rows pass it an fsspec file opened with each cache. Its
   frame offsets are private (`ND2File._rdr._frame_offsets`).
4. More than the file: fsspec's read-ahead cache holds one block, so readers that seek back
   and forth (czifile from the directory to each subblock's header, h5py through B-tree
   nodes) fetch the same 5 MiB blocks again and again.

What the numbers say:

- **Bytes.** With fsspec's defaults, tifffile reads 6 to 250 times the core's bytes (5.6 to
  108 MB for files the core opens with 0.4 to 4.6 MB), because each 5 MiB block is read
  whole; czifile and h5py read more than the whole file (660 MB of a 505 MB CZI, 558 MB of a
  64 MB IMS). Tuned to 64 KiB blocks, tifffile comes within 1.4 to 6 times the core's bytes,
  czifile within 13 to 20 times and h5py 62 times. nd2 is the exception: with 64 KiB
  blocks it reads 74 kB, less than the core's 127 to 192 kB, because it reads less. It skips
  the frame headers the ND2 profile requires the core to check (below), and it reads only the
  metadata it decodes, while the core reads all of the file's metadata for the mirror. With
  its own 512 KB blocks it reads 532 kB.
- **Requests.** The packages read through a file-like object, one request at a time, while
  the core's planner batches the reads a parser asks for, coalesces near ranges and sends
  each batch over up to 8 connections. For TIFF the counts are similar, but tifffile
  tuned for bytes needs 60 sequential requests for the QPTIFF. czifile reads each
  subblock's header on its own (485 requests for 481 subblocks, against the core's 19), and
  h5py walks B-tree nodes one by one (173 against 46). The core's 24 to 26 requests for an
  ND2 file are the frame headers the profile requires it to check (below); nd2 does not read
  them.

So on I/O, only nd2 beats the core, by not doing work the profile requires, and only when the
caller tunes fsspec. A tuned tifffile comes close; czifile and h5py are an order of magnitude
or more behind in bytes, requests or both. For TIFF and ND2, I/O alone would not rule a
package out; the next sections do.

## What the packages accept

The profiles' rejected inputs (`conformance/zarrify/rejects/<format>.json`, one fixture per
rejection) show what a package-backed path would do differently
([`experiments/third_party/rejects.py`](experiments/third_party/rejects.py) runs each
package over them). tifffile opened 28 of the
TIFF profile's 40 rejected fixtures. For 4 it could not write a level as references
(`write_fsspec` refuses FillOrder 2, PlanarConfiguration 3 twice, and a Compression tag with
no value); the other 24 it wrote whole:

- **Variants the profile does not cover yet** (6): strips (`unsupported_strips`), LZW
  (`unsupported_lzw`), the horizontal predictor (`unsupported_predictor`, as
  `imagecodecs_delta` + `zlib`), 16-bit JPEG, planar JPEG, and JPEG without
  PhotometricInterpretation. These are real gaps, and the reason to consider an extra.
- **Malformed files, with wrong references and no error** (6): a tile outside the file and a
  tile count short of the image (references past the end of the file), an offset past
  2^53 − 1 (that chunk left out, so it reads as the fill value), an image with no width
  (shape `(64, 0)`), and two YCbCr images with 2 × 2 subsampling, listed as `uint8` RGB with
  no codec, so that a Zarr reader decodes subsampled bytes as full-resolution pixels.
- **Other inputs the spec rejects** (12): an IFD cycle, a shared SubIFD, a SubIFD count that
  differs between planes, a signed width, planes of one level that differ in size, two
  malformed JPEG streams (whose chunks would fail when read), four OME-XML documents the
  profile reads strictly (TiffData outside the planes, an unmapped plane, a TiffData that
  steps too far, a self-closing UUID naming another file), and a file with no images (opened
  as nothing). Some of these references may be usable; the spec rejects the inputs because
  their meaning is ambiguous or their cost unbounded.

It failed to open the other 12, mostly with Python exceptions (`KeyError: 'DimensionOrder'`,
`ZeroDivisionError`) rather than a rejection.

nd2's reader assumes every frame's pixels start 4,096 bytes after its chunk's offset
(`24 + 4072`, a chunk name padded to 4,072 bytes, as NIS Elements writes it), unless
`validate_frames=True`. The ND2 profile reads each frame's header and places the pixels at
`o + 16 + n + 8` for the name length `n` it finds, and rejects a frame whose header is not a
chunk or whose name length differs from the others
([nd2.md §7.3](../spec/zarrify/nd2.md#73-frames)). So for a file written with another name
length, nd2's offsets are wrong. It also has no byte ranges at all for legacy (JPEG 2000)
ND2 files, the one ND2 variant the profile rejects that it reads. (nd2 lists frame offsets for
41 of the profile's 43 synthetic rejection fixtures, but cannot read the sizes of any of them:
they lack chunks NIS writes, such as `ImageMetadataSeqLV|0!`, so that comparison says nothing
either way.)

## Per format

| format | package | chunk ranges without pixels | license | install | fits the profile |
|---|---|---|---|---|---|
| TIFF, OME-TIFF, SVS | tifffile | yes: IFD offsets and byte counts; `write_fsspec` | BSD-3-Clause | 1.2 MB, pure Python, numpy | accepts malformed files silently (above) |
| NDPI | tifffile | yes, per MCU row (McuStarts) | BSD-3-Clause | as above | different chunking |
| QPTIFF | tifffile | yes | BSD-3-Clause | as above | different chunking of strips |
| ND2 | nd2 | private API only, modern files only | BSD-3-Clause | 0.4 MB, plus dask, pydantic, ome-types | assumes a fixed frame header |
| CZI | czifile | yes: each subblock's header is read for its data offset | BSD-3-Clause | 0.3 MB, plus imagecodecs (38 MB native) | listed subblocks for 34 of the profile's 39 rejected fixtures (a subblock outside the file, negative sizes, a split file among them) |
| CZI | pylibCZIrw | no: libCZI decodes subblocks, no byte ranges | LGPL-3.0 | 2.8 MB native | no |
| LIF | liffile | yes: memory blocks' offsets and sizes | BSD-3-Clause | 0.2 MB, pure Python | not measured |
| DICOM | pydicom | native pixel data: the element's offset; encapsulated: by walking fragment items | MIT | 8.3 MB, pure Python | not measured |
| NIfTI | nibabel | yes: `vox_offset` | MIT | 8.6 MB, pure Python | not measured |
| MRC | mrcfile | yes: header and extended header size | BSD-3-Clause | 0.1 MB, pure Python | not measured |
| FITS | astropy | uncompressed HDUs: data offsets; tile-compressed: astropy decompresses, the heap descriptors are not exposed | BSD-3-Clause | 24 MB native, plus pyerfa, PyYAML | partial |
| IMS (HDF5) | h5py | yes: `get_chunk_info` | BSD-3-Clause | 10.5 MB native (libhdf5) | read all 18 rejected fixtures: valid HDF5 whose Imaris layout the profile rejects, so the Imaris layer would remain refkv's |
| NRRD | pynrrd | yes: the header's data offset or detached file | MIT | 0.1 MB, pure Python | not measured |
| VSI | Bio-Formats (bioio-bioformats) | no: a JVM reader that decodes tiles | GPL-2.0 (Bio-Formats) | a JVM | no |
| MP3, FLAC, MP4 | PyAV (ffmpeg) | demuxed packets' positions, by reading the stream | BSD-3-Clause (PyAV), LGPL/GPL (ffmpeg) | 46 MB native | no |
| SAFE, Zarr v2, OME-Zarr, N5 | (none needed) | the chunks are whole objects; the work is the listing and the convention | | | |

## What it would cost the other hosts

None of these packages runs in the browser or in the Rust CLI. A package-backed path is a
Python-only path, and that is what decides the question:

- **Byte-identical archives.** The three hosts give the same archive for the same input
  because they run the same parser. An archive from `refkv[tiff]` would have no CLI or browser
  equivalent, and `just cli-identical` could not cover it. Two archives of the same TIFF would
  differ depending on which host, or which extras, made them.
- **No spec for the output.** A profile's spec defines its output, its rejections and its
  reads; a package defines its own, and changes them between versions. Either the extra's
  output is a new, unspecified profile, or it claims the TIFF profile while accepting inputs
  that profile rejects.
- **No mirror.** The `refkv_source` mirror, which keeps every byte of the source's structure
  ([conventions §8](../spec/conventions.md#8-the-ir-mirror)), is built from the core's IR of
  the file. A package gives chunk ranges, not that IR, so a package-backed archive would lose
  source metadata that every other archive of the format keeps.
- **Codecs.** tifffile writes the horizontal predictor as `imagecodecs_delta`, which no
  profile names, so those archives would decode in refkv.zarr with imagecodecs and nowhere
  else. (LZW is already named, as `imagecodecs_lzw`, by the QPTIFF profile.)
- **Weight.** The extras would be small for tifffile, liffile, mrcfile and pynrrd, but nd2
  brings dask and pydantic, czifile imagecodecs (38 MB), astropy 24 MB of native code,
  pylibCZIrw is LGPL native code, and Bio-Formats needs a JVM and is GPL.

## Recommendation

**(a) Keep the Rust parsers**, and use the packages as they are used today: as independent
verifiers (`js/test/<format>/verify.py`), now with the measurements above to compare against.

- **(c), replacing parsers, is ruled out** by the hosts: the browser and the CLI would lose
  the format, and every host would lose the byte-identical guarantee, the spec'd rejections
  and the mirror. The packages are also less strict than the specs: tifffile turned 6
  malformed TIFFs into wrong references without an error, and nd2 assumes a frame layout the
  ND2 profile checks.
- **(b), package-backed extras for the variants the core rejects, is not clearly
  worthwhile.** The variants it would add for TIFF (strips, LZW, predictor 2, other JPEG
  layouts) and ND2 (legacy JPEG 2000) are gaps in the profiles, not parsing problems. The
  QPTIFF profile already reads strips and LZW in Rust
  ([qptiff.md §4.1](../spec/zarrify/qptiff.md#41-data-types-and-codecs)), with a spec'd codec
  that every host writes the same way (the browser's own Zarr reader has no LZW decoder yet).
  Closing those gaps in the Rust TIFF profile lets all three hosts build those archives,
  keeps them identical, and keeps the mirror; an extra would give them to
  Python only, with an unspecified output that the other hosts cannot reproduce, and would
  need a strictness layer on top of tifffile to avoid silent wrong references. So no
  prototype is included.

Follow-ups this suggests:

1. Extend the TIFF profile to stripped images, LZW and the horizontal predictor, reusing the
   QPTIFF profile's strip and LZW handling: the variants tifffile reads that the core rejects.
   The browser would also need an `imagecodecs_lzw` decoder to read them, which it still
   lacks for QPTIFF's LZW levels too.
2. Use tifffile, czifile and h5py as differential oracles over the corpus: every chunk range
   the core gives should be one the package gives (where both accept the file), which would
   catch parser bugs the per-format verifiers miss.
