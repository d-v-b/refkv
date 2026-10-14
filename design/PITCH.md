# refkv: an experiment in reference-aware key-value storage

**refkv is an experiment, not a product.** If you need to virtualize Zarr
data today, use the established tools: [Icechunk](https://icechunk.io/)
for production datasets, and [kerchunk](https://github.com/fsspec/kerchunk)
where a static set of references read from Python is enough. Both are
maintained, documented and widely used, and VirtualiZarr writes both.

This document describes two things and keeps them apart. **refkv** is the
model: a reference value type for key-value stores, specified on its own
without any backend. **vzip** is the one implementation of that model the
specification binds: a store kept in a single ZIP file.

## The question

Petabytes of scientific data sit in HDF5, netCDF, GRIB and TIFF files on
object storage. [kerchunk](https://github.com/fsspec/kerchunk) showed that
Zarr readers can read those files in place, without copying them, if someone
writes down where each chunk lives: *this chunk is bytes 4,096–135,000 of
`s3://bucket/file.nc`*. This is "virtual Zarr".
[VirtualiZarr](https://github.com/zarr-developers/VirtualiZarr) grew out of
kerchunk and makes virtual datasets easy to build and combine with xarray.
refkv exists because of both projects; see
[Where the ideas come from](#where-the-ideas-come-from).

A virtual dataset is a Zarr store in which some keys hold real bytes
(metadata, small coordinate arrays) and others hold "go read these bytes
over there". The established ways to store that each come with an
implementation and an ecosystem:

- **kerchunk JSON** is one JSON document, read by fsspec's reference
  filesystem.
- **kerchunk Parquet** is a compact directory of Parquet files, also read by
  fsspec.
- **Icechunk** is a versioned, transactional repository with a Rust library,
  Python bindings and cloud backends. It is the most complete of the three
  and the one to reach for in production.

In each of them, "a value may be a reference" is a property of one format
and its reader. The experiment asks whether it can instead be a property of
key-value storage in general: a reference value type, specified without a
backend, such that any store that can tell bytes from references can hold
one, and such that one concrete binding could be read from the specification
in an afternoon, in any language, with tools already at hand. And it asks
what that costs against the established formats. refkv is the model, vzip is
the binding, and the rest of this document, [COMPARISON.md](COMPARISON.md)
and [FINDINGS.md](FINDINGS.md) are the findings.

## The model: refkv

refkv ("reference key value") is a **reference value type**. A store is a
key-value mapping in which every value is either bytes or a **reference**: a
description of how to assemble the value from byte ranges of other objects.
That is the whole data model ([spec/refkv.md
§2](../spec/refkv.md#2-data-model)). Part 1 of the
[specification](../spec/refkv.md) defines everything that follows from it
without a backend:

- **References** are a few bytes of protobuf. A `Range` names bytes
  `[offset, offset + length)` of one source; a `Concat` joins ranges
  ([§4](../spec/refkv.md#4-messages)).
- **Sources** are listed once in a **source table** and named by number.
  A source is an external object named by URL, another value of the same
  store, or literal bytes the table carries
  ([§5](../spec/refkv.md#5-source-table)).
- **Pins** let a source record the size, ETag or modification time of the
  object it names, so that a reader can refuse bytes that have changed
  ([§5.1](../spec/refkv.md#51-pins)).
- **Reading** is specified down to the errors and the reader policy
  ([§6](../spec/refkv.md#6-reading)), and **writing** down to the checks a
  writer must make ([§7](../spec/refkv.md#7-writing)).

Nothing in the model depends on Zarr. Keys are opaque strings; Zarr is the
motivating case, not a dependency. Nothing in it depends on a backend
either. A **binding** to a backend has to answer only three questions
([§3](../spec/refkv.md#3-bindings)): how a value is marked as a reference,
where the source table is kept, and how keys map to the backend's names. An
object store could answer them with object metadata, a file system with an
extended attribute, a database with a column
([§3.2](../spec/refkv.md#32-other-backends-informative)).

## The implementation: vzip

vzip is the ZIP binding of refkv, and the one binding the specification
makes normative ([Part 2](../spec/bindings/vzip.md#part-2-the-zip-binding)).
A **vzip archive** is a refkv store kept in one ordinary ZIP file:

- Bytes keys (metadata, small arrays) are normal ZIP entries.
- Reference keys are ZIP entries whose directory record carries a small
  extra field holding the reference.
- The source table is one more entry, which the archive comment locates.

A program that has never heard of refkv still sees a valid archive:

- `unzip -l` lists every key;
- `unzip -p` prints the metadata;
- reference keys show up as small files holding their encoded reference.

A vzip reader sees the store.

### Why ZIP first

The model could be bound to many backends. ZIP is the one the experiment
binds it to, because:

- **It is already cloud-optimized.** The index (the central directory) is at
  the end of the file. One range request for the tail finds everything else.
  ZIP is supported almost everywhere: every language, every OS, every archive
  tool.
- **It is one object.** Copy it, cache it, checksum it, put it behind a CDN,
  attach it to a DOI. There is no directory layout to keep consistent.
- **The overlay comes for free.** Whether a key is bytes or a reference is
  decided from its own directory record. No second index has to agree with
  the first.
- **Nobody owns it.** ZIP is documented by PKWARE's APPNOTE, and protobuf's
  wire format by a public spec. The vzip rules on top are [one
  document](../spec/bindings/vzip.md). Every archive records its format
  version (`vzip/0` today), so a reader knows whether it can read an archive
  before it tries.

### What the implementation is used for

Two things in this repository write refkv stores, both as vzip archives:

- **Virtual datasets from VirtualiZarr.** The Python converters take a
  VirtualiZarr dataset of chunk manifests and write its Zarr v3 metadata and
  one reference per chunk. This is the path [COMPARISON.md](COMPARISON.md)
  measures.
- **Virtualizers for image formats and stores.** A Rust core reads the
  structure of TIFF, NDPI, ND2, CZI, DICOM, NIfTI and Imaris files, of N5
  and Zarr v2 stores, and of Sentinel-2 SAFE products, and writes a store
  that presents the pixels as OME-Zarr, with every chunk a reference to the
  original bytes ([spec/virtualize.md](../spec/virtualize.md)).

Both are exercises of the model. Neither is a reason to adopt it.

## Status

Format version 0 is **experimental**. The [specification](../spec/refkv.md)
is believed complete, but it can change without regard for existing
archives, and it has no production users, release history or support. The
implementations in this repository are reference implementations written to
test the specification, not libraries to build on. Changes are recorded in
the [changelog](../spec/history/archive/REVISIONS.md). Feedback is welcome;
dependence is not advised.

## Where the ideas come from

refkv is an encoding of ideas that kerchunk and VirtualiZarr worked out.
Very little in its data model is new.

- **[kerchunk](https://github.com/fsspec/kerchunk)** (Martin Durant and
  contributors, part of the fsspec project) introduced virtual Zarr. It scans
  HDF5, netCDF, GRIB, TIFF and FITS files for the locations of their chunks,
  and fsspec's `ReferenceFileSystem` serves those references so that Zarr
  reads the files as if they were Zarr. refkv stores the mapping that
  kerchunk's [reference specification](https://fsspec.github.io/kerchunk/spec.html)
  defines:
  - **The data model:** a key's value is either inline data or
    `[url, offset, length]`. These are refkv's bytes values and `Range`
    references.
  - **Inline binary data:** values prefixed `base64:` carry binary data
    inline. These are refkv's literal ranges and `data` sources.
  - **Shared URLs:** `templates` let many references share one URL. In
    refkv, references name an entry of the source table by number.
  - **Binary storage:** the version 1 specification already anticipated
    "future possible binary storage" of references. refkv is one such
    encoding.

  kerchunk's Parquet layout showed how compact references can be:
  dictionary-encoded paths, columns of offsets and lengths, read lazily in
  pieces. It is the bar vzip's size and request counts are measured against
  in [COMPARISON.md](COMPARISON.md).
- **[VirtualiZarr](https://github.com/zarr-developers/VirtualiZarr)**
  (started by Tom Nicholas, now a zarr-developers project) recast virtual Zarr
  in Zarr's own terms. It grew out of discussions on kerchunk
  ([fsspec/kerchunk#377](https://github.com/fsspec/kerchunk/issues/377)).
  - **Chunk manifests:** VirtualiZarr's chunk manifests (a path, offset and
    length for every chunk of an array) are what refkv's references record.
  - **The code:** the Python converters
    ([convert.py](../python/src/refkv/convert.py), [shards.py](../python/src/refkv/shards.py)) take
    VirtualiZarr's `ManifestArray` datasets as input and reuse helpers from
    its Icechunk writer. The benchmarks build their datasets with its
    `ChunkManifest`.
  - **The open problem:** its roadmap names a Zarr-native on-disk chunk
    manifest format as future work
    ([zarr-specs#287](https://github.com/zarr-developers/zarr-specs/issues/287),
    Joe Hamman's manifest storage transformer proposal). vzip explores one
    container for such manifests.
  - **Virtual shards:** a virtual shard stores a source file's manifest as a
    Zarr shard index, so the manifest idea works inside Zarr's own sharding
    codec.
- **[Icechunk](https://icechunk.io/)** (Earthmover) checks virtual chunks
  against the ETag or modification time of the object they point into.
  refkv's source pins are the same idea.

What refkv adds is narrow: a reference value type with a binary encoding,
specified apart from any backend, and one binding of it to a single file
(ZIP), defined by one document with a conformance suite, so that references
can be read without fsspec, Python, or a particular storage engine. It is a
study of that one idea, not a replacement for these tools. If the idea turns
out to be useful, the right home for it is probably inside them, for example
as one more VirtualiZarr output format, rather than as a separate project.

## What it costs

[COMPARISON.md](COMPARISON.md) measures vzip against kerchunk JSON, kerchunk
Parquet and Icechunk on the same virtual dataset, read over HTTP in several
access patterns, with every request counted. In short: with a page index,
vzip opens and reads single values and maps as fast as the others; it is the
largest of the four on disk and the slowest to write; and a read that
crosses every page of its index, such as a time series, is slower than the
others. Those are the costs of the ZIP binding, measured so that the
experiment is honest about them. The model itself has no cost to measure;
only a binding does.

"Virtual shards" are a usage pattern the experiment also tried: one
reference per source file, made of the file's bytes followed by a standard
Zarr shard index, so that Zarr's own sharding codec does the per-chunk
lookup. They make the archive far smaller, at the cost of requiring source
files that tile a regular grid.

## What refkv is not

- **Not a production tool.** It has no users, no releases, no support, and
  a specification that may change incompatibly. Use Icechunk or kerchunk for
  anything you or others depend on.
- **Not a version-control system.** It has no commits, branches or
  transactions. To change a store, write a new one. Icechunk has history,
  concurrent writers and time travel, and is the right choice when a dataset
  changes.
- **Not tied to Zarr.** Keys are strings and values are bytes. Zarr is the
  motivating case, not a dependency.
- **Not tied to ZIP.** vzip is one binding. The model is specified so that
  other backends could hold the same store; none has been built.
- **Not a replacement for real Zarr data.** A reference points at bytes that
  live in other formats, with their chunking and compression.

## Who this is for

- **Data producers** who want to publish a virtual view of an archive
  should use Icechunk, or kerchunk through VirtualiZarr. Not refkv.
- **People designing reference formats**, in the Zarr community or
  elsewhere, may find the specification, the conformance suite and the
  measurements useful as evidence about what a backend-independent reference
  model, and a small single-file binding of it, can and cannot do.
- **Anyone curious** whether a ZIP file can carry a virtual dataset can read
  the specification and try the reference implementations, knowing they are
  experiments.

## Open questions

- **Stale references.** A reference points at bytes the store doesn't
  control. If a producer regenerates a file in place, a reference written
  earlier silently returns the new file's values; this was measured with
  netCDF files whose layout didn't change. Icechunk guards against this by
  pinning each reference to the object's ETag or last-modified time, and
  checking with a conditional request. refkv took the same idea as source pins
  ([spec/refkv.md §5.1](../spec/refkv.md#51-pins), since revision 3), per
  source rather than per chunk. What remains open is that browsers often
  cannot check pins: servers must expose the headers to cross-origin pages
  ([open feedback](../spec/history/archive/REVISIONS.md)).
- **Updates.** Appending a day of data means rewriting the archive, or
  stacking archives where later ones shadow earlier ones (not yet designed).
- **Other bindings.** The model claims any store that can tell bytes from
  references can hold one. Only the ZIP binding exists, so the claim is
  untested.
- **Ragged sources.** Virtual shards need source files that tile a regular
  grid. Zarr's newer rectilinear chunk grids may lift that restriction.
