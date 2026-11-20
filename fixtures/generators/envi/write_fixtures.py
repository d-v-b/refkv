"""Writes synthetic ENVI rasters to fixtures/envi/, covering the rules of the ENVI profile
(spec/zarrify/envi.md §8 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#8-envi-profile>):
the three interleaves, every data type of a flat array in both byte orders, a header offset, whole-file
gzip (with and without a header offset), the data file's names the profile probes (no extension, `.img`,
`.dat`, the header named `<data file>.hdr`, capitals), braced values over several lines, comments,
CRLF line ends, and the inputs the profile rejects (`envi_reject_*`).

Each input is a header file `<name>.hdr` and its data file beside it. The headers are written
line by line here, so that every field is exactly what the case needs; the data is NumPy's bytes
of distinct values in the header's interleave and byte order. js/test/envi/verify.py and
python/tests/ir/test_envi.py compare each accepted input's zarrification with NumPy's reading of
its data file, and with GDAL's.

Usage: uv run python fixtures/generators/envi/write_fixtures.py
"""

from __future__ import annotations

import gzip
import shutil
from pathlib import Path

import numpy as np

OUT = Path(__file__).parents[2] / "envi"

# numpy type of each ENVI data type code
TYPES = {1: "u1", 2: "i2", 3: "i4", 4: "f4", 5: "f8", 6: "c8", 9: "c16", 12: "u2", 13: "u4", 14: "i8", 15: "u8"}
# the data's axes, outermost first, by interleave: indexes into (bands, lines, samples)
ORDER = {"bsq": (0, 1, 2), "bil": (1, 0, 2), "bip": (1, 2, 0)}


def values(bands: int, lines: int, samples: int, code: int, seed: int = 0) -> np.ndarray:
    """Distinct values for every sample, shaped (bands, lines, samples)."""
    t = np.dtype(TYPES[code])
    v = (np.arange(bands * lines * samples, dtype=np.int64) * 7 + seed) % 251
    if t.kind == "i":
        v = v - 100
    out = v.astype(np.float64) / 4 - 20 if t.kind in "fc" else v
    if t.kind == "c":
        out = out + 1j * (out[::-1] + 0.5)
    return np.asarray(out).astype(t).reshape(bands, lines, samples)


def data(a: np.ndarray, interleave: str, big: bool) -> bytes:
    """The array's bytes as the interleave stores them, in the byte order."""
    order = ">" if big else "<"
    return np.ascontiguousarray(a.transpose(ORDER[interleave])).astype(a.dtype.newbyteorder(order)).tobytes()


def portable(member: bytes) -> bytes:
    """A gzip member with its header's OS byte (offset 9) at 255, "unknown": zlib writes its build's
    OS code there (3 on Linux, 19 on macOS), so that fixtures differ between platforms. The member
    has no header CRC (FHCRC, bit 1 of its flags), which the byte would change."""
    if member[:3] != b"\x1f\x8b\x08" or member[3] & 2:
        raise SystemExit("not a gzip member without a header CRC")
    return member[:9] + b"\xff" + member[10:]


def gz(b: bytes) -> bytes:
    """`b` as one gzip member, of no name or time, its OS byte portable."""
    return portable(gzip.compress(b, compresslevel=6, mtime=0))


def write(header_name: str, lines: list[str], data_name: str | None, content: bytes, eol: str = "\n") -> None:
    """A header file of the magic and `lines`, and its data file (none when `data_name` is None)."""
    (OUT / header_name).write_bytes((eol.join(["ENVI", *lines]) + eol).encode("utf-8"))
    if data_name is not None:
        (OUT / data_name).write_bytes(content)


def raster(name: str, *, bands: int = 3, lines: int = 4, samples: int = 5, code: int = 2, interleave: str = "bsq",
           big: bool = False, offset: int = 0, compression: int = 0, data_suffix: str = ".img",
           header_name: str | None = None, extra: tuple[str, ...] = (), fields: list[str] | None = None,
           eol: str = "\n", seed: int = 0, truncate: int = 0) -> None:
    """An ENVI raster: its header (`fields` in place of the usual ones when given) and its data file
    `<name><data_suffix>`, the data after `offset` bytes, gzip for `compression` 1."""
    if fields is None:
        fields = [f"samples = {samples}", f"lines = {lines}", f"bands = {bands}", f"header offset = {offset}",
                  "file type = ENVI Standard", f"data type = {code}", f"interleave = {interleave}",
                  f"byte order = {int(big)}"]
        if compression:
            fields.append(f"file compression = {compression}")
    raw = bytes(i % 256 for i in range(offset)) + data(values(bands, lines, samples, code, seed), interleave, big)
    content = gz(raw) if compression == 1 else raw
    content = content[: len(content) - truncate]
    write(header_name or f"{name}.hdr", [*fields, *extra], f"{name}{data_suffix}", content, eol)


