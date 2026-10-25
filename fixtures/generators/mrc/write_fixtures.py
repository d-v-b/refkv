"""Writes synthetic MRC2014 files to fixtures/mrc/, covering the rules of the MRC profile
(spec/zarrify/mrc.md §8
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mrc.md#8-mrc-profile>): both byte
orders, every mode, images, image stacks, volumes and volume stacks, the axis map, voxel
sizes, origins, display windows, extended headers, chunking, and the inputs the profile
rejects (`mrc_reject_*`).

Most files are packed field by field here, so that every header value is exactly what the
case needs; a few are written by mrcfile, as real files are. js/test/mrc/verify.py
compares each accepted file's zarrification with mrcfile's reading of it.

Usage: uv run python fixtures/generators/mrc/write_fixtures.py
"""

from __future__ import annotations

import math
import struct
from pathlib import Path

import mrcfile
import numpy as np

OUT = Path(__file__).parents[2] / "mrc"

# Offset and struct format of the header fields (spec/zarrify/mrc.md §2
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mrc.md#2-the-header>).
LAYOUT = {
    "nx": (0, "i"), "ny": (4, "i"), "nz": (8, "i"), "mode": (12, "i"),
    "nxstart": (16, "i"), "nystart": (20, "i"), "nzstart": (24, "i"),
    "mx": (28, "i"), "my": (32, "i"), "mz": (36, "i"),
    "cella": (40, "3f"), "cellb": (52, "3f"),
    "mapc": (64, "i"), "mapr": (68, "i"), "maps": (72, "i"),
    "dmin": (76, "f"), "dmax": (80, "f"), "dmean": (84, "f"),
    "ispg": (88, "i"), "nsymbt": (92, "i"),
    "extra1": (96, "8s"), "exttyp": (104, "4s"), "nversion": (108, "i"), "extra2": (112, "84s"),
    "origin": (196, "3f"), "map": (208, "4s"), "machst": (212, "4s"),
    "rms": (216, "f"), "nlabl": (220, "i"),
}
# mode: (numpy type, bytes per voxel)
MODES = {0: ("i1", 1), 1: ("i2", 2), 2: ("f4", 4), 4: ("c8", 8), 6: ("u2", 2), 12: ("f2", 2)}
MACHST = {"<": b"\x44\x44\0\0", ">": b"\x11\x11\0\0"}


def voxels(shape, mode: int, order: str, seed: int) -> bytes:
    """Distinct values for every voxel, columns fastest, in the file's byte order."""
    np_type, _ = MODES[mode]
    count = math.prod(shape)
    values = (np.arange(count, dtype=np.int64) * 7 + seed) % 251 - (0 if np_type[0] == "u" else 100)
    if np_type == "c8":
        return (values + 1j * (values[::-1])).astype(order + "c8").tobytes()
    return np.asarray(values).astype(order + np_type).tobytes()


def mrc(name: str, dims, mode: int = 2, *, order: str = "<", ispg: int = 1, data: bytes | None = None,
        extended: bytes = b"", truncate: int = 0, labels: list[bytes] | None = None, seed: int = 0,
        trailer: bytes = b"", **fields) -> None:
    """Packs an MRC2014 file: the 1024-byte header, the extended header, then the data.
    `dims` is (nx, ny, nz); `cella` defaults to the grid (a voxel size of 1 angstrom)."""
    header = bytearray(1024)
    nx, ny, nz = dims
    values = {
        "nx": nx, "ny": ny, "nz": nz, "mode": mode, "mx": nx, "my": ny, "mz": nz if ispg else 1,
        "cella": [float(nx), float(ny), float(nz if ispg else 1)], "cellb": [90.0, 90.0, 90.0],
        "mapc": 1, "mapr": 2, "maps": 3, "dmin": 0.0, "dmax": 0.0, "dmean": 0.0, "ispg": ispg,
        "nsymbt": len(extended), "exttyp": b"\0\0\0\0" if not extended else b"CCP4", "nversion": 20140,
        "origin": [0.0, 0.0, 0.0], "map": b"MAP ", "machst": MACHST[order], "rms": 0.0,
        "nlabl": len(labels or []),
    }
    values.update(fields)
    for key, value in values.items():
        offset, fmt = LAYOUT[key]
        if fmt.endswith("s"):
            struct.pack_into(fmt, header, offset, value)
        else:
            struct.pack_into(order + fmt, header, offset, *(value if isinstance(value, list) else [value]))
    for k, label in enumerate(labels or []):
        header[224 + 80 * k:224 + 80 * k + 80] = label.ljust(80, b" ")[:80]
    if data is None:
        data = voxels([max(d, 0) for d in dims], mode if mode in MODES else 2, order, seed)
    content = bytes(header) + extended + data + trailer
    (OUT / f"{name}.mrc").write_bytes(content[: len(content) - truncate])


