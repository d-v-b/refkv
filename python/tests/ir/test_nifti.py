"""The NIfTI IR: the Rust parser and its schema, the image projection against the frozen
profile (revision 25), the mirror's rebuild, and hostile probes (huge extension counts,
an esize past the voxel data, a negative esize, dimensions past the file)."""

import io
import struct
import time

import pytest

from conftest import FIXTURES, same_hierarchy
from vzip.ir import virtualize as via_ir
from vzip.ir.cmirror import rebuild_from_archive
from vzip.virtualize import Rejected
from vzip_reference import virtualize  # the frozen reference (conftest)

NIIS = sorted((FIXTURES / "nifti").glob("*.nii"))


@pytest.mark.parametrize("path", NIIS, ids=lambda p: p.name)
def test_projects_as_the_reference_and_rebuilds(path, tmp_path):
    data = path.read_bytes()
    url = f"https://data.test/{path.name}"
    try:
        _, ref = virtualize(str(path), url)
    except Rejected:
        with pytest.raises(Rejected):
            via_ir(str(path), url)
        return
    fmt, out, ir = via_ir(str(path), url)
    assert fmt == "nifti"
    ir.check()
    same_hierarchy(ref, out)
    out.write(str(tmp_path / "a.vzip"))
    buf = io.BytesIO()
    rebuild_from_archive(str(tmp_path / "a.vzip"), lambda o, n: data[o:o + n], buf.write)
    assert buf.getvalue() == data


def nifti1(chain: bytes, vox: int, voxels: bytes = bytes(8), dims=(1, 8)) -> bytes:
    """A little-endian NIfTI-1 uint8 file: the header, the extender 1, `chain` up to `vox`
    (zero-padded), then the voxels."""
    h = bytearray(348)
    struct.pack_into("<i", h, 0, 348)
    dim = list(dims) + [1] * (8 - len(dims))
    struct.pack_into("<8h", h, 40, *dim)
    struct.pack_into("<hh", h, 70, 2, 8)
    struct.pack_into("<f", h, 108, float(vox))
    struct.pack_into("<f", h, 112, 1.0)
    h[344:348] = b"n+1\0"
    body = bytes(h) + b"\1\0\0\0" + chain
    return body + bytes(vox - len(body)) + voxels


def probe(tmp_path, data: bytes):
    """(the output, seconds), or Rejected; the archive must rebuild the file."""
    path = tmp_path / "p.nii"
    path.write_bytes(data)
    t = time.perf_counter()
    _, out, ir = via_ir(str(path), "https://data.test/p.nii")
    seconds = time.perf_counter() - t
    ir.check()
    out.write(str(tmp_path / "p.vzip"))
    buf = io.BytesIO()
    rebuild_from_archive(str(tmp_path / "p.vzip"), lambda o, n: data[o:o + n], buf.write)
    assert buf.getvalue() == data
    return out, seconds


def test_a_million_empty_extensions_are_described_and_folded(tmp_path):
    """2^20 extensions of 8 bytes (the least): one element each, folded into one column
    run by the mirror; parsed in seconds, and the view stops at its budget."""
    n = 1 << 20
    chain = struct.pack("<ii", 8, 6) * n
    out, seconds = probe(tmp_path, nifti1(chain, 352 + 8 * n))
    assert out.summary["elements"] >= n and out.summary["folded"]["cruns"] >= 1
    assert seconds < 30


def test_an_esize_past_the_voxels_or_negative_ends_the_chain(tmp_path):
    """The chain stops at an esize that reaches past the voxel data, or below 8: the rest
    are gaps, never read as extensions."""
    for bad in (1 << 30, -16, 7):
        chain = struct.pack("<ii", 16, 6) + b"abcdefgh" + struct.pack("<ii", bad, 6)
        out, _ = probe(tmp_path, nifti1(chain, 400))
        assert out.summary["extensions"] is True


def test_dimensions_past_the_file_are_rejected(tmp_path):
    """Huge dimensions whose voxel data the file does not hold: rejected, not read."""
    path = tmp_path / "d.nii"
    path.write_bytes(nifti1(b"", 352, dims=(3, 32767, 32767, 32767)))
    with pytest.raises(Rejected, match="voxel data at 352 is outside"):
        via_ir(str(path), "https://data.test/d.nii")


def test_a_large_extension_is_not_read(tmp_path):
    """One 64 MiB extension: described by its 8-byte head, its data never read."""
    big = 64 << 20
    chain = struct.pack("<ii", big, 4) + bytes(big - 8)
    data = nifti1(chain, 352 + big)
    path = tmp_path / "l.nii"
    path.write_bytes(data)
    _, out, _ = via_ir(str(path), "https://data.test/l.nii")
    assert out.summary["planner"]["asked"] < 1 << 21
