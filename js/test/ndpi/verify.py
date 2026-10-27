"""Checks the browser NDPI zarrifier against tifffile and imagecodecs.

The NDPI fixtures in fixtures/ndpi/ go through the same check as the
TIFFs (js/test/tiff/verify.py): every level must equal what tifffile reads.
Then each accepted slide whose levels are JPEG XR is checked against the
streams themselves (spec/zarrify/ndpi.md §3
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#3-levels>): every
level's data type and samples are what imagecodecs decodes its first stream to,
whatever its tags say, and the root's source metadata and `omero` label are
level 0's acquisition tags (§5
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#5-source-metadata>).

Usage: uv run python js/test/ndpi/verify.py
"""

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

import imagecodecs
import tifffile
import zarr

import refkv.zarr
from refkv._narrow import at, mapping, sequence, string

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location("tiff_verify", HERE.parent / "tiff" / "verify.py")
assert spec is not None
assert spec.loader is not None
tiff_verify = sys.modules["tiff_verify"] = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tiff_verify)

JPEGXR = 22610  # Hamamatsu's Compression for JPEG XR (tifffile's JPEGXR_NDPI)
ACQUISITION = {65434: "Fluorescence", 65451: "Wavelength", 65454: "ExposureTime"}


def jpegxr_problems(path: Path, out: Path) -> list[str]:
    """What differs between a JPEG XR slide's hierarchy and its streams, as imagecodecs decodes
    them, and its acquisition tags, as tifffile reads them."""
    problems = []
    root = zarr.open_group(refkv.zarr.open(str(out), policy=tiff_verify.LOOPBACK_SOURCES), mode="r", zarr_format=3)
    with tifffile.TiffFile(path) as tf, path.open("rb") as f:
        levels = [p for p in map(tiff_verify.tiff_page, tf.pages) if p.tags[65421].value > 0]
        for li, page in enumerate(levels):
            j = next(i for i, n in enumerate(page.databytecounts) if n > 0)
            f.seek(page.dataoffsets[j])
            stream = imagecodecs.jpegxr_decode(f.read(page.databytecounts[j]))
            samples = 1 if stream.ndim == 2 else stream.shape[2]
            a = root.get_array(str(li))
            if (a.dtype, a.shape[0]) != (stream.dtype, samples):
                problems.append(f"level {li}: {a.dtype} × {a.shape[0]}, its streams {stream.dtype} × {samples}")
            if a.chunks[1:] != stream.shape[:2]:
                problems.append(f"level {li}: chunks {a.chunks}, its first stream {stream.shape}")
        tags = levels[0].tags
        want = {name: tags[code].value for code, name in ACQUISITION.items() if code in tags}
        got = dict(mapping(mapping(root.attrs["refkv_zarrified"]).get("ndpi", {})))
        if got != want:
            problems.append(f"source metadata {got}, the tags {want}")
        channels = sequence(at(root.attrs, "ome", "omero", "channels"))
        if len(channels) == 1:
            label = string(at(channels[0], "label"))
            if label != want.get("Fluorescence", "C0"):
                problems.append(f"label {label!r}, Fluorescence {want.get('Fluorescence')!r}")
    return problems


def main() -> int:
    fixtures = HERE.parents[2] / "fixtures" / "ndpi"
    failures = tiff_verify.main(fixtures)
    with tempfile.TemporaryDirectory() as tmp:
        for path in sorted(fixtures.glob("*.ndpi")):
            if "reject" in path.name:
                continue
            with tifffile.TiffFile(path) as tf:
                if tf.pages.first.compression != JPEGXR:
                    continue
            out = Path(tmp) / f"{path.stem}.vzip"
            command = [*tiff_verify.CLI, str(path), str(out), path.resolve().as_uri()]
            subprocess.run(command, capture_output=True, check=True)
            problems = jpegxr_problems(path, out)
            failures += bool(problems)
            print(f"{path.name:42s} jpegxr {problems or 'ok'}")
    print(f"\n{failures} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
