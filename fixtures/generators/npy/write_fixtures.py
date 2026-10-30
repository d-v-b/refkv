"""Writes synthetic NumPy inputs to fixtures/npy/, covering the rules of the NumPy profile
(spec/zarrify/npy.md §8 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/npy.md#8-numpy-profile>):
`.npy` files of every presented data type, in C and Fortran order, of rank 0, of size 0, in
format versions 1.0, 2.0 and 3.0, large enough to be cut into chunks, with a header past the
first read and bytes after the data; `.npz` archives as `np.savez` writes them, with ZIP64 end
records, a comment, and members it does not present; and the inputs the profile rejects
(`*_reject_*`).

The arrays are written by NumPy (`np.lib.format.write_array`); the archives by `zipfile`, as
`np.savez` writes them but with a fixed date, so that the files are the same on every run; the
rejected headers by hand. js/test/npy/verify.py and python/tests/ir/test_npy.py compare each
accepted input's zarrification with `np.load`.

Usage: uv run python fixtures/generators/npy/write_fixtures.py
"""

from __future__ import annotations

import io
import shutil
import struct
import zipfile
from pathlib import Path

import numpy as np

OUT = Path(__file__).parents[2] / "npy"
DATE = (2026, 10, 30, 0, 0, 0)


def values(shape: tuple[int, ...], dtype: str | np.dtype, seed: int = 0) -> np.ndarray:
    """Distinct values of every element, in `dtype`."""
    d = np.dtype(dtype)
    count = int(np.prod(shape))
    base = (np.arange(count, dtype=np.int64) * 7 + seed) % 251
    if d.names:
        out = np.zeros(count, dtype=d)
        for k, name in enumerate(d.names):
            out[name] = values((count,), d[name], seed + k)
        return out.reshape(shape)
    if d.kind == "b":
        v = base % 3 == 0
    elif d.kind in "iu":
        v = base - (0 if d.kind == "u" else 100)
    elif d.kind == "f":
        v = base / 4 - 20
    elif d.kind == "c":
        v = (base / 4 - 20) + 1j * (base / 8)
    elif d.kind == "M":
        v = (base * 1000).astype("datetime64[ns]")
    elif d.kind == "m":
        v = base.astype("timedelta64[s]")
    elif d.kind == "U":
        v = np.array([f"é{i}"[: d.itemsize // 4] for i in base])
    elif d.kind in "SV":
        v = np.array([bytes([i % 256]) * d.itemsize for i in base], dtype=f"S{d.itemsize}")
    else:
        raise ValueError(d)
    return np.asarray(v).astype(d).reshape(shape)


def npy_bytes(a: np.ndarray, version: tuple[int, int] | None = None, fortran: bool = False) -> bytes:
    """The NPY file of `a`, as NumPy writes it."""
    f = io.BytesIO()
    np.lib.format.write_array(f, np.asfortranarray(a) if fortran else a, version=version, allow_pickle=True)
    return f.getvalue()


def write(name: str, data: bytes) -> None:
    (OUT / name).write_bytes(data)


def npz(name: str, members: list[tuple[str, bytes, int]], comment: bytes = b"") -> None:
    """A `.npz` archive of `members` (file name, bytes, compression method), each written as
    `np.savez` writes a member (`force_zip64`), with a fixed date."""
    with zipfile.ZipFile(OUT / name, "w", allowZip64=True) as z:
        z.comment = comment
        for member, data, method in members:
            info = zipfile.ZipInfo(member, date_time=DATE)
            info.compress_type = method
            with z.open(info, "w", force_zip64=True) as f:
                f.write(data)


def handmade(descr: str, shape: str = "(2,)", data: bytes = b"", version: int = 1, extra: str = "") -> bytes:
    """An NPY file whose header is written here: `descr` and `shape` as Python writes them."""
    text = f"{{'descr': {descr}, 'fortran_order': False, 'shape': {shape}, {extra}}}"
    return header_bytes(text, version) + data


def header_bytes(text: str, version: int = 1, encoding: str = "latin1") -> bytes:
    raw = text.encode(encoding)
    pre = 10 if version == 1 else 12
    pad = -(pre + len(raw) + 1) % 64
    raw = raw + b" " * pad + b"\n"
    length = struct.pack("<H", len(raw)) if version == 1 else struct.pack("<I", len(raw))
    return b"\x93NUMPY" + bytes([version, 0]) + length + raw


def accepted() -> None:
    # every presented data type, C order
    for name, dtype, shape in [
        ("npy_c_float64", "<f8", (3, 4)),
        ("npy_bool", "|b1", (5, 3)),
        ("npy_int8", "|i1", (4, 4)),
        ("npy_uint16_be", ">u2", (2, 3, 4)),
        ("npy_int64", "<i8", (6,)),
        ("npy_float16", "<f2", (3, 5)),
        ("npy_float32_be", ">f4", (4, 3)),
        ("npy_complex64", "<c8", (2, 2)),
        ("npy_complex128_be", ">c16", (3,)),
        ("npy_datetime64_ns", "<M8[ns]", (4,)),
        ("npy_timedelta64_10s", ">m8[10s]", (2, 2)),
        ("npy_unicode", "<U4", (3, 2)),
        ("npy_unicode_be", ">U3", (2,)),
        ("npy_bytes", "|S5", (3, 2)),
        ("npy_void", "|V3", (4,)),
        ("npy_struct", np.dtype([("x", "<f4"), ("y", "<i2"), ("z", [("a", "|u1"), ("b", "<u4")])]), (5,)),
        ("npy_struct_be", np.dtype([("x", ">f8"), ("flag", "|b1")]), (2, 3)),
    ]:
        write(f"{name}.npy", npy_bytes(values(shape, dtype)))
    # Fortran order, of rank 2 and 3, and with a trailing axis; rank 1 (the same bytes either way)
    write("npy_f_float64.npy", npy_bytes(values((3, 4), "<f8"), fortran=True))
    write("npy_f_int16_3d.npy", npy_bytes(values((2, 3, 4), "<i2"), fortran=True))
    write("npy_f_bytes.npy", npy_bytes(values((3, 2), "|S2"), fortran=True))
    write("npy_f_rank1.npy", npy_bytes(values((7,), "<i4"), fortran=True))
    # rank 0, size 0
    write("npy_scalar_int32.npy", npy_bytes(np.array(42, dtype="<i4")))
    write("npy_scalar_bytes.npy", npy_bytes(np.array(b"abc", dtype="|S3")))
    write("npy_empty.npy", npy_bytes(np.zeros((0, 3), dtype="<f4")))
    # format versions 2.0 and 3.0 (a field name that is not latin-1)
    write("npy_version2.npy", npy_bytes(values((2, 3), "<u4"), version=(2, 0)))
    write("npy_version3.npy", npy_bytes(values((3,), np.dtype([("温度", "<f4"), ("b", "<i8")])), version=(3, 0)))
    # chunks, each over 1 MiB: cut along rows into two equal chunks; with an edge chunk; in
    # Fortran order, cut along the last axis
    write("npy_chunk_rows.npy", npy_bytes(values((1024, 1100), "|u1")))
    write("npy_chunk_edge.npy", npy_bytes(values((523, 2049), "|u1")))
    write("npy_chunk_fortran.npy", npy_bytes(values((1000, 1050), "|u1"), fortran=True))
    # a header past the first 4096-byte read: a structure of many fields
    many = np.dtype([(f"field_{k:03d}", "<i2") for k in range(400)])
    write("npy_long_header.npy", npy_bytes(values((2,), many)))
    # bytes after the data, which are a gap
    write("npy_trailing_bytes.npy", npy_bytes(values((2, 2), "<i4")) + b"trailing")
    # a header as Python 2 wrote one, its integers longs, as NumPy's test file win64python2.npy has
    write(
        "npy_python2_long.npy",
        header_bytes("{'descr': '<f8', 'fortran_order': False, 'shape': (2L, 3L), }") + values((2, 3), "<f8").tobytes(),
    )
    # a header in the variations the grammar allows: double quotes, other key order, no trailing comma
    write(
        "npy_header_variant.npy",
        header_bytes('{"shape": (2, 2), "fortran_order": False, "descr": "<i2"}') + values((2, 2), "<i2").tobytes(),
    )

    # archives as np.savez writes them, of two members and of one
    a, b = values((4, 5), "<f8"), values((3,), "<i4", 1)
    npz(
        "npz_savez.npz",
        [("image.npy", npy_bytes(a), zipfile.ZIP_STORED), ("labels.npy", npy_bytes(b), zipfile.ZIP_STORED)],
    )
    npz("npz_one.npz", [("arr_0.npy", npy_bytes(values((2, 2, 2), ">u2")), zipfile.ZIP_STORED)])
    # members not presented, each with its reason, beside presented ones
    npz(
        "npz_mixed.npz",
        [
            ("kept.npy", npy_bytes(values((3, 3), "<f4")), zipfile.ZIP_STORED),
            ("deflated.npy", npy_bytes(values((50,), "<f8")), zipfile.ZIP_DEFLATED),
            ("notes.txt", b"not an array\n", zipfile.ZIP_STORED),
            ("dir/nested.npy", npy_bytes(values((2,), "<i2")), zipfile.ZIP_STORED),
            ("refkv_source.npy", npy_bytes(values((2,), "<i2")), zipfile.ZIP_STORED),
            ("objects.npy", npy_bytes(np.array([{"a": 1}, None], dtype=object)), zipfile.ZIP_STORED),
            ("long_double.npy", handmade("'<f16'", data=bytes(32)), zipfile.ZIP_STORED),
            ("short.npy", handmade("'<f8'", data=bytes(8)), zipfile.ZIP_STORED),
            ("broken.npy", b"\x93NUMPY\x01\x00\x10\x00{'descr': '<f8'", zipfile.ZIP_STORED),
            ("scalar.npy", npy_bytes(np.array(2.5)), zipfile.ZIP_STORED),
            ("empty.npy", npy_bytes(np.zeros((0,), dtype="<i2")), zipfile.ZIP_STORED),
        ],
    )
    # ZIP64 end records and extra fields in the central directory, and an archive comment
    saved = zipfile.ZIP64_LIMIT, zipfile.ZIP_FILECOUNT_LIMIT
    zipfile.ZIP64_LIMIT, zipfile.ZIP_FILECOUNT_LIMIT = 0, 0
    try:
        npz("npz_zip64.npz", [("a.npy", npy_bytes(a), zipfile.ZIP_STORED), ("b.npy", npy_bytes(b), zipfile.ZIP_STORED)])
    finally:
        zipfile.ZIP64_LIMIT, zipfile.ZIP_FILECOUNT_LIMIT = saved
    # its end of central directory record as a file past ZIP64's limits writes it: the counts,
    # size and offset are in the ZIP64 record alone
    path = OUT / "npz_zip64.npz"
    raw = bytearray(path.read_bytes())
    e = raw.rindex(b"PK\x05\x06")
    raw[e + 8 : e + 20] = b"\xff" * 12
    path.write_bytes(bytes(raw))
    npz("npz_comment.npz", [("a.npy", npy_bytes(b), zipfile.ZIP_STORED)], comment=b"written by write_fixtures.py")


def rejected() -> None:
    write("npy_reject_short.npy", b"\x93NUMPY\x01\x00")
    write("npy_reject_version4.npy", b"\x93NUMPY\x04\x00" + bytes(10))
    write("npy_reject_version_minor.npy", b"\x93NUMPY\x01\x01\x10\x00" + bytes(16))
    write("npy_reject_header_outside.npy", b"\x93NUMPY\x01\x00\xff\x7f{'descr': '<f8'}")
    write(
        "npy_reject_not_utf8.npy",
        header_bytes("{'descr': '<f8', 'fortran_order': False, 'shape': (1,), }", 3)[:-3] + b"\xff\xfe\n" + bytes(8),
    )
    write("npy_reject_not_a_dict.npy", header_bytes("('descr', '<f8')") + bytes(8))
    write("npy_reject_keys.npy", header_bytes("{'descr': '<f8', 'shape': (1,), }") + bytes(8))
    write("npy_reject_shape.npy", handmade("'<f8'", shape="[1, 2]", data=bytes(16)))
    write("npy_reject_object.npy", npy_bytes(np.array([1, "a"], dtype=object)))
    write("npy_reject_long_double.npy", handmade("'<f16'", data=bytes(32)))
    write(
        "npy_reject_padding.npy",
        npy_bytes(values((2,), np.dtype({"names": ["a", "b"], "formats": ["|u1", "<i4"], "offsets": [0, 4]}))),
    )
    write("npy_reject_truncated.npy", npy_bytes(values((10,), "<f8"))[:-8])
    # every member deflated, as np.savez_compressed writes them: none can be presented
    npz(
        "npz_reject_compressed.npz",
        [
            ("a.npy", npy_bytes(values((4,), "<f8")), zipfile.ZIP_DEFLATED),
            ("b.npy", npy_bytes(values((2,), "<i4")), zipfile.ZIP_DEFLATED),
        ],
    )
    npz(
        "npz_reject_duplicate.npz",
        [
            ("a.npy", npy_bytes(values((2,), "<f8")), zipfile.ZIP_STORED),
            ("a.npy", npy_bytes(values((3,), "<f8")), zipfile.ZIP_STORED),
        ],
    )
    # a stored member whose central directory gives a compressed size other than its size
    npz("npz_reject_stored_size.npz", [("a.npy", npy_bytes(values((2,), "<f8")), zipfile.ZIP_STORED)])
    path = OUT / "npz_reject_stored_size.npz"
    raw = bytearray(path.read_bytes())
    cd = raw.rindex(b"PK\x01\x02")
    raw[cd + 20 : cd + 24] = struct.pack("<I", struct.unpack("<I", raw[cd + 24 : cd + 28])[0] - 1)
    path.write_bytes(bytes(raw))
    # an archive cut before its central directory
    npz("npz_reject_truncated.npz", [("a.npy", npy_bytes(values((2,), "<f8")), zipfile.ZIP_STORED)])
    path = OUT / "npz_reject_truncated.npz"
    raw = path.read_bytes()
    path.write_bytes(raw[: raw.index(b"PK\x01\x02")])


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    accepted()
    rejected()


if __name__ == "__main__":
    main()
