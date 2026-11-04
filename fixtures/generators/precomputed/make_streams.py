"""Writes streams.json: the chunks of the encodings only an encoder can write (compresso, crackle,
JPEG XL, fpzip, zfpc and kempressed), base64 by "<store>/<scale>/<chunk name>", which
write_fixtures.py places in the stores. They are kept as bytes, like its JPEG literals, since an
encoder's output depends on its build. Each is called as cloud-volume calls it
(cloudvolume/chunks.py, encode), the writer these encodings have, but JPEG XL's level and the zfpc
modes, chosen so that a test can state the values: lossless, but for one lossy JPEG XL scale.

Usage: uv run --with zfpc==0.1.2 python fixtures/generators/precomputed/make_streams.py
(with compresso 3.3.3, crackle-codec 0.52.0, fpzip 1.2.5 and imagecodecs 2026.10.10). zfpc is not in
the environment: it needs zfpy, whose version numcodecs, and so zarr, cannot parse on import.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import compresso
import crackle
import fpzip
import imagecodecs
import numpy as np
import zfpc
from write_fixtures import (
    COMPRESSO_VOLUME,
    CRACKLE_VOLUME,
    FLOAT_VOLUME,
    JXL_LOSSLESS,
    JXL_LOSSY,
    boxes,
    chunk_name,
    compresso_label,
    crackle_label,
    value,
)


def volume(lo, hi, channels, dtype, of) -> np.ndarray:
    """The chunk's array (x, y, z, c), Fortran order, as cloud-volume holds it."""
    shape = (hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2], channels)
    out = np.empty(shape, dtype=dtype, order="F")
    for index in np.ndindex(shape):
        x, y, z, c = index
        out[index] = of(lo[0] + x, lo[1] + y, lo[2] + z, c)
    return out


def labels(lo, hi, dtype, of) -> np.ndarray:
    return volume(lo, hi, 1, dtype, lambda x, y, z, _: of(x, y, z))[:, :, :, 0]


def kempressed(subvol: np.ndarray) -> bytes:
    """cloud-volume's encode_kempressed."""
    return fpzip.compress(2.0 + np.swapaxes(subvol, 2, 3), order="F")


def jxl(lo, hi, *, lossless: bool) -> bytes:
    """A gray image of width x and height y × z, as cloud-volume's encode_jpegxl gives one."""
    v = labels(lo, hi, "u1", lambda x, y, z: value(x, y, z))
    image = v.T.reshape(v.shape[2] * v.shape[1], v.shape[0])
    return imagecodecs.jpegxl_encode(image, photometric="GRAY", level=100 if lossless else 90, lossless=lossless,
                                     effort=5, decodingspeed=0)


def main() -> None:
    streams: dict[str, bytes] = {}
    for lo, hi, _ in boxes(*COMPRESSO_VOLUME):
        streams[f"compresso/0/{chunk_name(lo, hi)}"] = compresso.compress(labels(lo, hi, "u4", compresso_label))
    for lo, hi, _ in boxes(*CRACKLE_VOLUME):
        streams[f"crackle/0/{chunk_name(lo, hi)}"] = crackle.compress(labels(lo, hi, "u8", crackle_label))
    for lo, hi, _ in boxes(*JXL_LOSSLESS):
        streams[f"jxl/0/{chunk_name(lo, hi)}"] = jxl(lo, hi, lossless=True)
    for lo, hi, _ in boxes(*JXL_LOSSY):
        streams[f"jxl/1/{chunk_name(lo, hi)}"] = jxl(lo, hi, lossless=False)
    for lo, hi, _ in boxes(*FLOAT_VOLUME):
        name = chunk_name(lo, hi)
        streams[f"fpzip/0/{name}"] = fpzip.compress(
            volume(lo, hi, 2, "f4", lambda x, y, z, c: value(x, y, z, c) / 8), precision=0, order="F")
        streams[f"kempressed/0/{name}"] = kempressed(volume(lo, hi, 3, "f4", lambda x, y, z, c: value(x, y, z, c) / 64))
        # values float32 cannot hold, and each channel its own zfp stream
        streams[f"zfpc/0/{name}"] = zfpc.compress(volume(lo, hi, 2, "f8", lambda x, y, z, c: value(x, y, z, c) / 3))
        streams[f"zfpc_int32/0/{name}"] = zfpc.compress(
            volume(lo, hi, 2, "i4", lambda x, y, z, c: value(x, y, z, c) * 1000003),
            correlated_dims=[True, True, True, False])
    out = {k: base64.b64encode(v).decode() for k, v in sorted(streams.items())}
    (Path(__file__).parent / "streams.json").write_text(json.dumps(out, indent=0) + "\n")


if __name__ == "__main__":
    main()
