# HDF5 and NWB

refkv turns an image file into a Zarr hierarchy by writing a small archive of
references to the file's bytes, without copying the data
([overview](../README.md)). This page is about HDF5 files, and NWB files
(Neurodata Without Borders 2.x) first among them.

## Who this is for

NWB is the standard format of neurophysiology: the DANDI Archive holds
hundreds of terabytes of it, in HDF5 files from a few hundred kilobytes
(spike times and a trials table) to tens of gigabytes (raw electrophysiology
and two-photon movies). Reading one over HTTP through h5py makes a request for
every pointer of the file's metadata, and a file of thousands of objects can
take minutes before the first value; converting it to Zarr copies it. LINDI,
kerchunk and VirtualiZarr reference HDF5 chunks already, through h5py and
libhdf5, without a specification, and each copies the file's strings into its
reference file.

refkv reads the HDF5 structure itself, in rounds of requests that fetch
every structure a round needs at once, and writes an archive in which every
group is a Zarr group and every dataset a Zarr array at the same path, its
chunks byte ranges of the file. The attributes, links and object references
are in the form hdmf-zarr 0.14 writes and reads, so pynwb's `NWBZarrIO`
opens a zarrified NWB file as an NWB file. Any other HDF5 file (a netCDF-4
file, an instrument's output) gets the same hierarchy; an Imaris file is
the [IMS profile](../ims/README.md)'s.

## What you get

A Zarr v3 hierarchy that mirrors the file's:

- **Groups** are groups, with the HDF5 attributes as Zarr attributes. Soft
  links and second hard links are `_LINKS` (`[{"name", "source", "path"}]`),
  as hdmf-zarr writes them; an object reachable by several paths is
  presented once, at the first path in the order of the links' names.
- **Datasets** are arrays: integers, floats, h5py's `bool`, enumerations
  (their base integers), packed compounds as the `struct` data type, and
  fixed-length strings as bytes with a trailing axis. Their chunks are:
  - contiguous data cut into chunks of at most 1 MiB along the slowest axis;
  - compact data, one range in the object header;
  - chunked data, HDF5's own chunks, with `numcodecs.zlib` for deflate and
    `numcodecs.shuffle` for shuffle.
- **Variable-length strings** are `string` arrays with the `vlen-utf8`
  codec, each chunk a list of byte ranges of the file's global heap behind
  literal lengths: no string is copied.
- **Object references** are `string` arrays of paths with
  `_DTYPE: "object_reference"`, and reference attributes are
  `{"_REFERENCE": {"source": ".", "path": ...}}`: NWB's
  `DynamicTableRegion`s, `VectorIndex` targets, electrode groups and
  `.specloc`.
- **Compounds with string or reference members** (an NWB trials table's
  `timeseries` column) are `struct` arrays whose strings and paths are
  fixed-length UTF-32, written into the archive, as hdmf-zarr writes them.
- **`/specifications`**, the NWB schema an NWB file caches, is kept as its
  scalar string arrays, which `NWBZarrIO` loads.
- **Source metadata:** each node's `refkv_zarrified.hdf5` holds its object
  header's address, each dataset's HDF5 datatype, layout, chunk index,
  filters and fill value, and each attribute's HDF5 datatype. A group lists
  the children and attributes it does not present, each with the reason;
  the whole file is described on `refkv_source` by the IR mirror, from
  which it is rebuilt byte for byte.

**What is not presented**, and recorded instead: region references, opaque,
time, array and sequence datatypes, compounds with gaps, filters other than
deflate and shuffle, chunks a filter skipped, virtual datasets, datasets in
external files, and external links (not followed).
[spec/zarrify/hdf5.md §7.4](../../../spec/zarrify/hdf5.md#74-what-is-not-presented)
lists them.

## Try it

[example.py](example.py) zarrifies an NWB file of the DANDI Archive, opens it with
`NWBZarrIO`, and plots eight ROIs' traces. Its dependencies are in its header
(PEP 723), so it runs on its own (uv builds refkv from GitHub, which needs a
Rust toolchain, until a release on PyPI has `refkv.zarrify`):

```bash
uv run docs/zarrify/hdf5/example.py
```

Dandiset 000054, a two-photon session (NWB 2.9.0, 81 MB, every dataset
contiguous), from [corpus_hdf5.txt](../../../conformance/zarrify/corpus_hdf5.txt):

```bash
uv run refkv zarrify https://dandiarchive.s3.amazonaws.com/blobs/5ca/ea3/5caea343-caf7-4914-aa51-6379f9524c3b ophys.vzip
```

```
{"format": "hdf5", "objects": 135, "groups": 50, "arrays": 85, "unpresented": {}, "strings": 46, "references": 0, "links": {"soft": 6, "hard": 0, "external": 0}, "chunks": 0, "nwb": {"version": "2.9.0", "namespaces": {"core": ["2.9.0"], "hdmf-common": ["1.8.0"], "hdmf-experimental": ["0.5.0"]}}, "rounds": 27, ...}
```

Zarrifying took 9 seconds and read 390 KB of the file in 136 requests and
27 rounds; the archive is 210 KB. Read it as NWB:

```python
from hdmf_zarr.nwb import NWBZarrIO
import refkv.zarr

with NWBZarrIO(refkv.zarr.open("ophys.vzip"), "r") as io:
    nwb = io.read()
    series = nwb.processing["ophys"]["dF"]["dF"]
    print(nwb.session_description, series.data.shape, series.rate)
    print(series.data[:3, :4])
```

```
Preprocessed 2P and VR Data (22769, 161) 15.4609
[[0.11049788 0.3076745  0.13088487 0.07162745]
 [0.08992036 0.28434584 0.12397016 0.07158256]
 [0.0556308  0.24342182 0.1132441  0.07255694]]
```

These are the values `NWBHDF5IO` reads from the file; the three rows came from
one 1 MiB chunk (1628 rows of 161 float32), one range request.

**At scale.** An IBL raw electrophysiology file of 51 GB (dandiset 000409:
176 objects, 10,367 chunks) zarrifies in 28 rounds; an Allen Brain
Observatory file of 9,125 objects, of dandiset 000728, whose metadata h5py
over HTTP did not finish reading in four minutes, in 28 rounds as well.

**A known limit.** A float attribute is written as Zarr v3 writes a float
fill value: NaN and the infinities as the strings `"NaN"`, `"Infinity"` and
`"-Infinity"`, and a NaN with other bits as its bits in hex (`"0x7fc00001"`),
since JSON has no such numbers. The convention requires a reader to decode a
float attribute so, by the HDF5 datatype the node's source metadata records
for it
([spec/zarrify/hdf5.md §6](../../../spec/zarrify/hdf5.md#6-attributes)).
hdmf-zarr does not yet, and pynwb refuses the string: the Allen Brain
Observatory files of dandiset 000728, whose stimulus movies have a `rate` of
NaN, zarrify but do not open in `NWBZarrIO`.

## Supported and not supported

Supported:

- Superblocks 0 to 3; old (symbol table) and new (compact and dense) groups,
  soft links in either; compact and dense attributes; every chunk index
  (version 1 and 2 B-trees, single chunk, implicit, fixed array, extensible
  array).
- Contiguous, compact and chunked layouts, unallocated storage and chunks
  (the fill value), nonzero fill values.
- The data types above, both byte orders.

Rejected:

- Offsets or lengths of other than 8 bytes, a base address other than 0, a
  superblock after a user block.
- Structures that do not read: truncated files, chunks outside the file, a
  string that names no global heap object or one of another length.
- More than 131,072 objects, objects more than 64 links deep, more than
  2^20 strings, a dataset of strings, references or records over 64 MiB.

## Performance

- Zarrifying reads the metadata in rounds, each fetching every structure the
  last one named: about 25 to 30 rounds for an NWB file whatever its size,
  plus the pointers of its string and reference datasets.
- Reading a contiguous dataset's chunk is one range request of at most
  1 MiB; a chunked dataset's, HDF5's chunk as stored.
- The archive has one entry per chunk: 158 for the file above, 25,654 for an
  Allen probe file whose LFP is chunked one channel at a time.

## How it's verified

`python/tests/ir/test_hdf5.py` zarrifies the synthetic HDF5 and NWB files of
[fixtures/hdf5/](../../../fixtures/hdf5/) (written with h5py, h5netcdf and
pynwb by
[the generator](../../../fixtures/generators/hdf5/write_fixtures.py)) and
compares every dataset with h5py's values, every reference with h5py's
dereferenced objects, and each NWB file read by `NWBZarrIO` with the same
file read by `NWBHDF5IO`. The Rust tests rebuild every fixture byte for byte
from its mirror; `test_zarrify_conventions.py` checks that the browser
implementation's output is the same. The public files of
[corpus_hdf5.txt](../../../conformance/zarrify/corpus_hdf5.txt) were checked
against h5py the same way.

- Convention and profile: [spec/zarrify/hdf5.md](../../../spec/zarrify/hdf5.md)
- Rust core: [rust/refkv-ir/src/hdf5/](../../../rust/refkv-ir/src/hdf5/) (the reader, the walk, the datatypes, the profile), the projection [rust/refkv-ir/src/project/hdf5.rs](../../../rust/refkv-ir/src/project/hdf5.rs)
- Fixtures: [fixtures/hdf5/](../../../fixtures/hdf5/)
- Design: [design/plans/nwb.md](../../../design/plans/nwb.md)
