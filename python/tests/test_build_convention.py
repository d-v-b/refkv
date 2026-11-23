"""The routines of `refkv.build` that bind what every built-in projection calls: `grid`, `filled`,
`gzip_member`, `mirror` and `declaration`. `test_rebuild_npy.py` shows that a projection written
with them writes refkv's own output; here each is checked on its own, and each of its errors."""

import gzip
import json
import math
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest

from refkv import _core
from refkv.build import (
    Copy,
    Data,
    Literal,
    Ref,
    Value,
    Zeros,
    declaration,
    filled,
    grid,
    gzip_member,
    mirror,
    project,
    read,
)
from refkv.zarrify import Rejected

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
NPY = str(FIXTURES / "npy" / "npy_c_float64.npy")


# an edge chunk's padded reference passes the payload limit at 64 bytes, so that small arrays have
# copied chunks
SMALL_PAYLOADS = _core.Chunking(copy_threshold=64)


def _array(tmp_path, shape, dtype="|u1"):
    """A NumPy file of an array of `shape`, and the offset of its data."""
    path = tmp_path / "a.npy"
    values = np.arange(math.prod(shape), dtype=dtype).reshape(shape)
    np.save(path, values)
    return str(path), path.stat().st_size - values.nbytes, values


@pytest.mark.parametrize(("shape", "dtype", "limit"), [
    ((), "<i4", 1 << 20),  # a scalar: one chunk of no coordinates
    ((6, 4), "<i2", 1 << 20),  # the whole array in one chunk
    ((6, 4), "<i2", 16),  # cut along the first axis, evenly
    ((7, 5), "|u1", 10),  # an edge chunk, padded with zeros
    ((7, 100), "|u1", 300),  # an edge chunk whose padding does not fit: copied
    ((3, 2, 50), "|u1", 60),  # cut along a deeper axis
    ((0, 3), "<f4", 1 << 20),  # no elements: no chunks
])
def test_grid_cuts_contiguous_values_into_chunks_that_tile_them(tmp_path, shape, dtype, limit):
    """Each chunk is a range of the span, in C order and at most `limit` bytes; an edge chunk is
    padded with zeros to the chunk shape, or copied, as the input's chunking says; and the chunks,
    filled and laid on the grid, hold the array's values."""
    path, offset, values = _array(tmp_path, shape, dtype)
    item = values.dtype.itemsize
    with read(path, chunking=SMALL_PAYLOADS) as p:
        chunk_shape, chunks = grid(Ref(p, 0, offset, values.nbytes), shape, item, limit=limit)
        assert len(chunk_shape) == len(shape)
        full = math.prod(chunk_shape) * item
        assert full <= max(limit, item)
        counts = [-(-n // c) for n, c in zip(shape, chunk_shape, strict=True)]
        assert set(chunks) == (set(np.ndindex(*counts)) if values.size else set())
        padded = np.zeros([n * c for n, c in zip(counts, chunk_shape, strict=True)], dtype=values.dtype)
        kinds = set()
        for coords, v in chunks.items():
            head = v.parts[0]
            kinds.add((type(head).__name__, len(v.parts)))
            assert [type(q) for q in v.parts[1:]] in ([], [Zeros])
            assert v.size == full if shape else v.size == item
            data = filled(v)
            if isinstance(data, Value):  # a reference: its range, read here, then its zeros
                assert isinstance(head, Ref)
                data = p.bytes((head.source, head.offset, head.length)) + bytes(v.size - head.length)
            at = tuple(slice(i * c, (i + 1) * c) for i, c in zip(coords, chunk_shape, strict=True))
            padded[at] = np.frombuffer(data, dtype=values.dtype).reshape(chunk_shape)
        assert np.array_equal(padded[tuple(slice(0, n) for n in shape)], values)
        if (shape, limit) == ((7, 5), 10):
            assert ("Ref", 2) in kinds
        if (shape, limit) == ((7, 100), 300):
            assert ("Copy", 2) in kinds


def test_grid_refuses_a_span_that_is_not_a_ref():
    with read(NPY) as p, pytest.raises(TypeError, match="a span is a Ref"):
        grid(Value((Ref(p, 0, 0, 8),)), [1], 8, limit=64)  # type: ignore[arg-type]


def test_grid_refuses_a_span_of_another_length_than_the_array():
    with read(NPY) as p, pytest.raises(ValueError, match="is 96 bytes, and the span 8"):
        grid(Ref(p, 0, 128, 8), [3, 4], 8, limit=64)


def test_grid_refuses_an_element_larger_than_a_chunk():
    with read(NPY) as p, pytest.raises(ValueError, match="larger than a chunk's 4"):
        grid(Ref(p, 0, 128, 96), [12], 8, limit=4)


def test_grid_refuses_an_element_of_no_bytes():
    with read(NPY) as p, pytest.raises(ValueError, match="at least a byte"):
        grid(Ref(p, 0, 128, 0), [12], 0, limit=4)


def test_filled_refuses_a_ref_beside_a_copy():
    with read(NPY) as p, pytest.raises(ValueError, match="a Ref beside a Copy"):
        filled((Copy(Ref(p, 0, 0, 4)), Ref(p, 0, 4, 4)))


def test_filled_writes_out_what_stands_beside_a_copy():
    with read(NPY) as p:
        head = Path(NPY).read_bytes()[:6]
        value = (Literal(b"<"), Copy(Ref(p, 0, 0, 6)), Data(b"abcdef", 2, 3), Zeros(2))
        assert filled(value) == b"<" + head + b"cde\0\0"
        kept = Value((Ref(p, 0, 0, 6), Zeros(2)))
        assert filled(kept) is kept
        assert filled(b"chunk") == b"chunk"


def test_gzip_member_frames_a_deflate_stream_for_the_gzip_codec():
    """The header, the stream as it is, and the trailer: what gzip reads back as the data."""
    data = bytes(range(256)) * 9
    raw = zlib.compressobj(wbits=-15)
    stream = raw.compress(data) + raw.flush()
    member = gzip_member(Data(stream), crc32=zlib.crc32(data), size=len(data))
    assert [type(q) for q in member.parts] == [Literal, Data, Literal]
    assert member.parts[1] == Data(stream)
    assert gzip.decompress(b"".join(q.data for q in member.parts)) == data  # type: ignore[union-attr]


def test_gzip_member_refuses_a_checksum_that_is_not_32_bits():
    with pytest.raises(ValueError, match="a CRC-32 is 0 to 2\\^32 - 1"):
        gzip_member(Data(b"x"), crc32=1 << 32, size=1)


def _placed(written):
    """Each bytes entry's placement: whether it is read at open, and whether it is kept stored."""
    return {k: (written.at_open.get(k, k.rpartition("/")[2] == "zarr.json"), k in written.stored)
            for k, v in written.items() if isinstance(v, bytes)}


@pytest.mark.parametrize(("name", "recorded"), [
    ("npy/npz_compressed.npz", None), ("tiff/strips_uint8.tif", None), ("hdf5/hdf5_attributes.h5", None),
    ("fits/fits_bintable.fits", None), ("n5/n5_hierarchy", None),
    # what a listing ignored, which the convention records beside the mirror: an archive's, a store's
    ("zarr3/zarr3_deflated_documents.zip", "ignored"), ("safe/safe_l2a_pb0207", "empty_dirs"),
    ("safe/safe_l2a_pb0207.SAFE.zip", "empty_dirs"),
])
def test_the_mirror_is_the_one_refkv_s_own_output_holds(name, recorded):
    """The same entries under `refkv_source`, placed as refkv places them: all of them for a file,
    and for a store all but its objects (`refkv_source/objects`), which its projection adds. What
    a listing ignored is in the mirror's source metadata, as refkv's own output records it."""
    with read(str(FIXTURES / name)) as p:
        own = project(p).entries("refkv_source/")
        mine = mirror(p)
        objects = {k for k in own if k.startswith("refkv_source/objects/")}
        assert bool(objects) == (not p.plain_file)
        assert dict(mine) == {k: v for k, v in own.items() if k not in objects}
        assert _placed(mine) == {k: v for k, v in _placed(own).items() if k not in objects}
        assert any(not at_open for at_open, _ in _placed(mine).values())
        assert all(isinstance(v, bytes) for v in mine.values())
        node = mine["refkv_source/zarr.json"]
        assert isinstance(node, bytes)
        metadata = json.loads(node)["attributes"]["refkv_zarrified"][p.profile]
        assert {"ignored", "empty_dirs"} & set(metadata) == ({recorded} if recorded else set())


def test_a_mirror_past_its_budget_is_rejected():
    class OverBudget:
        def entries(self):
            raise _core.Rejected("budget: the mirror would have more than 4194305 rows")

    with read(NPY) as p:
        p._mirrored = OverBudget()  # type: ignore[assignment]
        with pytest.raises(Rejected, match="the mirror would have more than"):
            mirror(p)


def test_the_mirror_is_made_whatever_else_uses_the_input():
    """The mirror reads nothing and borrows nothing of the run: it is the same made of an open
    input, of a closed one, and from several threads while the input's own output is being made."""
    path = str(FIXTURES / "safe" / "safe_l2a_pb0207")
    with read(path) as p:
        before = dict(mirror(p))
        with ThreadPoolExecutor(8) as pool:
            output = pool.submit(project, p)
            mirrors = [pool.submit(mirror, p) for _ in range(16)]
            assert all(dict(m.result()) == before for m in mirrors)
            assert dict(output.result().entries("refkv_source/zarr.json")) == {
                "refkv_source/zarr.json": before["refkv_source/zarr.json"]}
    assert dict(mirror(p)) == before


def test_a_declaration_is_the_one_refkv_s_own_output_writes():
    """The root's names the profile, its version and the source (by the input's URL, or the one
    given), with the source metadata; a node below declares only where it has source metadata, and
    is otherwise left its attributes."""
    with read(NPY, "https://example.org/a.npy") as p:
        description = p.description
        assert description is not None
        root = declaration(p, own=description["own"], attributes={"title": "kept"})
        own = project(p).document("zarr.json")["attributes"]
        assert root == {**own, "title": "kept"}
        assert root["refkv_zarrified"]["source"] == {"url": "https://example.org/a.npy"}
        assert root["refkv_zarrified"]["npy"] == description["own"]
        below = declaration(p, "array", own={"note": 1})
        assert below["refkv_zarrified"] == {"npy": {"note": 1}}
        assert below["zarr_conventions"] == root["zarr_conventions"]
        assert declaration(p, "array", attributes={"title": "kept"}) == {"title": "kept"}
        assert declaration(p) == {k: v for k, v in root.items() if k != "title"} | {
            "refkv_zarrified": {k: v for k, v in root["refkv_zarrified"].items() if k != "npy"}}
        # the URL the root names the source by, where it is not the input's (an HDF5 store's
        # named file); a node below names none
        named = declaration(p, url="https://example.org/named.h5")
        assert named["refkv_zarrified"]["source"] == {"url": "https://example.org/named.h5"}
        assert declaration(p, "array", own={"note": 1}, url="https://example.org/named.h5") == below


def test_a_declaration_refuses_a_path_that_is_not_a_string():
    with read(NPY) as p, pytest.raises(TypeError, match="a path is a str"):
        declaration(p, 3)  # type: ignore[arg-type]
