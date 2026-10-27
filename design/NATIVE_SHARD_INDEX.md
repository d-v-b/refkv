# The shard index an HDF5 file already holds

*Measured 2026-10-27, against refkv at `63288330` (PR 141, which adds `refkv.phkv`). One dataset.*

A file that is both an HDF5 file and a Zarr v3 shard holds its shard index twice: as the Zarr
`zarrindex` and as the chunk index of the HDF5 dataset. For a file written with HDF5's new format, the two
are the same 8 KiB, so a virtual shard can reference the HDF5 copy instead of storing a copy of its own.
This note measures what that saves in vzip and phkv archives, and what it needs of the file.

## The dataset

https://s3.janelia.org/shroff-lab/s0_hdf5/ (the `shroff-lab` bucket of Janelia's open S3 endpoint) is a Zarr v3 array of 6368 x 1600
x 12000 uint8, sharded 512³ with 64³ inner chunks (Blosc, zstd, bitshuffle), index at the end, with
`crc32c`. Of its 1248 shards, 30 exist (`c/0/0/0`..`c/0/0/23`, `c/0/1/0`..`c/0/1/5`). Each is also an HDF5
file (superblock 3) with the datasets `zarrshard` (512³, 64³ chunks, fixed size, filter 32001) and
`zarrindex` (8196 bytes, contiguous, the last bytes of the file). `virtual.h5` of the same prefix is an HDF5
virtual dataset over the 30 files. The files are written with `libver="latest"` (`virtual.py` beside them),
so `zarrshard`'s layout message is version 4 and its chunk index is a **Fixed Array**.

## Why the entries coincide

| | HDF5 Fixed Array entry (filtered chunks) | Zarr shard index entry |
|---|---|---|
| bytes 0-7 | chunk address (file offset) | `offset` (from the start of the shard) |
| bytes 8-15 | compressed size (4 bytes), then filter mask (4 bytes, 0) | `nbytes` (8 bytes, little-endian) |
| an empty chunk | address `2⁶⁴−1`, size 0 | offset and `nbytes` both `2⁶⁴−1` |
| after the entries | a checksum (Jenkins lookup3) | a CRC-32C |

- The chunk-size field is `1 + (⌊log2(chunk bytes)⌋ + 8) / 8` bytes, at most 8 (`H5Dfarray.c`, for layout
  version 4), where the chunk's bytes are its elements' uncompressed. That is 4 bytes for chunks of 64 KiB up
  to just under 16 MiB, such as 64³ uint8 (256 KiB): an entry is then 16 bytes, as Zarr's is. 32³ uint8 chunks
  (32 KiB) give 3-byte fields and 15-byte entries, 256³ uint8 (16 MiB) give 5 and 17: no overlap with Zarr's
  index there. The filter mask is 0 when no chunk skips a filter, so the 8 bytes after the address are Zarr's
  `nbytes`.
- A Fixed Array holds one entry per chunk of the grid, in row-major order of the chunk's position, the last
  dimension fastest (`H5D__farray_idx_get_addr`: `Σ max_down_chunks[u] × scaled[u]`). Zarr's index is in the
  same order. The HDF5 file format specification (appendix C, VII.C) does not state this order: it follows
  from the library.
- The shard is the whole HDF5 file, so an address is already an offset from the start of the shard, if the
  file's base address is 0: HDF5 addresses are relative to the superblock's base address, which a userblock
  (`H5Pset_userblock`) moves, and every address with it. The chunks are packed from `0x4000` and end where
  `zarrindex` begins.
- With 512 entries the Fixed Array is not paged (pages start above 2¹⁰ entries), so the entries are one
  contiguous 8192-byte run in the data block, 14 bytes after its `FADB` signature.

On all 30 shards the entries parse, both checksums verify, and the entries equal the first 8192 bytes of
`zarrindex` in 29. The 30th, `c/0/0/23`, has 256 of 512 chunks (the last shard along x is 224 elements wide)
and differs only in the size field of the empty slots: HDF5 writes 0, Zarr `2⁶⁴−1`.

## Four ways to write the virtual shard

A shard's value is `[file bytes 0..end] ++ [index]`, with the sharding codec's `index_location` `end`:

- **inline**: the 8192 entry bytes and 4 CRC bytes are in the reference itself (what
  `refkv.shards.write_vzip_sharded(inline_index=True)` does).
- **internal**: the same bytes are an entry of the archive, which the reference points at (vzip: a hidden
  DEFLATE entry; phkv: an ordinary key, deflated, because a standalone store reserves no names).
