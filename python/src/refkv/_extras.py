"""The optional parts of refkv, and the error that names the extra each one needs.

The base install reads and writes vzip archives, holds a `ReferenceStore`, and
zarrifies image files and stores, without Zarr. The extras add:

- `zarr`: zarr-python, for `refkv.zarr`, which opens a reference store as a
  read-only zarr store (for zarr and xarray), and the codecs that archives of
  zarrified image files and stores use and zarr-python lacks
  (`refkv.zarr.codecs`), which zarr finds through refkv's `zarr.codecs` entry
  points;
- `images`: imagecodecs-zarr, whose codecs the archives of JPEG, JPEG 2000 and other images name
  (docs/zarrify/README.md's table of codecs lists them, from the codec registry,
  spec/zarrify/codecs/), and imagecodecs, whose LZW decoder `refkv.tiff_lzw` calls, with the `zarr`
  extra;
- `media`: the decoders of the `refkv.stream` codec, which reads zarrified MP3,
  FLAC and MP4 files: ffmpeg's MP3, AAC, FLAC, H.264, HEVC, AV1 and VP9 decoders, through PyAV,
  with the `zarr` extra;
- `precomputed`: the decoders of the `refkv.precomputed` codec's `compresso`, `crackle`, `fpzip`
  and `kempressed` encodings, with the `zarr` extra;
- `obstore`: obstore, whose stores (`S3Store`, `GCSStore`, `AzureStore`, ...)
  an application passes as `registry=` to read `s3://`, `gs://` or `az://`
  sources. refkv does not import it: a registry takes any obspec store, and
  refkv reads local files and `http(s)` URLs, archives and sources, itself.
  `OBSTORE_HINT` is what refkv's errors say where such a store is needed;
- `virtualizarr`: VirtualiZarr, for `ReferenceStore.from_virtualizarr`,
  `refkv.convert` and `refkv.shards`, with the `zarr` extra, since they build
  Zarr metadata, and the `obstore` extra, whose stores VirtualiZarr's
  registries hold. refkv does not import xarray: it calls the methods of the
  datasets VirtualiZarr builds.
"""

from __future__ import annotations

OBSTORE_HINT = "obstore's, which refkv's 'obstore' extra installs: pip install 'refkv[obstore]'"
"""Where an error names a store for a bucket, such as `S3Store(...)`: whose it is."""


def missing(extra: str, feature: str, error: ImportError) -> ImportError:
    """The ImportError for `feature` when an optional package of `extra` is missing."""
    name = error.name or str(error)
    e = ImportError(f"{feature} needs {name!r}, which refkv's {extra!r} extra installs: "
                    f"pip install 'refkv[{extra}]'", name=error.name)
    e.__cause__ = error
    return e
