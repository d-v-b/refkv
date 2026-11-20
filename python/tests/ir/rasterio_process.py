"""rasterio in a process of its own, for the tests that compare with GDAL: the SAFE tests'
(test_safe_stored.py) reference reader of a band file's image, and the writer of the band files no
fixture holds; and the ENVI tests' (test_envi.py, test_envi_stored.py) reference reader.

GDAL's libraries and imagecodecs' do not share a process: with rasterio loaded first, imagecodecs'
LERC codec finds GDAL's LERC library and fails, in the TIFF tests that run after. So the tests run
this script, give it a request as JSON on its standard input, and read the arrays it writes, an
`.npz` archive, from its standard output:

- `python rasterio_process.py read`: of each `{"path", "offset", "length", "windows"}`, a JP2 file
  held in a range of a file, the image rasterio reads whole (`image_<i>`, its bands first) and in
  each window `[column, row, width, height]` (`window_<i>_<j>`), with, in `about`, a JSON list, the
  driver that read it and the shapes of the blocks GDAL reads it in;
- `python rasterio_process.py write`: of each `{"rows", "columns", "tile", "samples", "bits",
  "layers", "precincts", "seed"}`, the bytes of a JP2 file (`file_<i>`) of random values, written
  by OpenJPEG, losslessly, a tile-part a tile;
- `python rasterio_process.py envi`: of each path of an ENVI data file, the raster GDAL's ENVI
  driver reads with the header beside it (`image_<i>`, its bands first), and, in `fields`, a JSON
  list of the header's fields as GDAL reads them (its `ENVI` metadata domain) and its driver."""

import io
import json
import sys
import warnings

import numpy as np
import rasterio
from rasterio.errors import NotGeoreferencedWarning
from rasterio.io import MemoryFile
from rasterio.transform import from_origin
from rasterio.windows import Window


def read(requests: list[dict]) -> dict[str, np.ndarray]:
    out: dict[str, np.ndarray] = {}
    about = []
    for i, r in enumerate(requests):
        with open(r["path"], "rb") as f:
            f.seek(r["offset"])
            data = f.read(r["length"])
        with MemoryFile(data) as memory, memory.open() as image:
            about.append({"driver": image.driver, "blocks": sorted(set(image.block_shapes))})
            out[f"image_{i}"] = image.read()
            for j, (column, row, width, height) in enumerate(r["windows"]):
                out[f"window_{i}_{j}"] = image.read(window=Window(column, row, width, height))
    out["about"] = np.array(json.dumps(about))
    return out


def write(requests: list[dict]) -> dict[str, np.ndarray]:
    out: dict[str, np.ndarray] = {}
    for i, r in enumerate(requests):
        rows, columns, samples, bits, layers = r["rows"], r["columns"], r["samples"], r["bits"], r["layers"]
        rng = np.random.default_rng(r["seed"])
        values = rng.integers(0, 1 << bits, size=(samples, rows, columns)).astype("u1" if bits <= 8 else "u2")
        with MemoryFile(ext=".jp2") as memory:
            # BLOCKSIZE_STRICT keeps tiles under 32 pixels, and tiles larger than the image
            with memory.open(driver="JP2OpenJPEG", width=columns, height=rows, count=samples, dtype=values.dtype,
                             blockysize=r["tile"][0], blockxsize=r["tile"][1], NBITS=bits, REVERSIBLE="YES",
                             QUALITY=",".join(str(100 * (k + 1) // layers) for k in range(layers)),
                             PRECINCTS=r["precincts"] or "", RESOLUTIONS=2, YCC="YES" if samples == 3 else "NO",
                             GMLJP2="YES", GeoJP2="NO", BLOCKSIZE_STRICT="YES", crs="EPSG:32632",
                             transform=from_origin(499980, 5200020, 10, 10)) as image:
                image.write(values)
            out[f"file_{i}"] = np.frombuffer(memory.read(), dtype="u1")
    return out


def envi(paths: list[str]) -> dict[str, np.ndarray]:
    out: dict[str, np.ndarray] = {}
    fields = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", NotGeoreferencedWarning)
        for i, path in enumerate(paths):
            with rasterio.open(path) as image:
                out[f"image_{i}"] = image.read()
                fields.append({"driver": image.driver, "fields": image.tags(ns="ENVI")})
    out["fields"] = np.array(json.dumps(fields))
    return out


if __name__ == "__main__":
    done = {"read": read, "write": write, "envi": envi}[sys.argv[1]](json.load(sys.stdin))
    archive = io.BytesIO()
    np.savez(archive, allow_pickle=False, **done)
    sys.stdout.buffer.write(archive.getvalue())