def main() -> None:
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)

    # the example of spec/zarrify/envi.md §6 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#6-example>
    raster("envi_bil_int16_big", interleave="bil", big=True,
           fields=["description = {a BIL test raster}", "samples = 5", "lines = 4", "bands = 3", "header offset = 0",
                   "file type = ENVI Standard", "data type = 2", "interleave = bil", "byte order = 1"],
           extra=("band names = {red, green, blue}",))
    # every data type, the interleaves and byte orders in turn
    for k, code in enumerate(TYPES):
        name = np.dtype(TYPES[code]).name
        raster(f"envi_type_{code}_{name}", code=code, interleave=("bsq", "bil", "bip")[k % 3], big=k % 2 == 1, seed=k)
    # ENVI's own name for the data file, none, and the fields it writes; a 1-byte type with no byte order
    raster("envi_bsq_uint8", code=1, data_suffix="", bands=2, lines=3, samples=4,
           fields=["samples = 4", "lines = 3", "bands = 2", "data type = 1", "interleave = bsq"])
    # a hyperspectral header: braced values over several lines, comments, unknown fields, CRLF
    raster("envi_bip_float32", code=4, interleave="bip", bands=6, lines=2, samples=3, data_suffix=".dat", eol="\r\n",
           extra=("; written by the refkv fixture generator", "wavelength units = Nanometers",
                  "wavelength = {", " 450.0, 500.0, 550.0,", " 600.0, 650.0, 700.0}",
                  "map info = {UTM, 1.000, 1.000, 500000.000, 4100000.000, 3.0, 3.0, 13, North, WGS-84, units=Meters}",
                  "data ignore value = -9999", "a line that is no field"))
    # a header offset: the data file's first bytes are ENVI's embedded header
    raster("envi_header_offset", offset=128, interleave="bil", code=12)
    # whole-file gzip, with and without a header offset (counted in the inflated bytes)
    raster("envi_gzip", compression=1, interleave="bip", code=4)
    raster("envi_gzip_header_offset", compression=1, offset=32, code=3, big=True)
    # the header named for its data file, `<data file>.hdr`, as VirtualiZarr finds it first
    raster("envi_named.img", data_suffix="", header_name="envi_named.img.hdr", code=5)
    # capitals: `.HDR` probes `.IMG`
    raster("envi_upper", data_suffix=".IMG", header_name="envi_upper.HDR", code=13, interleave="bip")
    # a band, a line and a sample of one, and no optional field
    raster("envi_single", bands=1, lines=1, samples=1, code=14,
           fields=["samples = 1", "lines = 1", "bands = 1", "data type = 14", "interleave = BIL"])

    # rejected inputs (conformance/zarrify/rejects/envi.json)
    raster("envi_reject_file_type", fields=["samples = 5", "lines = 4", "bands = 3", "data type = 2",
                                            "interleave = bsq", "file type = ENVI Spectral Library"])
    raster("envi_reject_framed", extra=("major frame offsets = {0, 8}",))
    raster("envi_reject_no_samples", fields=["lines = 4", "bands = 3", "data type = 2", "interleave = bsq"])
    raster("envi_reject_samples_zero", fields=["samples = 0", "lines = 4", "bands = 3", "data type = 2",
                                               "interleave = bsq"])
    raster("envi_reject_no_interleave", fields=["samples = 5", "lines = 4", "bands = 3", "data type = 2"])
    raster("envi_reject_interleave", fields=["samples = 5", "lines = 4", "bands = 3", "data type = 2",
                                             "interleave = bsx"])
    raster("envi_reject_data_type", fields=["samples = 5", "lines = 4", "bands = 3", "data type = 7",
                                            "interleave = bsq"])
    raster("envi_reject_byte_order", fields=["samples = 5", "lines = 4", "bands = 3", "data type = 2",
                                             "interleave = bsq", "byte order = 2"])
    raster("envi_reject_header_offset", fields=["samples = 5", "lines = 4", "bands = 3", "data type = 2",
                                                "interleave = bsq", "header offset = -1"])
    raster("envi_reject_compression", fields=["samples = 5", "lines = 4", "bands = 3", "data type = 2",
                                              "interleave = bsq", "file compression = 2"])
    raster("envi_reject_size", truncate=1)
    raster("envi_reject_offset_past_end", fields=["samples = 5", "lines = 4", "bands = 3", "data type = 2",
                                                  "interleave = bsq", "header offset = 4096"])
    raster("envi_reject_no_data_file", data_suffix=".img")
    (OUT / "envi_reject_no_data_file.img").unlink()
    write("envi_reject_too_large.hdr", ["samples = 2097152", "lines = 2097152", "bands = 2097152", "data type = 5",
                                        "interleave = bsq"], "envi_reject_too_large.img", bytes(8))
    short = ["samples = 1", "lines = 1", "bands = 1", "data type = 1", "interleave = bsq", "file compression = 1"]
    write("envi_reject_gzip_short.hdr", short, "envi_reject_gzip_short.img", b"\x1f\x8b\x08" + bytes(7))
    raster("envi_reject_not_gzip", fields=["samples = 5", "lines = 4", "bands = 3", "data type = 2",
                                           "interleave = bsq", "file compression = 1"])


if __name__ == "__main__":
    main()
