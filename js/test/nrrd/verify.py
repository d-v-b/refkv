"""Checks the browser NRRD zarrifier's output against pynrrd.

Each fixtures/nrrd/*.nrrd and *.nhdr is served over local HTTP, a detached header's data file
beside it (probed by the zarrifier: a file that does not exist is a 404), and zarrified by
js/conformance/zarrify.ts (the browser code, run under Node). The array, read through the
reference reader and zarr-python, must equal pynrrd's (`nrrd.read`), its axes put in the image's
order: the axes are placed here from pynrrd's kinds and space directions, independently of the
zarrifiers (spec/zarrify/nrrd.md §4.1
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#41-the-array>), and so are the
scales and translation (§4.3
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#43-scales-units-and-position>).
Every field and key/value pair pynrrd reads must be on the root, the same text or the same
numbers (pynrrd strips a pair's spaces and leaves its escapes, which the NRRD format keeps and
reads). Every file of the input must be rebuilt from the archive's IR mirror (the files are
described in place, spec/zarrify/nrrd.md §5.2
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#52-the-ir-mirror>).
`nrrd_reject_*` inputs must be rejected.

Usage: uv run python js/test/nrrd/verify.py [<dir of NRRD inputs>]
"""

from __future__ import annotations

import math
import re
import sys
import warnings
import zipfile
from pathlib import Path

import nrrd
import numpy as np
import zarr

HERE = Path(__file__).parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "conformance" / "vzip"))

import refkv.zarr  # noqa: E402  (a vzip archive as a zarr store)
from refkv import _core  # noqa: E402
from refkv._narrow import JSON, at, mapping, ndarray, sequence, string  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # js/test, for verify_common
from verify_common import LOOPBACK_SOURCES, metadata_v3, run, serve  # noqa: E402

nrrd.reader.ALLOW_DUPLICATE_FIELD = True  # a repeated key, which NRRD allows (its last value counts)
warnings.filterwarnings("ignore", "Duplicate header field")

# pynrrd's field names, as the NRRD format names each field (its alternate forms)
ALIASES = {"blocksize": "block size", "oldmin": "old min", "oldmax": "old max", "datafile": "data file",
           "lineskip": "line skip", "byteskip": "byte skip", "sampleunits": "sample units", "axismins": "axis mins",
           "axismaxs": "axis maxs", "centerings": "centers"}
FIELDS = {"dimension", "type", "block size", "encoding", "endian", "content", "min", "max", "old min", "old max",
          "data file", "line skip", "byte skip", "number", "sample units", "sizes", "spacings", "thicknesses",
          "axis mins", "axis maxs", "centers", "labels", "units", "kinds", "space", "space dimension", "space units",
          "space origin", "space directions", "measurement frame"}
SPATIAL = {"domain", "space"}


def numbers(text: str) -> list[float]:
    """The numbers of a descriptor, in order, `nan` and `inf` included (vectors' parentheses and
    commas, and `none`, left out)."""
    return [float(w) for w in re.split(r"[\s,()]+", text) if w and w.lower() != "none"]


def same_numbers(a: list[float], b: list[float]) -> bool:
    return len(a) == len(b) and all(x == y or (math.isnan(x) and math.isnan(y)) for x, y in zip(a, b, strict=True))


def flat(v: object) -> list[float]:
    """pynrrd's value of a numeric field as a flat list (a vector `none` is a row of NaNs, which
    the text writes as `none`)."""
    if isinstance(v, np.ndarray):
        return [float(x) for row in np.atleast_2d(v) if not np.all(np.isnan(row)) or v.ndim == 1 for x in np.ravel(row)]
    return [float(v)] if isinstance(v, (int, float)) else []


def field_problems(header: dict, own: dict) -> list[str]:
    """Every field and key/value pair pynrrd reads, against the root's."""
    problems = []
    fields = mapping(own["fields"])
    pairs = mapping(own["key_values"])
    seen = set()
    for name, value in header.items():
        f = ALIASES.get(name, name)
        if f not in FIELDS:
            mine = next((string(v) for k, v in pairs.items() if k.strip() == name), None)
            escaped = None if mine is None else mine.replace("\\", "\\\\").replace("\n", "\\n").strip()
            if escaped != str(value):
                problems.append(f"key {name!r}: {mine!r} != pynrrd's {value!r}")
            continue
        seen.add(f)
        text = fields.get(f)
        if not isinstance(text, str):
            problems.append(f"field {f!r} missing")
            continue
        if isinstance(value, str):
            if text != value:
                problems.append(f"field {f!r}: {text!r} != pynrrd's {value!r}")
        elif isinstance(value, list):  # quoted strings, or kinds
            got = re.findall(r'"((?:[^"\\]|\\.)*)"', text) if '"' in text else text.split()
            if [g.replace('\\"', '"') for g in got] != value:
                problems.append(f"field {f!r}: {got} != pynrrd's {value}")
        elif not same_numbers(numbers(text), flat(value)):
            problems.append(f"field {f!r}: {text!r} != pynrrd's {value!r}")
    if extra := set(fields) - seen:
        problems.append(f"fields {sorted(extra)} that pynrrd does not read")
    return problems


