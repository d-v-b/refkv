"""The ENVI IR: the Rust parser and its schema, the image projection (spec/zarrify/envi.md) against
NumPy's reading of every accepted input's data file and GDAL's reading of the raster, the chunks'
byte ranges against VirtualiZarr's `ENVIParser`'s rule (restated here from its source,
https://github.com/zarr-developers/VirtualiZarr/blob/30221eb05a3bb0040956bda1dcc68e6bded5e149/virtualizarr/parsers/envi.py:
the VirtualiZarr this project locks predates the parser), the mirror's rebuild of both files, and
the host's side: the data file probed beside the header, from a local directory and over HTTP, where a
missing file is a 404. ENVI has no frozen Python reference; rust/refkv-ir/tests/envi.rs checks the
layout's details, and conformance/zarrify/rejects/envi.json each rejection's message."""

from __future__ import annotations

import gzip
import re
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest
import zarr
from gdal_envi import gdal
from ir_helpers import FIXTURES, ROOT

import refkv.zarr
from refkv import _core
from refkv._narrow import at, mapping, ndarray
from refkv.host import object_url
from refkv.ir.store import StoreIrOutput
from refkv.policy import Policy
from refkv.zarrify import Rejected
from refkv.zarrify import _zarrify as zarrify
from refkv.zarrify import zarrify as zarrify_named
from refkv.zarrify.profile_classes import EnviSource

sys.path.insert(0, str(ROOT / "conformance" / "zarrify"))
from proxy import Proxy

ENVI = FIXTURES / "envi"
INPUTS = sorted(p for p in ENVI.iterdir() if p.suffix in (".hdr", ".HDR") and "reject" not in p.name)
# ENVI's data type codes, as NumPy names their types
TYPES = {1: "u1", 2: "i2", 3: "i4", 4: "f4", 5: "f8", 6: "c8", 9: "c16", 12: "u2", 13: "u4", 14: "i8", 15: "u8"}
# the data's axes, outermost first, by interleave, as indexes into (bands, lines, samples)
ORDER = {"bsq": (0, 1, 2), "bil": (1, 0, 2), "bip": (1, 2, 0)}


def zarrified(path: Path, url: str | None = None, policy: Policy | None = None) -> tuple[StoreIrOutput, zarr.Group]:
    fmt, out = zarrify(str(path), url=url, policy=policy)
    assert fmt == "envi"
    assert isinstance(out, StoreIrOutput)
    return out, zarr.open_group(refkv.zarr.store(out.to_store(policy=policy)), mode="r", zarr_format=3)


def numpy_read(data: Path, fields: dict[str, str]) -> np.ndarray:
    """NumPy's reading of the data file by the header's fields (as GDAL reads them), shaped (bands,
    lines, samples): a gzip file inflated whole, the header offset's bytes skipped, the rest the
    interleave's array."""
    raw = data.read_bytes()
    if fields.get("file_compression", "0") == "1":
        raw = gzip.decompress(raw)
    shape = tuple(int(fields[k]) for k in ("bands", "lines", "samples"))
    order = ORDER[fields["interleave"].lower()]
    dtype = np.dtype(TYPES[int(fields["data_type"])]).newbyteorder(">" if fields.get("byte_order") == "1" else "<")
    stored = np.frombuffer(raw[int(fields.get("header_offset", "0")):], dtype).reshape([shape[i] for i in order])
    return stored.transpose(np.argsort(order))


@pytest.mark.parametrize("path", INPUTS, ids=lambda p: p.name)
def test_inputs_match_numpy_gdal_and_virtualizarr_and_rebuild(path, tmp_path):
    """Every accepted input's array, read back through zarr-python, is NumPy's reading of its data
    file and GDAL's of the raster (values, data type, axes `c`, `y`, `x`); its chunks are the byte
    ranges VirtualiZarr's `ENVIParser` gives (a band or a line each, or a gzip file whole); and
    both files are rebuilt from the mirror alone: the data file, which the chunks reference, is
    described in place, and the header file, which none does, is held."""
    out, root = zarrified(path)
    data = path.parent / out.summary["dataFile"]
    want, fields = gdal(data)
    assert np.array_equal(numpy_read(data, fields), want)
    got = ndarray(root.get_array("0")[...])
    assert got.dtype == want.dtype.newbyteorder("=")
    assert got.shape == want.shape
    assert np.array_equal(got, want)
    assert at(root.attrs, "refkv_zarrified", "envi", "data_file") == data.name

    # the chunks' ranges: `ENVIParser`'s rule, with its default of one chunk along the outermost axis
    gzipped = fields.get("file_compression", "0") == "1"
    order = ORDER[fields["interleave"].lower()]
    stored = [want.shape[i] for i in order]
    if gzipped:
        ranges = {"0/c/0/0/0": (0, data.stat().st_size)}
    else:
        slab = want.dtype.itemsize * stored[1] * stored[2]
        offset = int(fields.get("header_offset", "0"))
        ranges = {}
        for i in range(stored[0]):
            coords = [0, 0, 0]
            coords[order[0]] = i
            ranges["0/c/" + "/".join(map(str, coords))] = (offset + i * slab, slab)
    assert {k: [("object", data.name, *r)] for k, r in ranges.items()} == {k: list(v) for k, v in out.refs.items()}

    rebuilt = _core.rebuild_store(out.entries.get)
    assert {k for k, _, _ in rebuilt} == {path.name, data.name}
    sources = out.table()[0]
    for key, size, held in rebuilt:
        b = (path.parent / key).read_bytes()
        assert size == len(b), key
        if key == path.name:
            assert held == b, key
        else:
            assert held is None, key
            assert object_url(out.url, key) in sources, key
    out.write(str(tmp_path / "a.vzip"))


