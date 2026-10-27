# The shard index an HDF5 file already holds

*Measured 2026-10-27, against refkv at `63288330` (PR 141, which adds `refkv.phkv`). One dataset.*

A file that is both an HDF5 file and a Zarr v3 shard holds its shard index twice: as the Zarr
`zarrindex` and as the chunk index of the HDF5 dataset. For a file written with HDF5's new format, the two
are the same 8 KiB, so a virtual shard can reference the HDF5 copy instead of storing a copy of its own.
This note measures what that saves in vzip and phkv archives, and what it needs of the file.

## The dataset

`s3://shroff-lab/s0_hdf5/` (https://s3.janelia.org/shroff-lab/s0_hdf5/) is a Zarr v3 array of 6368 x 1600
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

- The chunk-size field is `1 + (log2(chunk bytes) + 8) / 8` bytes (`H5Dfarray.c`, for layout version 4):
  4 bytes for 64³ uint8 chunks, so an entry is 16 bytes, as Zarr's is. The filter mask is 0 when no chunk
  skips a filter, so the 8 bytes after the address are Zarr's `nbytes`.
- A Fixed Array holds one entry per chunk of the grid, in row-major order of the chunk's position, the last
  dimension fastest (`H5D__farray_idx_get_addr`: `Σ max_down_chunks[u] × scaled[u]`). Zarr's index is in the
  same order. The HDF5 file format specification (appendix C, VII.C) does not state this order: it follows
  from the library.
- The shard is the whole HDF5 file, so an address is already an offset from the start of the shard. The
  chunks are packed from `0x4000` and end where `zarrindex` begins.
- With 512 entries the Fixed Array is not paged (pages start above 2¹⁰ entries), so the entries are one
  contiguous 8192-byte run in the data block, 14 bytes after its `FADB` signature.

On all 30 shards the entries parse, both checksums verify, and the entries equal the first 8192 bytes of
`zarrindex` in 29. The 30th, `c/0/0/23`, has 256 of 512 chunks (the last shard along x is 224 elements wide)
and differs only in the size field of the empty slots: HDF5 writes 0, Zarr `2⁶⁴−1`.

## Three ways to write the virtual shard

A shard's value is `[file bytes 0..end] ++ [index]`, with the sharding codec's `index_location` `end`:

- **inline**: the 8192 entry bytes and 4 CRC bytes are in the reference itself (what
  `refkv.shards.write_vzip_sharded(inline_index=True)` does).
- **internal**: the same bytes are an entry of the archive, which the reference points at (vzip: a hidden
  DEFLATE entry; phkv: an ordinary key, deflated, because a standalone store reserves no names).
- **native**: no index bytes are stored. The value is `[file 0..end] ++ [file range of the Fixed Array
  entries] ++ [4 CRC bytes inline]`. The CRC-32C is that of the 8192 bytes the reader sees, which is also
  the CRC a range of those bytes carries (refkv.md §4.2).

## Results

Archives of the 30 shards and `zarr.json`, in bytes (`design/experiments/native_shard_index/`):

| binding | inline | internal | native |
|---|---:|---:|---:|
| vzip | 496,660 | 111,106 | **5,616** |
| vzip, `mirror_refs=False` | 250,332 | 110,628 | **4,810** |
| phkv | 248,526 | 106,421 | **2,777** |

- Native costs 93 bytes a shard in phkv and 160-187 in vzip: the reference, with its two ranges and the CRC.
- vzip's default writes a reference's payload twice, as the entry's body and in its extra field, so inline
  doubles there; phkv has no mirror.
- The entries deflate badly, from 8196 bytes to about 3.5 KB a shard, so an archive entry for the index
  brings phkv from 248,526 to 106,421 bytes (-57%), and native to 2,777 (-99% of inline, -97% of internal).
- Four regions, three full shards and the partial `c/0/0/23`, decode to the same bytes through zarr-python in
  all nine archives.

## What it needs, and what it does not show

- **A file written with the new format** (`libver="latest"`, or 1.10 and later, for the Fixed Array). Files
  written with the 1.8 format use a version 1 B-tree: ImarisWriter pins 1.8 (`H5Pset_libver_bounds` in its
  source, and every Imaris sample file checked), and so does netCDF-C 4.9.2 (its source, and a file written
  with it).
- **A filter mask of 0 on every chunk**, and fixed-size dimensions (a Fixed Array's condition). The parser
  of the script rejects a shard that breaks either.
- **An empty slot reads as `(2⁶⁴−1, 0)`, not Zarr's `(2⁶⁴−1, 2⁶⁴−1)`.** In zarr-python's main branch (read, not
  run), `get_chunk_slice` takes a chunk as missing only if both fields are `2⁶⁴−1` (its vectorized path, if
  the offset is). A read
  of an empty chunk inside the array's bounds would reach for byte `2⁶⁴−1`. Here every empty slot of the
  partial shard lies beyond the array's edge, so no read asks for one. This case is **untested**, as are
  other readers (tensorstore, zarrs, the browser).
- **The order is the library's.** It matched on every shard measured, and is as `H5Dfarray.c` computes it.
  Nothing in the format specification promises it.
- **Sizes of one dataset**, with no per-range checksums of the file ranges (the writers' `checksum=` would
  read each shard whole), and no timing or request counts. An inline index is read from the archive, which
  the reader already holds. A native index is a range of the shard's host, so by construction it is one more
  request when a shard is first read. This was not measured.
- **A parser of the Fixed Array is needed** to find the entries (the script's `parse_fixed_array`: layout
  message, `FAHD`, `FADB`). It reads the first 16 KiB of a file. A zarrifier would put it in the Rust parsers.

## Reproducing

```
uv run python design/experiments/native_shard_index/compare.py OUT_DIR --verify
```

[results.md](experiments/native_shard_index/results.md) has the output. The script needs refkv with PR 141
(`refkv.phkv`) and the `zarr` extra, and the network. On macOS 27, the Rust extension built by `uv sync`
did not load (dyld: "mis-aligned LINKEDIT string pool") until rebuilt with `CARGO_PROFILE_RELEASE_STRIP=none`.
