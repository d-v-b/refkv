"""The documentation's examples: each one a file that runs, quoted verbatim by its page."""

import re
import runpy
from pathlib import Path

import numpy as np
import pytest
import tifffile
import zarr

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


def test_tiff_stack_example_runs(tmp_path, monkeypatch, capsys):
    """examples/tiff_stack.py writes its synthetic stack, saves the sidecar, and finds every
    plane equal to its file, also after the directory moves."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["tiff_stack.py"])
    runpy.run_path(str(DOCS / "examples" / "tiff_stack.py"), run_name="__main__")
    assert "all 24 planes match their files" in capsys.readouterr().out
    (tmp_path / "tiff_stack").rename(tmp_path / "moved")
    image = zarr.open_group(refkv.zarr.open(str(tmp_path / "moved" / "stack.vzip")), mode="r").get_array("0")
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