- **native**: no index bytes are stored. The value is `[file 0..end] ++ [file range of the Fixed Array
  entries] ++ [4 CRC bytes inline]`. The CRC-32C is that of the 8192 bytes the reader sees, which is also
  the CRC a range of those bytes carries ([refkv.md §4.2](../spec/refkv.md#42-range)). An empty slot reads as
  HDF5 writes it.
- **spliced**: native, with Zarr's own encoding of an empty slot. The index is built from ranges of the Fixed
  Array entries for the runs of present chunks, and of one 0xff source, interned once in the archive, for the
  runs of empty slots, so a reader sees `zarrindex`'s bytes exactly, and its CRC. For a full shard that is the
  native value; the partial shard `c/0/0/23` has 128 runs.

## Results

Archives of the 30 shards and `zarr.json`, in bytes (`design/experiments/native_shard_index/`):

| binding | inline | internal | native | spliced |
|---|---:|---:|---:|---:|
| vzip | 496,660 | 111,106 | **5,616** | 7,553 |
| vzip, `mirror_refs=False` | 250,332 | 110,628 | **4,810** | 5,797 |
| phkv | 248,526 | 106,421 | **2,777** | 3,748 |

- Native costs 93 bytes a shard in phkv and 160-187 in vzip: the reference, with its two ranges and the CRC.
- Spliced adds about 1 KB (phkv 971 bytes, vzip 1-2 KB) over native, all of it in the one partial shard. Its
  128 runs cost about 7 bytes each as ranges: with the 0xff runs as literals instead, the same shard cost
  4.9 KB, 60% of its inline index.
- vzip's default writes a reference's payload twice, as the entry's body and in its extra field, so inline
  doubles there; phkv has no mirror.
- The entries deflate badly, from 8196 bytes to about 3.5 KB a shard, so an archive entry for the index
  brings phkv from 248,526 to 106,421 bytes (-57%), and native to 2,777 (-99% of inline, -97% of internal).
- Every archive passes `--verify`, which does not compare archives with each other, whose `[file 0..end]`
  range is shared, but with the source: four regions (three full shards and the partial `c/0/0/23`) decoded
  through zarr-python equal the same regions read directly from the source array over HTTP; and the last 8212
  bytes of every virtual shard, as the archive's store serves them, equal the shard's own last 16 chunk
  bytes followed by the index the variant presents (`zarrindex`, or for native the verbatim entries with
  their CRC), which anchors `end`, the file ranges and the index. All 12 archives pass.

## What it needs, and what it does not show

- **A file written with the new format** (`libver="latest"`, or 1.10 and later, for the Fixed Array). Files
  written with the 1.8 format use a version 1 B-tree: ImarisWriter pins 1.8 (`H5Pset_libver_bounds` in its
  source, and every Imaris sample file checked), and so does netCDF-C 4.9.2 (its source, and a file written
  with it).
- **A chunk-size field of 4 bytes**, so 16-byte entries: uncompressed chunks of 64 KiB to under 16 MiB.
  Other sizes give entries of another width, which are not Zarr's.
- **A base address of 0**, and 8-byte offsets and lengths: a file with a userblock would have every native
  entry point early by the userblock's size.
- **A filter mask of 0 on every chunk**, and fixed-size dimensions (a Fixed Array's condition).
- The script's parser rejects a shard that breaks any of these (`parse_fixed_array`, `load_shard`); a
  zarrifier would have to as well.
- **An empty slot reads as `(2⁶⁴−1, 0)` in native, not Zarr's `(2⁶⁴−1, 2⁶⁴−1)`.** In zarr-python's main branch
  (read, not run), `get_chunk_slice` takes a chunk as missing only if both fields are `2⁶⁴−1` (its vectorized
  path, if the offset is), so a read of an empty chunk inside the array's bounds would reach for byte `2⁶⁴−1`.
  Here every empty slot of the partial shard lies beyond the array's edge, so no read asks for one, and that
  case is untested, as are other readers (tensorstore, zarrs, the browser). Spliced removes the question: a
  reader sees Zarr's own bytes, whatever it does with empty chunks, for about 1 KB on this array.
- **The order is the library's.** It matched on every shard measured, and is as `H5Dfarray.c` computes it.
  Nothing in the format specification promises it.
- **Sizes of one dataset**, with no per-range checksums of the file ranges (the writers' `checksum=` would
  read each shard whole), and no timing or request counts. An inline index is read from the archive, which
  the reader already holds. A native index is a range of the shard's host, so by construction it is one more
  request when a shard is first read. This was not measured.
- **A parser of the Fixed Array is needed** to find the entries (the script's `parse_fixed_array`: superblock,
  layout message, `FAHD`, `FADB`). It reads the first 16 KiB of a file. A zarrifier would put it in the Rust parsers.

## Reproducing

```
uv run --script --locked design/experiments/native_shard_index/compare.py OUT_DIR --verify
```

The script carries its environment as inline metadata (PEP 723), locked in `compare.py.lock`: refkv at PR
141's head `63288330` (for `refkv.phkv`), built from its git source, with the `zarr` extra, and `fsspec` and
`aiohttp` for reading the source array; zarr-python 3.4.2, numpy 2.5.4. `exclude-newer` bounds a re-lock. Building refkv needs a Rust toolchain, and the network
serves the shards. [results.md](experiments/native_shard_index/results.md) has the output.

On macOS 27, the Rust extension did not load (dyld: "mis-aligned LINKEDIT string pool") until built with
`CARGO_PROFILE_RELEASE_STRIP=none`; the run above sets it. When PR 141 has merged, the `rev` can name a
commit of `main`, and the lock file be regenerated with `uv lock --script`.
