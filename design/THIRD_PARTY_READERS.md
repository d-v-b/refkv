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
- **tifffile** 2026.9.20: `TiffFile`, every series and level's chunk offsets and byte counts
  from its IFDs, then `write_fsspec` per level (kerchunk's path);
- **nd2** 0.12.0: `ND2File`, every frame's offset;
- **czifile** 2026.8.16: `CziFile`, every subblock's data offset and size;
- **h5py** 3.16.0 (IMS): `h5py.File`, `get_chunk_info` of every chunk.

The packages read a file object, and how that object fetches bytes decides their I/O, so each
package ran through seven: fsspec 2026.9.0's HTTP file with no cache, read-ahead, or an LRU
block cache, at 64 KiB and 1 MiB blocks; and obstore 0.11.1's buffered reader
(`obstore.open_reader`) with 64 KiB and 1 MiB buffers. The table gives, for each file, the
core, the package's fastest fsspec setting, and its fastest obstore setting (of two settings
equally fast at the precision shown, the one that reads fewer bytes);
[the full sweep](experiments/third_party/results.md) has every setting. Requests and bytes
are deterministic. Wall times are end to end on one laptop, on a warm cache: the core's include
building the archive and its mirror, which the packages do not do (most of the core's 10 s
for the QPTIFF is building 268,860 references, not reading).

| file | size | core | package, fastest fsspec setting | package, fastest obstore setting |
|---|---:|---|---|---|
| IDR idr0096 OME-TIFF | 487 MB | 9 req · 457 kB · 1.3 s | tifffile, block cache 1 MiB: 3 · 1.8 MB · 0.5 s | tifffile, 1 MiB: 42 · 14.6 MB · 6.5 s |
| TCGA SVS | 538 MB | 16 · 720 kB · 2.4 s | tifffile, block cache 1 MiB: 7 · 5.9 MB · 1.6 s ¹ | tifffile, 64 KiB: 49 · 3.1 MB · 8.8 s |
| NDPI (ki67) | 221 MB | 19 · 428 kB · 2.1 s | tifffile, block cache 1 MiB: 5 · 3.7 MB · 1.6 s ² | tifffile, 64 KiB: 44 · 2.9 MB · 7.7 s |
| QPTIFF | 362 MB | 32 · 4.6 MB · 10.1 s | tifffile, block cache 1 MiB: 12 · 11.2 MB · 4.0 s | tifffile, 64 KiB: 186 · 10.4 MB · 31.1 s |
| ND2 (S-BIAD2077) | 263 MB | 26 · 192 kB · 1.4 s | nd2, block cache 1 MiB: 3 · 1.5 MB · 0.5 s ³ | nd2, 64 KiB: 4 · 74 kB · 0.6 s |
| ND2 (pollen) | 138 MB | 24 · 127 kB · 1.8 s | nd2, block cache 64 KiB: 3 · 86 kB · 0.5 s ³ | nd2, 64 KiB: 4 · 74 kB · 0.6 s |
| CZI (Axioscan) | 505 MB | 19 · 1.6 MB · 1.2 s | czifile, read-ahead 1 MiB: 288 · 301 MB · 45.4 s | czifile, 64 KiB: 487 · 31.9 MB · 73.9 s |
| CZI (IDR idr0011) | 44 MB | 13 · 315 kB · 1.0 s | czifile, read-ahead 1 MiB: 33 · 33.2 MB · 5.1 s | czifile, 64 KiB: 69 · 4.5 MB · 10.5 s |
| IMS (Convallaria) | 64 MB | 46 · 180 kB · 2.0 s | h5py, block cache 1 MiB: 33 · 33.6 MB · 5.0 s | h5py, 1 MiB: 278 · 290 MB ⁴ · 43.4 s |

1. tifffile also lists the SVS's stripped thumbnail, label and macro images, which the TIFF
   profile skips ([tiff.md §4.2](../spec/zarrify/tiff.md#42-pyramid-levels)); `write_fsspec`
   fails on those three levels ("incomplete chunks are not supported").
2. The two chunk an NDPI JPEG level differently: tifffile one chunk per MCU row of its
   McuStarts, the profile blocks of them ([ndpi.md §7](../spec/zarrify/ndpi.md#7-ndpi-profile)).
3. nd2's frame offsets are private (`ND2File._rdr._frame_offsets`). Given a URL rather than a
   file, nd2 opens it with fsspec itself, in 512 KB blocks: 5 requests, 532 kB, 0.8 s.
4. More than the file: obstore's reader refills its whole buffer after every seek, so a reader
   that seeks back and forth fetches the same bytes again and again.

What the numbers say:

- **TIFF and ND2: tuned, the packages are as fast as the core.** With fsspec's 1 MiB block
  cache, tifffile finds every chunk of each TIFF in 0.5 to 4 s (the core: 1.3 to 10 s, building
  the archive included), reading 2.5 to 9 times the core's bytes in fewer requests. With
  64 KiB blocks it reads 1.0 to 1.7 times the core's bytes, in 8 to 82 requests (the core: 9
  to 32). With 64 KiB blocks, nd2 reads 74 to 135 kB of the S-BIAD2077 file (the core: 192 kB)
  and 74 to 86 kB of the pollen file (the core: 127 kB), in 3 or 4 requests (the core: 24 to
  26), because it does less: it skips the frame headers the ND2 profile requires the core to
  check (below), and reads only the metadata it decodes, while the core reads all of it for
  the mirror. So for these formats, I/O is no argument either way.
- **CZI and IMS: no setting closes the gap.** czifile reads each subblock's header with a read
  of its own, and h5py each B-tree node, one request after another: 33 to 288 requests at
  best, against the core's 13 to 46, and 5 to 45 s against 1 to 2 s. A larger block buys
  fewer requests with many more bytes (301 MB of a 505 MB CZI). No cache helps when the
  reads are scattered and the reader asks for them one at a time; the core's planner batches
  the reads a parser asks for, coalesces near ranges and sends them concurrently.
- **obstore's file reader is slower than any fsspec cache for tifffile, czifile and h5py, and
  close to fsspec with no cache.** `ReadableFile` drops its buffer on every seek, so a parser
  that seeks between small reads (tifffile from tag to tag, czifile from header to header,
  h5py from node to node) makes about one request per seek: 42 to 186 requests for tifffile,
  69 to 487 for czifile and 278 for h5py, where fsspec's block cache needs 3 to 82, 42 to 485
  and 33 to 40. For nd2, which reads a few large ranges, it matches fsspec's read-ahead (4
  requests, 74 kB). obstore's strength is elsewhere: `get_ranges` (vectored reads) and async
  concurrency, which only a caller that knows its next reads in advance can use. That caller
  is a planner like the core's; none of these packages has one.

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
  worthwhile.** I/O does not decide it for TIFF: tuned, tifffile reads as fast as the core
  (for CZI and IMS it would, against czifile and h5py). The variants it would add for TIFF (strips, LZW, predictor 2, other JPEG
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
