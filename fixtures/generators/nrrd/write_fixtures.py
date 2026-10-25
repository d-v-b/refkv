"""Writes synthetic NRRD inputs to fixtures/nrrd/, covering the rules of the NRRD profile
(spec/zarrify/nrrd.md §8 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nrrd.md#8-nrrd-profile>):
raw data in both byte orders and of every type, the type names' alternate forms, byte skip
(and -1) and line skip, gzip, detached headers (`.nhdr`) with their data file beside them or in a
directory below, orientation (axis-aligned, oblique), kinds (color, a list axis, time), key/value
pairs and comments, chunking, and the inputs the profile rejects (`nrrd_reject_*`).

Most headers are written line by line here, so that every field is exactly what the case needs;
one file is written by pynrrd, as real files are. js/test/nrrd/verify.py compares each accepted
input's zarrification with pynrrd's reading of it.

Usage: uv run python fixtures/generators/nrrd/write_fixtures.py
"""

from __future__ import annotations

import gzip
import math
import shutil
from pathlib import Path

import nrrd
import numpy as np

OUT = Path(__file__).parents[2] / "nrrd"

# numpy type of each NRRD type name used here
TYPES = {
    "uchar": "u1", "unsigned char": "u1", "uint8": "u1", "signed char": "i1", "int8_t": "i1",
    "short": "i2", "int16": "i2", "ushort": "u2", "unsigned short int": "u2", "int": "i4", "int32_t": "i4",
    "uint": "u4", "unsigned int": "u4", "longlong": "i8", "long long int": "i8", "ulonglong": "u8", "uint64": "u8",
    "float": "f4", "double": "f8",
}


def values(sizes: list[int], np_type: str, order: str, seed: int = 0) -> bytes:
    """Distinct values for every sample, the header's first axis fastest, in the byte order."""
    count = math.prod(sizes)
    v = (np.arange(count, dtype=np.int64) * 7 + seed) % 251 - (0 if np_type[0] in "uf" else 100)
    if np_type[0] == "f":
        v = v.astype(np.float64) / 4 - 20
    return np.asarray(v).astype(order + np_type).tobytes()


def gz(data: bytes) -> bytes:
    """`data` as one gzip member, of no name or time."""
    return gzip.compress(data, compresslevel=6, mtime=0)


def header(fields: list[str], magic: str = "NRRD0004", eol: str = "\n") -> bytes:
    """A header: the magic, the lines, and the blank line that ends it."""
    return (eol.join([magic, *fields]) + eol + eol).encode("utf-8")


def attached(name: str, sizes: list[int], type_: str = "uchar", *, endian: str = "little", encoding: str = "raw",
             extra: tuple[str, ...] = (), prefix: bytes = b"", data: bytes | None = None, truncate: int = 0,
             magic: str = "NRRD0004", eol: str = "\n", seed: int = 0) -> None:
    """A NRRD file with its data after its header (`prefix` before the data: skipped bytes or lines)."""
    np_type = TYPES.get(type_, "u1")
    fields = [f"type: {type_}", f"dimension: {len(sizes)}", f"sizes: {' '.join(map(str, sizes))}"]
    if np.dtype(np_type).itemsize > 1:
        fields.append(f"endian: {endian}")
    fields += [f"encoding: {encoding}", *extra]
    if data is None:
        data = values(sizes, np_type, "<" if endian == "little" else ">", seed)
        data = gz(data) if encoding in ("gzip", "gz") else data
    content = header(fields, magic, eol) + prefix + data
    (OUT / f"{name}.nrrd").write_bytes(content[: len(content) - truncate])


def patch(name: str, old: bytes, new: bytes) -> None:
    """Replaces `old` with `new` in the header of the file `name`."""
    path = OUT / name
    path.write_bytes(path.read_bytes().replace(old, new, 1))