def placement(header: dict) -> tuple[list[str], dict[str, float], dict[str, float] | None]:
    """Each axis's image axis (fastest first), each image axis's scale, and the translation when
    the orientation is axis-aligned, from pynrrd's header (spec/zarrify/nrrd.md §4.1, §4.3)."""
    n = int(header["dimension"])
    kinds = [k.lower() for k in header.get("kinds", ["???"] * n)]
    dirs = header.get("space directions")
    roles = []
    for i in range(n):
        if kinds[i] in SPATIAL:
            roles.append("space")
        elif kinds[i] == "time":
            roles.append("time")
        elif kinds[i] not in ("???", "none"):
            roles.append("channel")
        else:
            roles.append("channel" if dirs is not None and np.all(np.isnan(dirs[i])) else "space")
    spatial = iter("xyz")
    names = [{"time": "t", "channel": "c"}.get(r) or next(spatial) for r in roles]
    spacings = header.get("spacings", [math.nan] * n)

    def valid(v: float) -> float | None:
        return float(v) if math.isfinite(v) and v > 0 else None

    aligned = dirs is not None and all(
        not np.all(np.isnan(dirs[i])) and all((dirs[i][k] > 0) if k == "xyz".index(names[i]) else dirs[i][k] == 0
                                              for k in range(len(dirs[i])))
        for i in range(n) if names[i] in "xyz")
    scale, translation = {}, None
    for i, a in enumerate(names):
        if a in "xyz" and dirs is not None and not np.all(np.isnan(dirs[i])):
            s = valid(dirs[i]["xyz".index(a)]) if aligned else valid(math.sqrt(sum(c * c for c in dirs[i])))
        elif a == "c":
            s = None
        else:
            s = valid(spacings[i])
        scale[a] = s if s is not None else 1
    origin = header.get("space origin")
    if aligned and origin is not None and np.all(np.isfinite(origin)):
        translation = {a: (float(origin["xyz".index(a)]) if a in "xyz" else 0) for a in names}
    return names, scale, translation


def problems_of(path: Path, out: Path) -> list[str]:
    problems = []
    data, header = nrrd.read(str(path), index_order="C")  # the header's last axis first
    root = zarr.open_group(refkv.zarr.open(str(out), policy=LOOPBACK_SOURCES), mode="r", zarr_format=3)
    arr = root.get_array("0")
    names = [str(n) for n in metadata_v3(arr).dimension_names or []]
    axes, scale, translation = placement(header)
    stored = list(reversed(axes))
    if sorted(names) != sorted(axes):
        return [f"axes {names} != {axes}"]
    want = np.transpose(data, [stored.index(a) for a in names])
    got = ndarray(arr[...])
    if got.dtype != want.dtype.newbyteorder("=") or got.shape != want.shape:
        problems.append(f"{got.dtype} {got.shape} != pynrrd's {want.dtype} {want.shape}")
    elif not np.array_equal(got, want, equal_nan=got.dtype.kind == "f"):
        problems.append("values differ from pynrrd's")
    own = mapping(at(root.attrs, "refkv_zarrified", "nrrd"))
    if list(sequence(own["axes"])) != axes:
        problems.append(f"the root's axes {own['axes']} != {axes}")
    dataset = at(root.attrs, "ome", "multiscales", 0, "datasets", 0)
    transforms: list[JSON] = list(sequence(at(dataset, "coordinateTransformations")))
    if at(transforms[0], "scale") != [scale[a] for a in names]:
        problems.append(f"scale {at(transforms[0], 'scale')} != {[scale[a] for a in names]}")
    got_tr = at(transforms[1], "translation") if len(transforms) > 1 else None
    if got_tr != (None if translation is None else [translation[a] for a in names]):
        problems.append(f"translation {got_tr} != {translation}")
    problems += field_problems(header, dict(own))
    # the rebuild: every file of the input from the mirror's table and its own bytes
    with zipfile.ZipFile(out) as z:
        keys = set(z.namelist())
        rebuilt = _core.rebuild_store(lambda k: z.read(k) if k in keys else None)
    for key, size, held in rebuilt:
        f = path.parent / key
        if held is not None or not f.is_file() or f.stat().st_size != size:
            problems.append(f"{key}: not described in place as a {size}-byte file")
    return problems


def main(fixtures: Path) -> int:
    paths = sorted(p for p in fixtures.iterdir() if p.suffix in (".nrrd", ".nhdr"))
    return run(paths, lambda path, out, _: problems_of(path, out), url=serve(paths))


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "fixtures" / "nrrd"))
