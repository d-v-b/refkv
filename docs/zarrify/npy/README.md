# NumPy

refkv turns a file into a Zarr hierarchy by writing a small archive of references to the
file's bytes, without copying its values ([overview](../README.md)). This page is about
NumPy's own files: `.npy` files and `.npz` archives.

## Who this is for

`np.save` and `np.savez` are how a great deal of scientific data is passed around: model
weights and embeddings, simulation outputs, preprocessed images, the arrays of a paper's
supplementary data. Reading one remotely means downloading it whole, and putting one next to
Zarr data means converting it. A `.npy` file is a short header and one block of values, and
an `.npz` archive is a ZIP file of them, so refkv reads the headers and writes an archive in
which every Zarr chunk is a run of at most 1 MiB of that block. A reader then fetches only the
chunks it slices. An archive `np.savez_compressed` wrote holds each array deflated, which can
only be read from its start, so each of its arrays is one chunk.

## What you get

A Zarr group at the archive root (not an OME-Zarr image: a NumPy array has no axes to name),
with one array per NumPy array:

- **A `.npy` file:** one array, `array`.
- **A `.npz` archive:** one array per member, named as `np.load` names it (`x.npy` is `x`).
  A member that cannot be presented is listed with the reason, below.
- **Data types:** `bool`, the integers and floats (`float16` to `float64`), `complex64` and
  `complex128`, `datetime64` and `timedelta64` with a unit, `U<n>` strings
  (`fixed_length_utf32`), structured types of those (`struct`), in either byte order.
  Fixed-length bytes and voids (`S<n>`, `V<n>`) are `uint8` with a trailing axis of their size.
- **Order:** a Fortran-order array has the same shape, with a `transpose` codec: its chunks
  are the file's bytes in the file's order.
- **Compressed archives:** a member that `np.savez_compressed` deflated is one chunk, the
  whole array: its deflate data, wrapped as a gzip stream, decoded by `gzip` and then
  `refkv.skip`, which drops the NPY header the stream begins with. Reading any part of the
  array reads and inflates all of it.
- **Chunks:** the values as the file stores them, cut into equal runs of at most 1 MiB along
  the outermost axis that allows it (the innermost, for Fortran order). An array of rank 0 is
  one chunk; an empty one has none.
- **Dimension names:** `dim_0`, `dim_1`, …, as xarray names a NumPy array's axes, and `byte`
  for the trailing axis of bytes.
- **Source metadata:** each array's header (format version, `descr`, `fortran_order`,
  `shape`, the data's offset) under `refkv_zarrified.npy`, and for an archive every member:
  its name, compression method, sizes, and its path or the reason it is not presented. The
  whole file, the ZIP structures included, is described on `refkv_source` by the Rust core's
  IR mirror, from which it is rebuilt byte for byte.

**Not presented.** An archive's member is left out, and listed with its reason, when it is
compressed by a method other than deflate (bzip2, LZMA; `method`), is not a
`.npy` member (`not_npy`), has a name that is not a Zarr node name (`name`, `reserved`), has a
header refkv cannot read (`header`), has a data type Zarr cannot hold (object arrays, which
are pickles, and long doubles; `datatype`), or is truncated (`size`). A `.npy` file of such an
array, and an archive with no member to present, are rejected.
[spec/zarrify/npy.md §7](../../../spec/zarrify/npy.md#7-what-the-layout-does-not-express)
lists what the layout does not express.

## Try it

[example.py](example.py) saves an archive with `np.savez`, zarrifies it, and plots one of its
arrays with matplotlib, read through Zarr. Its dependencies are in its header (PEP 723), so it runs on its own, with refkv from PyPI:

```bash
uv run docs/zarrify/npy/example.py
```

A `.npy` file from NumPy's own test data, written by Python 2 on 64-bit Windows, from
[corpus_npy.txt](../../../conformance/zarrify/corpus_npy.txt):

```bash
uv run refkv zarrify https://raw.githubusercontent.com/numpy/numpy/2f7fe64b8b6d7591dd208942f1cc74473d5db4cb/numpy/lib/tests/data/win64python2.npy win64python2.vzip
```

```
{"format": "npy", "kind": "npy", "arrays": [{"path": "array", "dataType": "float64", "shape": [2], "order": "C", "chunkShape": [2], "chunks": 1}], "unpresented": {}, ...}
```

Read it:

```python
import zarr
import refkv.zarr

g = zarr.open_group(refkv.zarr.open("win64python2.vzip"), mode="r")
print(g.attrs["refkv_zarrified"]["npy"]["array"])
print(g["array"][...])
```

```
{'version': [1, 0], 'descr': '<f8', 'fortran_order': False, 'shape': [2], 'offset': 80}
[1. 1.]
```

These are `np.load`'s values. An archive's arrays are its members; with the `arrays.vzip`
that example.py writes:

```python
g = zarr.open_group(refkv.zarr.open("arrays.vzip"), mode="r")
print(sorted(g.array_keys()))
print([(m["name"], m.get("path", m.get("reason"))) for m in g.attrs["refkv_zarrified"]["npy"]["members"]])
```

```
['image', 'profile']
[('image.npy', 'image'), ('profile.npy', 'profile')]
```

The archive is 5.5 KB, for a 965 KB `.npz`.

## Supported and not supported

Supported:

- `.npy` files of format versions 1, 2 and 3, C or Fortran order, either byte order, and the
  data types above.
- `.npz` archives from `np.savez` (stored members) and `np.savez_compressed` (deflated
  members), ZIP64 included.

Not presented, each member listed with its reason: members compressed otherwise, members that
are not `.npy`, names that are not Zarr node names, unreadable headers, object arrays and long
doubles, and truncated members.

Rejected:

- A `.npy` file whose header does not read, of a data type Zarr cannot hold, or shorter than
  its data ([spec §8.2](../../../spec/zarrify/npy.md#82-an-npy-file)).
- An archive with no member to present.

## How it's verified

`python/tests/ir/test_npy.py` zarrifies every synthetic file and archive in
[fixtures/npy/](../../../fixtures/npy/) (written by NumPy and `zipfile`,
[fixtures/generators/npy/write_fixtures.py](../../../fixtures/generators/npy/write_fixtures.py))
and compares every presented array with `np.load`'s, value for value, in its shape and data
type; it checks the members an archive leaves out and their reasons, the chunk shapes, and the
copied edge chunks. `js/test/npy/verify.py` does the same for the browser code. Every archive's
mirror rebuilds its file, and the inputs the profile rejects are listed with their messages in
[conformance/zarrify/rejects/npy.json](../../../conformance/zarrify/rejects/npy.json).

- Profile: [spec/zarrify/npy.md](../../../spec/zarrify/npy.md#part-2-the-profile)
- Rust core: [rust/refkv-ir/src/npy/](../../../rust/refkv-ir/src/npy/)
