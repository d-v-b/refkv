"""The optional parts of refkv, and the error that names the extra each one needs.

The base install reads and writes vzip archives, holds a `ReferenceStore`, and
zarrifies image files and stores, without Zarr. The extras add:

- `zarr`: zarr-python, for `refkv.zarr`, which opens a reference store as a
  read-only zarr store (for zarr and xarray), and registers the codecs that
  archives of zarrified image files and stores use (`refkv.zarr.codecs`);
- `images`: the JPEG, JPEG 2000 and JPEG XR decoders of those codecs
  (imagecodecs), with the `zarr` extra;
- `media`: the decoders of the `refkv.stream` codec, which reads zarrified MP3 and
  MP4 files: ffmpeg's MP3, AAC and H.264 decoders, through PyAV, with the `zarr`
  extra;
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
                    f"pip install 'refkv[{extra}] @ git+https://github.com/d-v-b/refkv#subdirectory=python', "
                    f"or from a clone, pip install './python[{extra}]'", name=error.name)
    e.__cause__ = error
    return e
