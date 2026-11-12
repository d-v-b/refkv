"""The documentation's examples: each one a file that runs, quoted verbatim by its page."""

import io
import re
import runpy
import struct
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import tifffile
import zarr

import refkv
import refkv.zarr
from refkv.zarrify import zarrify

DOCS = Path(__file__).resolve().parents[2] / "docs"
INCLUDE = re.compile(r"<!-- include: (\S+) -->\n```\w*\n(.*?)^```$", re.DOTALL | re.MULTILINE)


def test_pages_quote_their_examples_verbatim():
    """Every `<!-- include: path -->` marker is followed by a code block that is the
    file at `path` (relative to the page), byte for byte, and the file compiles."""
    found = {}
    for page in DOCS.rglob("*.md"):
        for path, block in INCLUDE.findall(page.read_text()):
            example = (page.parent / path).resolve()
            source = example.read_text()
            assert block == source, f"{page.name} quotes {path}, which has changed"
            compile(source, str(example), "exec")
            found[example.relative_to(DOCS).as_posix()] = page.name
    assert found.get("examples/oisst.py") == "virtual-datasets.md"
    assert found.get("examples/tiff_stack.py") == "tiff-stack.md"
    assert found.get("examples/tiff_concat.py") == "tiff-concat.md"


def test_tiff_stack_example_runs(tmp_path, monkeypatch, capsys):
    """examples/tiff_stack.py writes its synthetic stack, saves the sidecar, and finds every
    plane equal to its file, also after the directory moves (from a path with a space,
    which a source URL percent-encodes)."""
    tmp_path = tmp_path / "a b"
    tmp_path.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["tiff_stack.py"])
    runpy.run_path(str(DOCS / "examples" / "tiff_stack.py"), run_name="__main__")
    assert "all 24 planes match their files" in capsys.readouterr().out
    (tmp_path / "tiff_stack").rename(tmp_path / "moved")
    policy = refkv.Policy(prefixes=((tmp_path / "moved").as_uri() + "/",))
    zs = refkv.zarr.open(str(tmp_path / "moved" / "stack.vzip"), policy=policy)
    image = zarr.open_group(zs, mode="r").get_array("0")
    assert image.shape == (24, 256, 256)
    assert np.asarray(image[23]).any()


def test_tiff_stack_rejects_a_different_layout(tmp_path):
    """Files whose arrays differ in shape cannot be stacked."""
    example = runpy.run_path(str(DOCS / "examples" / "tiff_stack.py"))
    tifffile.imwrite(tmp_path / "a.tif", np.zeros((64, 64), np.uint16))
    tifffile.imwrite(tmp_path / "b.tif", np.zeros((64, 32), np.uint16))
    stores = [zarrify(str(tmp_path / n)) for n in ("a.tif", "b.tif")]
    with pytest.raises(ValueError, match=r"b\.tif: array '0' differs from a\.tif's"):
        example["stack"](stores, ["a.tif", "b.tif"])


def test_tiff_concat_example_runs(tmp_path, monkeypatch, capsys):
    """examples/tiff_concat.py writes its synthetic stack, saves the sidecar, and finds every
    page equal to its file, also after the directory moves (from a path with a space)."""
    tmp_path = tmp_path / "a b"
    tmp_path.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["tiff_concat.py"])
    runpy.run_path(str(DOCS / "examples" / "tiff_concat.py"), run_name="__main__")
    assert "all 24 pages match their files" in capsys.readouterr().out
    (tmp_path / "tiff_concat").rename(tmp_path / "moved")
    data = refkv.open(str(tmp_path / "moved" / "stack.vzip")).get("stack.tif")
    assert data is not None
    assert tifffile.imread(io.BytesIO(data), key=23).any()


def _pages(paths):
    """Every page of the TIFF files `paths`, in order, as arrays."""
    pages = []
    for path in paths:
        with tifffile.TiffFile(path) as tf:
            pages += [page.asarray() for page in tf.pages]
    return pages