def ccp4_symmetry(ops: list[str]) -> bytes:
    """A CCP4 extended header: 80-byte records of symmetry operators, space-padded."""
    return b"".join(op.encode().ljust(80, b" ") for op in ops)


def by_mrcfile() -> None:
    """Files as mrcfile writes them (with a fixed label in place of its timestamp)."""
    rng = np.random.default_rng(1)

    def write(name: str, data, voxel_size, stack: bool = False, origin=None) -> None:
        with mrcfile.new(OUT / f"{name}.mrc", overwrite=True) as f:
            f.set_data(data)
            if stack:
                f.set_image_stack()
            f.voxel_size = voxel_size
            if origin is not None:
                f.header.origin.x, f.header.origin.y, f.header.origin.z = origin
            f.header.label[0] = "written by write_fixtures.py".ljust(80)
            f.update_header_stats()

    write("mrc_mrcfile_volume", rng.standard_normal((4, 5, 6)).astype(np.float32), 1.25, origin=(-3.0, 2.5, 10.0))
    write("mrc_mrcfile_stack", rng.integers(0, 4000, (3, 5, 6), dtype=np.int16), (0.5, 0.5, 1.0), stack=True)
    write("mrc_mrcfile_image", rng.integers(0, 255, (7, 6), dtype=np.uint16), 2.0)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for p in OUT.glob("*.mrc"):
        p.unlink()
    by_mrcfile()

    # Volumes, both byte orders, with a voxel size, an origin and a display window.
    mrc("mrc_volume_float32_le", (4, 3, 2), 2, cella=[6.0, 4.5, 3.0], origin=[-1.5, 2.0, 7.25],
        dmin=-100.0, dmax=150.0, dmean=3.5, rms=12.0, labels=[b"a float32 volume", b"two labels"])
    mrc("mrc_volume_int16_be", (3, 4, 2), 1, order=">", cella=[1.5, 2.0, 1.0], dmin=-100.0, dmax=100.0)
    # An image (ispg 0, one section), an image stack, and a volume stack (ispg 401, two volumes of 3 sections).
    mrc("mrc_image_int8", (5, 3, 1), 0, ispg=0, cella=[10.0, 6.0, 1.0], dmin=-100.0, dmax=100.0)
    mrc("mrc_image_stack_uint16", (4, 3, 5), 6, ispg=0, machst=b"\x44\x41\0\0", cella=[4.0, 3.0, 1.0])
    mrc("mrc_volume_stack_float16", (3, 2, 6), 12, ispg=401, mz=3, cella=[3.0, 2.0, 3.0], dmin=-100.0, dmax=100.0)
    mrc("mrc_volume_complex64", (3, 2, 2), 4, dmin=-100.0, dmax=100.0)
    for mode, name in [(0, "int8"), (1, "int16"), (6, "uint16"), (12, "float16"), (2, "float32_be")]:
        mrc(f"mrc_mode_{name}", (3, 3, 2), mode, order=">" if name.endswith("_be") else "<", seed=mode)

    # The axis map: columns, rows and sections along x, y and z in another order; not a permutation.
    mrc("mrc_axis_map_permuted", (4, 3, 2), 2, mapc=3, mapr=2, maps=1, cella=[8.0, 6.0, 4.0],
        origin=[1.0, 2.0, 3.0])
    mrc("mrc_axis_map_permuted_image", (4, 3, 1), 2, ispg=0, mapc=2, mapr=1, maps=3, cella=[8.0, 6.0, 1.0])
    mrc("mrc_axis_map_zero", (4, 3, 2), 2, mapc=0, mapr=0, maps=0)
    mrc("mrc_axis_map_duplicate", (4, 3, 2), 2, mapc=1, mapr=1, maps=3)
    mrc("mrc_axis_map_out_of_range", (4, 3, 2), 2, mapc=1, mapr=2, maps=4)

    # Header values that change the output but are never rejected.
    nan, inf = float("nan"), float("inf")
    mrc("mrc_edge_window_reversed", (3, 3, 2), 2, dmin=10.0, dmax=-10.0)
    mrc("mrc_edge_window_nan", (3, 3, 2), 2, dmin=nan, dmax=10.0)
    mrc("mrc_edge_window_inf", (3, 3, 2), 2, dmin=0.0, dmax=inf)
    mrc("mrc_edge_cella_zero", (3, 3, 2), 2, cella=[0.0, 3.0, 2.0])
    mrc("mrc_edge_cella_negative", (3, 3, 2), 2, cella=[3.0, -3.0, 2.0])
    mrc("mrc_edge_cella_nan", (3, 3, 2), 2, cella=[3.0, 3.0, nan])
    mrc("mrc_edge_m_zero", (3, 3, 2), 2, mx=0, cella=[3.0, 3.0, 2.0])
    mrc("mrc_edge_m_negative", (3, 3, 2), 2, my=-3, cella=[3.0, 3.0, 2.0])
    mrc("mrc_edge_m_larger", (3, 3, 2), 2, mx=6, my=6, mz=4, cella=[3.0, 3.0, 2.0])
    mrc("mrc_edge_origin_nan", (3, 3, 2), 2, origin=[1.0, nan, 0.0])
    mrc("mrc_edge_origin_inf", (3, 3, 2), 2, origin=[inf, 0.0, 0.0])
    mrc("mrc_edge_origin_one", (3, 3, 2), 2, origin=[0.0, 0.0, -2.5])
    # An image stack's origin is a phase origin in pixels: no translation.
    mrc("mrc_edge_origin_image_stack", (3, 3, 2), 2, ispg=0, origin=[1.0, 2.0, 0.0])
    mrc("mrc_edge_origin_image", (3, 3, 1), 2, ispg=0, origin=[1.0, 2.0, 0.0])
    # IMOD's stamp and flags at bytes 152 and 156: bit 0 makes mode 0 unsigned.
    imod = lambda flags: bytes(40) + struct.pack("<ii", 1146047817, flags) + bytes(36)
    mrc("mrc_imod_uint8", (4, 3, 2), 0, extra2=imod(1), dmin=0.0, dmax=255.0)
    mrc("mrc_imod_uint8_other_flags", (4, 3, 2), 0, extra2=imod(7))
    mrc("mrc_imod_int8", (4, 3, 2), 0, extra2=imod(2))
    mrc("mrc_imod_flag_no_stamp", (4, 3, 2), 0, extra2=bytes(44) + struct.pack("<i", 1) + bytes(36))
    mrc("mrc_imod_uint8_be", (4, 3, 2), 0, order=">", extra2=bytes(40) + struct.pack(">ii", 1146047817, 1) + bytes(36))
    mrc("mrc_edge_volume_stack_one", (3, 2, 3), 2, ispg=401, mz=3)
    mrc("mrc_edge_ispg_230", (3, 2, 2), 2, ispg=230)
    mrc("mrc_edge_ispg_negative", (3, 2, 2), 2, ispg=-1)
    mrc("mrc_edge_version_old", (3, 2, 2), 2, nversion=0, map=b"MAP\0")
    mrc("mrc_edge_nz_one_volume", (3, 2, 1), 2, ispg=1)

    # Header bytes: a negative zero and a NaN payload, latin-1 text, text after a NUL, extra space.
    mrc("mrc_header_float_bits", (3, 2, 2), 2, dmean=-0.0, rms=struct.unpack("<f", b"\x01\x00\xc0\x7f")[0])
    mrc("mrc_header_latin1", (3, 2, 2), 2, labels=[b"caf\xe9 au lait", b"\xff\xfe"])
    mrc("mrc_header_text_after_nul", (3, 2, 2), 2, labels=[b"visible\0hidden after the NUL"],
        exttyp=b"\0abc", extra1=b"\x01\x02\x03\x04\x05\x06\x07\x08", extra2=bytes(range(84)))
    mrc("mrc_header_ten_labels", (3, 2, 2), 2, labels=[f"label {k}".encode() for k in range(10)])

    # Extended headers: CCP4 symmetry records (text), an FEI1 header (opaque), a large one.
    mrc("mrc_extended_ccp4", (3, 2, 2), 2, extended=ccp4_symmetry(["X,Y,Z", "-X,-Y,Z"]), ispg=3)
    mrc("mrc_extended_fei1", (3, 2, 2), 2, extended=bytes(range(256)) * 3, exttyp=b"FEI1")
    mrc("mrc_extended_large", (3, 2, 2), 2, extended=bytes(5000), exttyp=b"SERI")
    mrc("mrc_extended_empty_type", (3, 2, 2), 2, extended=bytes(16), exttyp=b"\0\0\0\0")

    # Bytes after the data: a gap.
    mrc("mrc_region_trailer", (3, 2, 2), 2, trailer=b"TRAILER!")

    # Chunks (spec/zarrify/mrc.md §4.1
    # <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mrc.md#41-the-array>): 20 sections of
    # 64 KiB, cut into 2 chunks of 10; a section of 1.1 MB, cut into 2 blocks of 550 rows; a row
    # of 2^20 + 1 bytes, cut into 2 chunks, the second padded with a literal zero byte.
    mrc("mrc_chunk_sections", (256, 128, 20), 1, ispg=0)
    mrc("mrc_chunk_rows", (1000, 1100, 1), 0, ispg=0)
    mrc("mrc_chunk_edge_literal", (2**20 + 1, 1, 1), 0, ispg=0)

    # Rejected, one per rule of spec/zarrify/mrc.md §8.1
    # <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mrc.md#81-header>.
    mrc("mrc_reject_short_header", (3, 2, 2), 2, data=b"", truncate=1024 - 500)
    mrc("mrc_reject_machst", (3, 2, 2), 2, machst=b"\0\0\0\0")
    mrc("mrc_reject_machst_swapped", (3, 2, 2), 2, machst=b"\x41\x44\0\0")
    mrc("mrc_reject_nx_zero", (0, 2, 2), 2)
    mrc("mrc_reject_ny_negative", (3, -2, 2), 2, data=b"")
    mrc("mrc_reject_nz_zero", (3, 2, 0), 2)
    mrc("mrc_reject_mode_complex_int16", (3, 2, 2), 3, data=bytes(48))
    mrc("mrc_reject_mode_int4", (3, 2, 2), 101, data=bytes(6))
    mrc("mrc_reject_mode_unknown", (3, 2, 2), 7, data=bytes(48))
    mrc("mrc_reject_mode_negative", (3, 2, 2), -1, data=bytes(48))
    mrc("mrc_reject_nsymbt_negative", (3, 2, 2), 2, nsymbt=-80)
    mrc("mrc_reject_extended_outside", (3, 2, 2), 2, nsymbt=1000)
    mrc("mrc_reject_volume_stack_mz", (3, 2, 5), 2, ispg=401, mz=2)
    mrc("mrc_reject_volume_stack_mz_zero", (3, 2, 4), 2, ispg=401, mz=0)
    mrc("mrc_reject_truncated", (3, 2, 2), 2, truncate=1)
    mrc("mrc_reject_data_overflow", (2**30, 2**30, 2**30), 2, data=bytes(48))
    mrc("mrc_reject_data_outside", (3, 2, 2), 2, extended=bytes(16), data=bytes(47))


if __name__ == "__main__":
    main()
