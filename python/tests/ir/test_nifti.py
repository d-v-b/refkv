"""The NIfTI IR: the Rust parser and its schema, the image projection against the frozen
profile (revision 25), the mirror's rebuild, and hostile probes (huge extension counts,
an esize past the voxel data, a negative esize, dimensions past the file)."""

import json
import os
import struct
import time
from pathlib import Path

import pytest
from ir_helpers import FIXTURES, as_the_reference, assert_rebuilds, same_hierarchy
from refkv_reference import virtualize  # the frozen reference (ir_helpers)

from refkv import _core
from refkv.ir import zarrify as via_ir
from refkv.zarrify import Rejected, common

NIIS = sorted((FIXTURES / "nifti").glob("*.nii"))


@pytest.mark.parametrize("path", NIIS, ids=lambda p: p.name)
def test_projects_as_the_reference_and_rebuilds(path, tmp_path):
    as_the_reference(path, tmp_path, "nifti")


def test_edge_chunks_past_the_payload_are_copied_as_the_reference_copies_them(tmp_path, monkeypatch):
    """An edge chunk whose reference (its range and its zero padding) does not fit in a
    payload is copied into the archive, padding included (spec/zarrify/nifti.md §7.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nifti.md#72-chunks>). With
    NIfTI's chunk limit no file reaches it in practice, so both sides take a payload limit
    of 8 bytes here: every padded edge chunk is copied, by the run's last batch, and the
    hierarchy is the frozen reference's."""
    monkeypatch.setattr(common, "MAX_PAYLOAD", 8)
    _core._set_copy_threshold(8)
    copied = 0
    try:
        for path in NIIS:
            url = f"https://data.test/{path.name}"
            try:
                _, ref = virtualize(str(path), url)
            except Rejected:
                continue
            _, out, _ = via_ir(str(path), url)
            same_hierarchy(ref, out)  # the copied chunks' bytes included
            copied += sum(k.startswith("0/c/") for k in out.bytes_entries)
            data = path.read_bytes()
            assert_rebuilds(out, data, tmp_path)
    finally:
        _core._set_copy_threshold(None)
    assert copied >= 1  # nifti_chunk_edge_literal.nii's edge chunk


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
    assert_rebuilds(out, data, tmp_path, "p.vzip")
    return out, seconds


def test_a_million_empty_extensions_are_described_and_folded(tmp_path):
    """2^20 extensions of 8 bytes (the least): one element each, folded into one column
    run by the mirror; parsed in seconds, and the view stops at its budget."""
    n = 1 << 20
    chain = struct.pack("<ii", 8, 6) * n
    out, seconds = probe(tmp_path, nifti1(chain, 352 + 8 * n))
    assert out.summary["elements"] >= n
    assert out.summary["folded"]["cruns"] >= 1
    assert seconds < 30


def test_an_esize_past_the_voxels_or_negative_ends_the_chain(tmp_path):
    """The chain stops at an esize that reaches past the voxel data, or below 8: the rest
    are gaps, never read as extensions."""
    for bad in (1 << 30, -16, 7):
        chain = struct.pack("<ii", 16, 6) + b"abcdefgh" + struct.pack("<ii", bad, 6)
        out, _ = probe(tmp_path, nifti1(chain, 400))
        assert out.summary["extensions"] is True


def test_integers_past_2_53_are_text_in_the_root(tmp_path):
    """NIfTI-2 i64 fields the profile never checks (slice_start, slice_end, and dim[4..7]
    beyond n): an integer within +-(2^53 - 1) is a number in the root, any other text
    (i64::MIN included), as the reference writes them."""
    edges = [2**53 - 1, -(2**53 - 1), 2**53, -(2**53), 2**63 - 1]
    h = bytearray(540)
    struct.pack_into("<i", h, 0, 540)
    h[4:12] = b"n+2\0\r\n\x1a\n"
    struct.pack_into("<hh", h, 12, 2, 8)
    struct.pack_into("<8q", h, 16, 3, 2, 2, 2, *edges[:3], -(1 << 63))
    struct.pack_into("<q", h, 168, 544)
    struct.pack_into("<qq", h, 224, edges[3], edges[4])
    path = tmp_path / "m.nii"
    path.write_bytes(bytes(h) + bytes(4) + bytes(8))
    _, out, _ = via_ir(str(path), "https://data.test/m.nii")
    header = json.loads(out.bytes_entries["zarr.json"])["attributes"]["refkv_zarrified"]["nifti"]["header"]
    assert header["dim"][4:] == [2**53 - 1, -(2**53 - 1), str(2**53), str(-(1 << 63))]
    assert (header["slice_start"], header["slice_end"]) == (str(-(2**53)), str(2**63 - 1))
    _, ref = virtualize(str(path), "https://data.test/m.nii")
    same_hierarchy(ref, out)


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


@pytest.mark.skipif(os.environ.get("REFKV_LARGE_TESTS") != "1",
                    reason="a 16 GiB sparse file, 5 GB of memory and about 13 minutes: set REFKV_LARGE_TESTS=1")
def test_more_than_2_22_chunks_are_cut_as_the_reference_cuts_them(tmp_path, monkeypatch):
    """A NIfTI-2 uint8 volume of 4096 x 43 x 97542 voxels (a sparse file of 16 GiB): its
    rows of 43 x 4096 bytes do not fit the chunk limit with a small padding, so the cut
    moves to x, and the array has 97542 x 43 = 4194306 chunks of one row, more than 2^22
    (spec/zarrify/nifti.md §4.1
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nifti.md#41-the-array>). The IR
    cuts them all, as the frozen reference does: the archives are equivalent outside
    refkv_source."""
    monkeypatch.syspath_prepend(str(Path(__file__).parents[3] / "conformance" / "zarrify"))
    import compare  # noqa: PLC0415 (after syspath_prepend)

    x, y, z = 4096, 43, 97542
    h = bytearray(540)
    struct.pack_into("<i", h, 0, 540)
    h[4:12] = b"n+2\0\r\n\x1a\n"
    struct.pack_into("<hh", h, 12, 2, 8)
    struct.pack_into("<8q", h, 16, 3, x, y, z, 1, 1, 1, 1)
    struct.pack_into("<q", h, 168, 544)
    path = tmp_path / "big.nii"
    with open(path, "wb") as f:
        f.write(bytes(h) + bytes(4))
        f.truncate(544 + x * y * z)
    url = "https://data.test/big.nii"
    _, ref = virtualize(str(path), url)
    ref.write(str(tmp_path / "ref.vzip"))
    del ref
    _, out, _ = via_ir(str(path), url)
    assert out.summary["chunks"] == z * y > 1 << 22
    out.write(str(tmp_path / "ir.vzip"))
    del out
    a = compare._zip_view(tmp_path / "ref.vzip")
    b = compare._zip_view(tmp_path / "ir.vzip")
    assert compare.differences(compare.without_source(compare.at_revision(a)), compare.without_source(b)) == []
