"""GDAL's ENVI driver's reading of the ENVI fixtures, the reference reader of test_envi.py and
test_envi_stored.py. rasterio runs in a process of its own (rasterio_process.py says why): every
accepted fixture is read there once, in one run."""

import io
import json
import subprocess
import sys
from functools import cache
from pathlib import Path

import numpy as np
from ir_helpers import FIXTURES

ENVI = FIXTURES / "envi"
HEADERS = sorted(p for p in ENVI.iterdir() if p.suffix in (".hdr", ".HDR") and "reject" not in p.name)
RASTERIO = Path(__file__).with_name("rasterio_process.py")


def data_file(header: Path) -> Path:
    """A fixture header's data file: the first of the names the fixtures use that the directory
    lists, as written (a file system that ignores case finds `.img` for `.IMG`)."""
    stem, names = header.name[:-4], {p.name for p in header.parent.iterdir() if p.is_file()}
    return header.parent / next(n for s in ("", ".img", ".dat", ".IMG") if (n := f"{stem}{s}") in names)


@cache
def readings() -> dict[str, tuple[np.ndarray, dict[str, str]]]:
    """By data file name: GDAL's array (bands, lines, samples), and the header's fields as GDAL
    reads them (its `ENVI` metadata domain, a field's spaces as `_`)."""
    paths = [data_file(h) for h in HEADERS]
    done = subprocess.run([sys.executable, str(RASTERIO), "envi"], input=json.dumps([str(p) for p in paths]).encode(),
                          capture_output=True, check=True)
    with np.load(io.BytesIO(done.stdout)) as arrays:
        about = json.loads(str(arrays["fields"]))
        out = {}
        for i, p in enumerate(paths):
            assert about[i]["driver"] == "ENVI", p
            out[p.name] = (arrays[f"image_{i}"], about[i]["fields"])
    return out


def gdal(data: Path) -> tuple[np.ndarray, dict[str, str]]:
    """GDAL's reading of the raster whose data file is the fixture `data`."""
    return readings()[data.name]