@pytest.mark.parametrize("byteorder", ["<", ">"])
def test_tiff_concat_keeps_every_page(tmp_path, byteorder):
    """The stack's pages are the inputs' pages, in order, whatever their coding: strips of
    several rows, tiles, zlib, a predictor, RGB, floats, several pages in one file (written
    together, so their strips are contiguous, or appended, so they are not), and tags whose
    values are not inline."""
    example = runpy.run_path(str(DOCS / "examples" / "tiff_concat.py"))
    rng = np.random.default_rng(0)
    plane = rng.integers(0, 4000, (64, 96), dtype=np.uint16)
    inputs: dict[str, tuple[np.ndarray, dict[str, Any]]] = {
        "a": (plane, {}),
        "b": (plane, {"rowsperstrip": 7, "compression": "zlib", "predictor": True}),
        "c": (plane, {"tile": (32, 32)}),
        "d": (rng.integers(0, 255, (64, 96, 3), dtype=np.uint8), {"photometric": "rgb", "compression": "zlib"}),
        "e": (rng.random((3, 64, 96), dtype=np.float32), {"description": "three planes " * 10}),
    }
    paths = []
    for name, (array, options) in inputs.items():
        paths.append(tmp_path / f"{name}.tif")
        kwargs: dict[str, Any] = {"photometric": "minisblack", **options}
        tifffile.imwrite(paths[-1], array, byteorder=byteorder, **kwargs)
    paths.append(tmp_path / "f.tif")
    for z in range(3):
        tifffile.imwrite(paths[-1], plane + z, byteorder=byteorder, append=True)

    example["concat"](paths).save_vzip(tmp_path / "stack.vzip")
    data = refkv.open(str(tmp_path / "stack.vzip")).get("stack.tif")
    assert data is not None
    with tifffile.TiffFile(io.BytesIO(data)) as tf:
        assert tf.byteorder == byteorder
        got = [page.asarray() for page in tf.pages]
    expected = _pages(paths)
    assert len(got) == len(expected) == 10
    for g, e in zip(got, expected, strict=True):
        np.testing.assert_array_equal(g, e)


def _handmade(path, entries):
    """A little-endian classic TIFF of one IFD, whose entries are LONGs: `entries` maps a tag
    to its value."""
    ifd = struct.pack("<H", len(entries))
    ifd += b"".join(struct.pack("<HHII", tag, 4, 1, v) for tag, v in sorted(entries.items()))
    path.write_bytes(b"II*\0" + struct.pack("<I", 8) + ifd + struct.pack("<I", 0) + bytes(16))
    return path


# a 4 x 4 8-bit grayscale image, without its strips
_IMAGE = {256: 4, 257: 4, 258: 8, 262: 1}


def test_tiff_concat_rejects_no_files():
    example = runpy.run_path(str(DOCS / "examples" / "tiff_concat.py"))
    with pytest.raises(ValueError, match="no TIFF files"):
        example["concat"]([])


def test_tiff_concat_rejects_more_files_than_a_reader_reads(tmp_path):
    example = runpy.run_path(str(DOCS / "examples" / "tiff_concat.py"))
    with pytest.raises(ValueError, match=r"1025 files: .* at most 1024 reads"):
        example["concat"]([tmp_path / f"{i}.tif" for i in range(1025)])  # refused before any is read


def test_tiff_concat_rejects_a_bigtiff(tmp_path):
    example = runpy.run_path(str(DOCS / "examples" / "tiff_concat.py"))
    tifffile.imwrite(tmp_path / "a.tif", np.zeros((4, 4), np.uint8), bigtiff=True)
    with pytest.raises(ValueError, match=r"a\.tif: a BigTIFF file"):
        example["concat"]([tmp_path / "a.tif"])


def test_tiff_concat_rejects_mixed_byte_orders(tmp_path):
    example = runpy.run_path(str(DOCS / "examples" / "tiff_concat.py"))
    tifffile.imwrite(tmp_path / "a.tif", np.zeros((4, 4), np.uint8), byteorder="<")
    tifffile.imwrite(tmp_path / "b.tif", np.zeros((4, 4), np.uint8), byteorder=">")
    with pytest.raises(ValueError, match=r"b\.tif: byte order '>', not the first file's '<'"):
        example["concat"]([tmp_path / "a.tif", tmp_path / "b.tif"])


def test_tiff_concat_rejects_a_page_without_strips_or_tiles(tmp_path):
    example = runpy.run_path(str(DOCS / "examples" / "tiff_concat.py"))
    with pytest.raises(ValueError, match=r"a\.tif, page 0: no strips or tiles"):
        example["concat"]([_handmade(tmp_path / "a.tif", _IMAGE)])


def test_tiff_concat_rejects_an_old_style_jpeg_page(tmp_path):
    example = runpy.run_path(str(DOCS / "examples" / "tiff_concat.py"))
    page = {**_IMAGE, 259: 6, 273: 0, 278: 4, 279: 16, 513: 8, 514: 8}
    with pytest.raises(ValueError, match=r"a\.tif, page 0: an old-style JPEG page"):
        example["concat"]([_handmade(tmp_path / "a.tif", page)])


def test_tiff_concat_rejects_a_stack_of_4_gib(tmp_path):
    example = runpy.run_path(str(DOCS / "examples" / "tiff_concat.py"))
    page = {**_IMAGE, 273: 0, 278: 4, 279: 2**32 - 16}  # a strip's byte count, not its bytes
    with pytest.raises(ValueError, match="4 GiB or more"):
        example["concat"]([_handmade(tmp_path / "a.tif", page)])