def test_the_header_is_recorded_as_written():
    """Every field is recorded, by its name in lower case, its value as written: a braced value
    over several lines with its line ends, a field the layout does not read; and the comments."""
    _, root = zarrified(ENVI / "envi_bip_float32.hdr")
    own = mapping(at(root.attrs, "refkv_zarrified", "envi"))
    assert own == {
        "fields": {
            "samples": "3", "lines": "2", "bands": "6", "header offset": "0", "file type": "ENVI Standard",
            "data type": "4", "interleave": "bip", "byte order": "0", "wavelength units": "Nanometers",
            "wavelength": "{\n 450.0, 500.0, 550.0,\n 600.0, 650.0, 700.0}",
            "map info": "{UTM, 1.000, 1.000, 500000.000, 4100000.000, 3.0, 3.0, 13, North, WGS-84, units=Meters}",
            "data ignore value": "-9999",
        },
        "comments": ["written by the refkv fixture generator"],
        "data_type": "float32", "interleave": "bip", "byte_order": "little", "compression": "none",
        "data_file": "envi_bip_float32.dat",
    }


def test_a_header_not_named_hdr_is_rejected(tmp_path):
    shutil.copy(ENVI / "envi_type_2_int16.hdr", tmp_path / "raster.txt")
    with pytest.raises(Rejected, match=re.escape('the ENVI header "raster.txt" is not named <name>.hdr')):
        zarrify(str(tmp_path / "raster.txt"))


def test_a_data_file_outside_the_scope_is_absent(tmp_path):
    """Every name the profile probes is optional (spec/zarrify/envi.md §8.3
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/envi.md#83-the-data-file>): one outside
    the scope is absent, without a request, and the next is probed."""
    shutil.copy(ENVI / "envi_bsq_uint8.hdr", tmp_path / "raster.hdr")
    for name in ("raster", "raster.dat"):
        shutil.copy(ENVI / "envi_bsq_uint8", tmp_path / name)
    _, out = zarrify(str(tmp_path / "raster.hdr"), scope=[str(tmp_path / "raster.dat")])
    assert isinstance(out, StoreIrOutput)
    assert out.summary["dataFile"] == "raster.dat"


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """The fixtures served over HTTP by the harness proxy (ranges, and a 404 for a file that is
    not there): the directory's URL, ending in `/`."""
    return Proxy(ENVI, tmp_path_factory.mktemp("cache")).base + "f/"


def test_over_http_the_data_file_is_probed_beside_the_header(server, tmp_path):
    """The host probes the data file's names beside the header, a 404 for each that is not there
    (`envi_bsq_uint8` before `envi_bsq_uint8.img`): the archive's one source is the data file, and
    the root names the header."""
    policy = Policy(allow_private_hosts=True)
    path = ENVI / "envi_header_offset.hdr"
    fmt, out = zarrify(server + path.name, policy=policy)
    assert fmt == "envi"
    assert isinstance(out, StoreIrOutput)
    assert set(out.sizes) == {"envi_header_offset.hdr", "envi_header_offset.img"}
    assert out.url == server
    out.write(str(tmp_path / "a.vzip"))
    g = zarr.open_group(refkv.zarr.open(str(tmp_path / "a.vzip"), policy=policy), mode="r", zarr_format=3)
    assert at(g.attrs, "refkv_zarrified", "source") == {"url": server + path.name}
    want, _ = gdal(ENVI / "envi_header_offset.img")
    assert np.array_equal(ndarray(g.get_array("0")[...]), want)


def test_over_http_a_missing_data_file_is_rejected(server):
    with pytest.raises(Rejected, match=re.escape('no data file beside the ENVI header "envi_reject_no_data_file.hdr"')):
        zarrify(server + "envi_reject_no_data_file.hdr", policy=Policy(allow_private_hosts=True))


def test_a_header_whose_first_line_is_not_envi_is_rejected(tmp_path):
    """Named as the ENVI profile's input (spec/zarrify.md §1.7
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#17-named-inputs-and-layouts>), a
    `.hdr` file whose first line is not `ENVI` fails the magic check, which no signature reaches."""
    (tmp_path / "raster.hdr").write_bytes(b"NOT ENVI\nsamples = 1\n")
    (tmp_path / "raster").write_bytes(b"\0")
    with pytest.raises(Rejected, match=re.escape("not an ENVI header: its first line is not ENVI")):
        zarrify_named(EnviSource(str(tmp_path / "raster.hdr")))