def detached(name: str, sizes: list[int], type_: str, data_file: str, *, endian: str = "little",
             encoding: str = "raw", extra: tuple[str, ...] = (), prefix: bytes = b"", write: str | None = None,
             tail: bytes = b"", data: bool = True) -> None:
    """A detached header `<name>.nhdr` and, with `data`, the data file it names, written at
    `write` (by default the name, relative to the header's directory); `tail`: bytes after the
    header's blank line."""
    np_type = TYPES[type_]
    fields = [f"type: {type_}", f"dimension: {len(sizes)}", f"sizes: {' '.join(map(str, sizes))}"]
    if np.dtype(np_type).itemsize > 1:
        fields.append(f"endian: {endian}")
    fields += [f"encoding: {encoding}", *extra, f"data file: {data_file}"]
    (OUT / f"{name}.nhdr").write_bytes(header(fields) + tail)
    if not data:
        return
    raw = values(sizes, np_type, "<" if endian == "little" else ">")
    path = OUT / (write if write is not None else data_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(prefix + (gz(raw) if encoding == "gzip" else raw))


def accepted() -> None:
    # types, by name and alternate names, in both byte orders
    for i, t in enumerate(TYPES):
        endian = "big" if i % 2 else "little"
        attached(f"nrrd_type_{t.replace(' ', '_')}_{endian}", [5, 4, 3], t, endian=endian, seed=i)
    attached("nrrd_raw_uint8_2d", [7, 5], extra=("spacings: 0.5 0.25", 'units: "mm" "mm"'))
    # orientation: axis-aligned (applied, with a translation) and oblique, radiological (not)
    attached("nrrd_space_lps_aligned", [6, 5, 4], "short", extra=(
        "space: left-posterior-superior", "space directions: (0.5,0,0) (0,0.75,0) (0,0,1.25)",
        "space origin: (-12,3.5,40)", 'space units: "mm" "mm" "mm"', "kinds: domain domain domain"))
    attached("nrrd_space_oblique", [6, 5, 4], "short", endian="big", extra=(
        "space: RAS", "space directions: (-0.5,0,0) (0,0.6,0.45) (0,-0.3,0.4)", "space origin: (10, 20, 30)",
        'space units: "mm" "mm" "mm"'))
    attached("nrrd_space_dimension_2d", [6, 5], "float", extra=(
        "space dimension: 2", "space directions: (2,0) (0,3)", 'space units: "um" "um"'))
    # kinds: a color axis (interleaved samples), a list axis (diffusion), a time axis
    attached("nrrd_kinds_rgb", [3, 8, 6], extra=("kinds: RGB-color domain domain", "spacings: nan 0.1 0.1",
                                                  'labels: "rgb" "x" "y"'))
    attached("nrrd_kinds_dwi", [4, 5, 4, 3], "short", extra=(
        "kinds: list space space space", "space: right-anterior-superior",
        "space directions: none (2,0,0) (0,2,0) (0,0,2.5)", "space origin: (-5,-5,-3)",
        "measurement frame: (1,0,0) (0,1,0) (0,0,1)", "modality:=DWMRI", "DWMRI_b-value:=1000",
        "DWMRI_gradient_0000:=0 0 0", "DWMRI_gradient_0001:=1 0 0", "DWMRI_gradient_0002:=0 1 0",
        "DWMRI_gradient_0003:=0 0 1"))
    attached("nrrd_kinds_time", [5, 4, 3, 6], "ushort", endian="big", extra=(
        "kinds: domain domain domain time", "spacings: 0.2 0.2 0.5 1.5", 'units: "mm" "mm" "mm" "s"',
        "min: 0", "max: 250"))
    # everything the header may hold besides: comments, escapes, CRLF line ends, the other fields
    attached("nrrd_header_everything", [4, 3, 2], "ushort", eol="\r\n", magic="NRRD0005", extra=(
        "# a comment", "## another, ## and all", "#", "content: a test volume: the header's fields", "min: 3",
        "max: 249.5", "old min: -1", "oldmax: 2", "sample units: counts", "number: 24",
        "thicknesses: nan nan 2.5", "axis mins: 0 0 0", "axismaxs: 4 3 2", "centers: cell cell node",
        'labels: "x \\"one\\"" "y" "z"', 'units: "mm" "mm" "mm"', "spacings: 1 1 2",
        "escaped:=a\\nline\\\\end", "repeated:=first", "repeated:=last", "with spaces := and = signs:="))
    attached("nrrd_magic_0001", [3, 2], magic="NRRD0001")
    # skips
    attached("nrrd_byte_skip", [6, 5], "short", extra=("byte skip: 17",), prefix=bytes(range(17)))
    attached("nrrd_line_skip", [6, 5], "short", extra=("line skip: 2", "byte skip: 3"),
             prefix=b"first skipped line\nsecond\r\nabc")
    detached("nrrd_detached_byte_skip_end", [5, 4], "float", "nrrd_detached_byte_skip_end.dat",
             extra=("byte skip: -1",), prefix=b"P5 a header of another format\n" + bytes(40))
    # gzip, attached and detached
    attached("nrrd_gzip_3d", [6, 5, 4], "short", encoding="gzip", extra=("space: LPS",
             "space directions: (1,0,0) (0,1,0) (0,0,2)", "space origin: (0,0,0)"))
    attached("nrrd_gz_rgb", [3, 5, 4], encoding="gz", extra=("kinds: RGB-color domain domain",))
    detached("nrrd_detached_gzip", [5, 4, 3], "int", "./nrrd_detached_gzip_files/data.raw.gz", encoding="gzip",
             write="nrrd_detached_gzip_files/data.raw.gz", extra=("line skip: 1",), prefix=b"skipped\n")
    # detached raw, the header followed by a blank line and more
    detached("nrrd_detached_raw", [6, 5, 2], "short", "nrrd_detached_raw.raw", endian="big",
             tail=b"ignored after the blank line\n")
    # chunked: 140000 bytes, two chunks of 350 rows; and a cut with an edge chunk
    attached("nrrd_chunk_rows", [200, 700])
    attached("nrrd_chunk_edge", [100, 30, 47], "ushort")
    # as pynrrd writes a file (gzip, its own field order)
    data = (np.arange(4 * 5 * 6, dtype=np.int16).reshape(4, 5, 6) * 3) % 200
    nrrd.write(str(OUT / "nrrd_pynrrd_gzip.nrrd"), data, {"space": "left-posterior-superior",
               "space directions": np.diag([0.5, 0.5, 1.0]), "space origin": np.array([1.0, 2.0, 3.0]),
               "kinds": ["domain", "domain", "domain"]}, index_order="F")
    # pynrrd writes the header's time-free fields only; its gzip member has no time either
    nrrd.write(str(OUT / "nrrd_pynrrd_raw.nrrd"), data.astype(np.float32), {"encoding": "raw"}, index_order="C")


def rejected() -> None:
    attached("nrrd_reject_ascii", [3, 2], encoding="ascii", data=b"1 2 3\n4 5 6\n")
    attached("nrrd_reject_hex", [3, 2], encoding="hex", data=b"010203040506\n")
    attached("nrrd_reject_bzip2", [3, 2], encoding="bzip2", data=b"BZh9")
    attached("nrrd_reject_block", [3, 2], "uchar", extra=("block size: 4",), data=bytes(24))
    patch("nrrd_reject_block.nrrd", b"type: uchar", b"type: block")
    attached("nrrd_reject_type_unknown", [3, 2], "uchar", data=bytes(6))
    patch("nrrd_reject_type_unknown.nrrd", b"type: uchar", b"type: char")
    (OUT / "nrrd_reject_no_blank_line.nrrd").write_bytes(header(["type: uchar", "dimension: 1", "sizes: 3"])[:-1])
    attached("nrrd_reject_unknown_field", [3, 2], extra=("colour: blue",))
    attached("nrrd_reject_field_twice", [3, 2], extra=("encoding: raw",))
    attached("nrrd_reject_not_a_line", [3, 2], extra=("just some words",))
    attached("nrrd_reject_sizes_count", [3, 2], data=bytes(6))
    patch("nrrd_reject_sizes_count.nrrd", b"sizes: 3 2", b"sizes: 3 2 1")
    attached("nrrd_reject_no_endian", [3, 2], "short", data=bytes(12))
    patch("nrrd_reject_no_endian.nrrd", b"endian: little\n", b"")
    attached("nrrd_reject_truncated", [6, 5], "short", truncate=1)
    attached("nrrd_reject_gzip_byte_skip", [3, 2], encoding="gzip", extra=("byte skip: 4",))
    attached("nrrd_reject_gzip_not_gzip", [6, 5], encoding="gzip", data=bytes(40))
    attached("nrrd_reject_one_axis", [12])
    attached("nrrd_reject_two_channel_axes", [3, 2, 4, 5], extra=("kinds: RGB-color vector domain domain",))
    attached("nrrd_reject_line_skip_past_end", [3, 2], extra=("line skip: 3",), data=b"one\n" + bytes(6))
    attached("nrrd_reject_space_directions_count", [3, 2], extra=("space: RAS", "space directions: (1,0,0) (0,1)"))
    # detached headers whose data file is not one file below the header's directory, or is missing
    detached("nrrd_reject_detached_list", [3, 2], "uchar", "LIST", data=False)
    detached("nrrd_reject_detached_pattern", [3, 2, 2], "uchar", "slice%03d.raw 1 2 1 2", data=False)
    detached("nrrd_reject_detached_absolute", [3, 2], "uchar", "/data/image.raw", data=False)
    detached("nrrd_reject_detached_parent", [3, 2], "uchar", "../image.raw", data=False)
    detached("nrrd_reject_detached_missing", [3, 2], "uchar", "image.raw", data=False)
    detached("nrrd_reject_detached_itself", [3, 2], "uchar", "./nrrd_reject_detached_itself.nhdr", data=False)
    # a first line longer than the magic, which the signature does not take (named as NRRD, the
    # profile rejects it)
    (OUT / "nrrd_reject_magic_long.nrrd").write_bytes(b"NRRD0004 \ntype: uchar\n\n")


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    accepted()
    rejected()


if __name__ == "__main__":
    main()
