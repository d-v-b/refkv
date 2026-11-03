# Neuroglancer precomputed

refkv presents a chunked array store as a Zarr v3 hierarchy by writing a small archive whose chunk
entries point at the store's own objects, without copying them ([overview](../README.md)). This
page is about Neuroglancer's precomputed format.

## Who this is for

Precomputed is the format of connectomics: BANC, FlyWire, FAFB, hemibrain, H01 and MICrONS are
published in it, as petabytes on S3 and Google Cloud Storage. Neuroglancer reads it directly, and
so does tensorstore. People who want those volumes in Zarr tools, or in an analysis that speaks
Zarr, have had to rewrite them, or to scan every shard to build a reference to every chunk.

refkv needs neither. It reads one document, `info`, and the store's listing, and writes an archive
with one reference per object. BANC's 690 million chunks in 87 767 shards become a 15 MB archive in
24 seconds, and a read of a few chunks takes about a second.

## What you get

A Zarr v3 hierarchy whose root is an OME-NGFF 0.5 image, one array per scale, full resolution first.

- **Arrays:** `[z, y, x]`, or `[c, z, y, x]` with more than one channel, so that index `i` is voxel
  `voxel_offset + i`. An unsharded scale's Zarr chunk is one chunk of the volume. A sharded scale's
  is one whole shard, the box of chunks that shard holds: the array reads the shard's own index to
  find a chunk, so a read of one chunk takes three requests, as it does in Neuroglancer.
- **Codec:** `refkv.precomputed`, which refkv's Python package provides. It decodes `raw`, `jpeg`,
  `png` and `compressed_segmentation` chunks, gzip chunks and indexes within shards, and
  chunks stored with a gzip, brotli or Zstandard coding, which it finds from each object's bytes.
  It reads a chunk clipped by the volume's upper bound, with the fill value past the bound.
- **Attributes:** the whole `info` document on the root, as `refkv_zarrified.precomputed.info`, and
  each array's scale on the array. Physical scale and position in nanometers, as OME-NGFF's
  transformations, where a translation places the center of the first voxel, so that the levels of a
  pyramid line up. An `image` also gets channel windows.
- **Other objects:** `provenance`, meshes, skeletons, segment properties and anything else are kept
  whole under `refkv_source/objects/`, each referenced where it is.
- **The store itself:** `refkv_source` is the store's IR mirror
  ([conventions §8.9](../../../spec/conventions.md#89-stores)): the listing (every object's key and
  size) and `info`, byte for byte.

The layout and its reasons are in
[spec/zarrify/precomputed.md](../../../spec/zarrify/precomputed.md).

## Try it

```bash
uv run docs/zarrify/precomputed/example.py
```

```python
import zarr

import refkv.zarr
from refkv.zarrify import zarrify

URL = "https://bossdb-open-data.s3.amazonaws.com/bates_phelps_kim_yang2025/banc/em/"
root = zarr.open_group(refkv.zarr.store(zarrify(URL)), mode="r")
coarsest = root.get_array("6")  # [z, y, x] of the 512 nm level
section = coarsest[3500]
```

Give the store's URL with a trailing `/`. Neuroglancer's own spellings, `precomputed://...` and
`...|neuroglancer-precomputed:`, are not store URLs: give the URL they wrap.

Save the archive to skip the listing the next time:

```bash
uv run python -m refkv.zarrify https://open-neurodata.s3.amazonaws.com/kharris15/oblique/em/ kharris15.vzip
```

## Viewing

Neuroglancer reads the original store as it is, and is the viewer to use for it. The Neuroglancer
fork that opens refkv's archives does not have the codec `refkv.precomputed` yet, so an archive of
this format reads in Python, through zarr-python, and not yet in the demo.

## Supported and not supported

| | |
|---|---|
| Layouts | unsharded, and sharded with the `identity` hash |
| Encodings | `raw`, `jpeg`, `png`, `compressed_segmentation` |
| Data types | `uint8` to `uint64`, `int8` to `int64`, `float32`, `float64` |
| Chunk codings | none, gzip, brotli, Zstandard, per object |
| Shard codings | `raw` or `gzip` for each of chunks and minishard indexes |

These are not presented as arrays, and not lost. The objects stay referenced under
`refkv_source/objects/`, and the root's `refkv_zarrified.precomputed.not_presented` says why, scale
by scale:

- the encodings `compresso`, `fpzip`, `zfpc`, `kempressed` and `jxl`;
- the `murmurhash3_x86_128` hash, which scatters a shard's chunks over the volume. All 8 sharded
  volumes of the 13 surveyed use `identity`;
- shards that wrap, each holding several boxes of chunks;
- a `key` outside the store, and the chunk sizes after a scale's first.

A store of meshes, skeletons or annotations alone has no volume, and is rejected.

A browser reads objects stored with a coding only through `fetch`, which decodes them, and refkv's
browser reader does not accept that yet. Python does ([spec/refkv.md §5.2](../../../spec/refkv.md#52-reading-over-http)).

## Performance

Measured on 2026-11-03 from a home connection, in `us-east-1`'s direction:

| volume | objects | chunks | zarrify | archive | reading a few chunks |
|---|---|---|---|---|---|
| Harris15, raw and unsharded | 766 | 766 | 1.4 s | 136 KB | 1 to 3 s (a whole 4 MB chunk) |
| BANC, JPEG in shards | 87 767 | 690 million | 24 s | 15 MB | about 1 s |

Zarrifying takes the listing's time and one read of `info`. A Zarr chunk of BANC's finest scale is
1.3 GB when decoded, but the codec allocates only what a read selects.

## How it's verified

[python/tests/ir/test_precomputed.py](../../../python/tests/ir/test_precomputed.py) compares every
fixture's arrays with the values its generator wrote and with tensorstore's reading of the same
store, an independent implementation of the format, value for value. `just verify-of precomputed`
does the same through the browser's zarrifier, and with `--remote` checks a region of a public
volume. Harris15 and BANC were checked that way, across scales, the volume's edge included.
