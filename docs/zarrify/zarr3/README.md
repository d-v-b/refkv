# Zarr v3

refkv presents a chunked array store as a Zarr v3 hierarchy by writing a
small archive whose chunk entries point at the store's own objects, without
copying them ([overview](../README.md)). This page is about Zarr v3
hierarchies, in a store or zipped.

## Who this is for

A Zarr v3 store is already the target format, so refkv adds no translation.
What it adds is the archive: one file that indexes a hierarchy on a bucket you
do not own, or inside a zip file, as references, with a record of the store
beside it. That is what VirtualiZarr's `ZarrParser` and `ZippedZarrParser`
give its users, and refkv covers the same inputs
([d-v-b/refkv#271](https://github.com/d-v-b/refkv/issues/271)).

refkv lists the store (or reads the zip file's central directory), reads
every `zarr.json`, and writes an archive in which each node's `zarr.json` is
the store's own and each chunk entry references the existing chunk object
under the same key.

## What you get

A Zarr v3 hierarchy with a node at the path of every node of the store.

- **Arrays:** every member of `zarr.json` as written: data type, fill value,
  chunk grid (`regular`, or zarr-extensions' `rectilinear`), chunk key
  encoding (`default` or `v2`), dimension names and codecs, whatever their
  names. Nothing is decoded, so an array reads from the archive exactly as it
  reads from the store.
- **Sharded arrays:** each shard is one chunk entry, referenced whole; a
  reader's partial reads of a shard (its index, then an inner chunk) are
  ranges of that entry.
- **Attributes:** in place, so OME-NGFF 0.5, xarray and GeoZarr readers see
  them as in the store, with refkv's declaration added. Other conventions'
  CMOs stay in `zarr_conventions`, after refkv's. Only an attribute
  `refkv_zarrified`, and a `zarr_conventions` that is not a list of other
  conventions, move under `refkv_zarrified.zarr3.attributes`.
- **Consolidated metadata** is left out of the root's `zarr.json`; the store's
  document, with it, is in the mirror.
- **Other objects:** any object that is neither a `zarr.json` nor a chunk
  (a README, a Zarr v2 `.zgroup` beside the hierarchy) is kept whole, under
  `refkv_source/objects/`. In a zip file, the mirror rebuilds the whole zip
  file instead.

## Try it

A store is a URL ending in `/`, or a directory with the URL it is served from:

```bash
uv run refkv zarrify fixtures/zarr3/zarr3_hierarchy h.vzip --url https://example.org/h.zarr/
```

A zipped store is a file, and needs no URL:

```bash
uv run refkv zarrify fixtures/zarr3/zarr3_hierarchy.zip h.vzip
```

```python
import zarr
import refkv.zarr

g = zarr.open_group(refkv.zarr.open("h.vzip"), mode="r")
print(g["a"][...])
```

## Supported and not supported

Supported:

- Zarr v3 groups and arrays of 0 to 32 dimensions, on any S3-compatible host
  that answers `ListObjectsV2` anonymously, a Hugging Face Hub repository, or
  a local directory (with `--url`).
- Zip files whose chunks are stored, as zarr-python's `ZipStore` writes them,
  ZIP64 included. A `zarr.json` written twice (zarr-python rewrites a document
  by adding an entry) is read from its last entry, as `zipfile` reads it.
  Deflated `zarr.json` documents are inflated.

Left out, when the store's root is a group: a node whose `zarr.json` is not
valid Zarr v3, or an array with another chunk grid or chunk key encoding, with
storage transformers, or, in a zip file, with a compressed chunk. The rest of
the hierarchy is written, the reason is under
`refkv_zarrified.zarr3.not_presented` on the root, and the node's objects are
kept.

Rejected:

- A store or zip file whose root has no `zarr.json`, or whose root would be
  left out.
- A zip file of a directory store made with `zip -r`, whose entries are under
  the store's directory: zip the store's contents instead.

## How it's verified

`python/tests/ir/test_zarr3.py` zarrifies each fixture over HTTP and compares
every array, its metadata and its attributes with zarr-python reading the
store itself. `rust/refkv-ir/tests/zarr3.rs` checks that every mirror rebuilds
its listing and documents, or its zip file byte for byte. The rules are in
[spec/zarrify/zarr3.md](../../../spec/zarrify/zarr3.md).
