"""`refkv.build.fits_arrays` and `fits_cards`: a FITS HDU's arrays and cards as the FITS
convention writes them; `refkv.build.hdf5_hierarchy`, an HDF5 input's hierarchy as the HDF5
convention presents it: each made from the parser's description and the IR, with no output of
refkv's. Each is checked against refkv's own output of every accepted fixture of its format, and
for each of its errors."""

import json
import re
from pathlib import Path

import h5py
import netCDF4
import numpy as np
import pytest

from refkv import _core
from refkv.build import (
    Parsed,
    filled,
    fits_arrays,
    fits_cards,
    hdf5_hierarchy,
    netcdf3_hierarchy,
    project,
    read,
    seal,
)
from refkv.zarrify import Rejected
from refkv.zarrify.layouts.examples.hdf5_rechunk import Hdf5Rechunk
from refkv.zarrify.layouts.examples.xarray_v2 import XarrayV2
from refkv.zarrify.layouts.virtualizarr import VirtualiZarr

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
FITS = sorted(p for p in (FIXTURES / "fits").iterdir() if p.is_file() and ".fits" in p.name and "reject" not in p.name)


@pytest.fixture(params=[None, 64], ids=["payload-limit", "copied-edges"])
def copy_threshold(request):
    """The payload past which an edge chunk is copied: refkv's, and one small enough that the
    fixtures' edge chunks are copied."""
    _core._set_copy_threshold(request.param)
    yield request.param
    _core._set_copy_threshold(None)


def edged(tmp_path: Path) -> Path:
    """A FITS image of more than a chunk's bytes whose rows do not divide evenly into chunks, so
    that its last chunk is an edge chunk, padded or copied: no fixture has one."""
    fits = pytest.importorskip("astropy.io.fits")
    path = tmp_path / "edged.fits"
    fits.PrimaryHDU(np.arange(1031 * 1100, dtype=np.uint8).reshape(1031, 1100)).writeto(path)
    return path


def test_an_hdu_s_arrays_and_cards_are_those_refkv_s_own_output_writes(tmp_path, copy_threshold):
    """For every HDU of every accepted fixture: each array's document is refkv's own but for the
    declarations in its attributes, its chunks are refkv's (a copied chunk as the bytes refkv
    copies), and the cards are those refkv's source metadata gives, where it gives them. Images,
    tables' rows and heaps, tile-compressed images, files compressed whole and an edge chunk among
    them."""
    seen = {"image": 0, "rows": 0, "heap": 0, "tiled": 0, "copied": 0, "cards past the budget": 0}
    for path in [*FITS, edged(tmp_path)]:
        with read(str(path)) as parsed:
            built = project(parsed)
            description = parsed.description
            assert description is not None
            for hdu in description["hdus"]:
                index = hdu["index"]
                cards = fits_cards(parsed, index)
                theirs = (built.source_metadata(str(index)) or {}).get("cards")
                if theirs is None:
                    seen["cards past the budget"] += hdu["kind"] != "empty"
                else:
                    assert cards == theirs, f"{path.name} {index}"
                for under, array in fits_arrays(parsed, index).items():
                    node = f"{index}/{under}".rstrip("/")
                    doc, chunks = built.array_entries(node)
                    assert array.document() == {**doc, "attributes": {}}, f"{path.name} {node}"
                    mine = {k[len(node) + 1:]: filled(v) if not isinstance(v, bytes) else v
                            for k, v in array.entries(to=node).items() if not k.endswith("zarr.json")}
                    assert mine == chunks, f"{path.name} {node}"
                    kind = "tiled" if hdu["compression"] else (under or "image")
                    seen[kind] += 1
                    seen["copied"] += any(isinstance(v, bytes) for v in chunks.values())
    assert all(seen[k] for k in ("image", "rows", "heap", "tiled")), seen
    assert copy_threshold is None or seen["copied"], seen


def test_the_routines_ask_for_no_output_of_refkv_s(monkeypatch):
    async def refuse(_self):
        raise AssertionError("refkv's own output was asked for")

    monkeypatch.setattr(Parsed, "_output_async", refuse)
    with read(FIXTURES / "fits" / "fits_comp_rice_int16.fits") as parsed:
        assert set(fits_arrays(parsed, 1)) == {""}
        assert fits_cards(parsed, 1)[0][0] == "XTENSION"


def test_fits_arrays_refuses_an_input_that_is_not_a_fits_file():
    with read(FIXTURES / "npy" / "npy_c_float64.npy") as parsed, pytest.raises(ValueError, match="not a npy input"):
        fits_arrays(parsed, 0)


def test_fits_arrays_refuses_an_hdu_the_file_does_not_have():
    with read(FIXTURES / "fits" / "fits_mef_sci_err_dq.fits") as parsed, pytest.raises(ValueError, match="no HDU 9"):
        fits_arrays(parsed, 9)


def test_fits_cards_refuses_an_input_that_is_not_a_fits_file():
    with read(FIXTURES / "npy" / "npy_c_float64.npy") as parsed, pytest.raises(ValueError, match="not a npy input"):
        fits_cards(parsed, 0)


def test_fits_cards_refuses_an_hdu_the_file_does_not_have():
    with read(FIXTURES / "fits" / "fits_mef_sci_err_dq.fits") as parsed, pytest.raises(ValueError, match="no HDU 9"):
        fits_cards(parsed, 9)


def test_an_hdu_of_no_data_has_no_arrays():
    with read(FIXTURES / "fits" / "fits_mef_sci_err_dq.fits") as parsed:
        description = parsed.description
        assert description is not None
        assert description["hdus"][0]["kind"] == "empty"
        assert fits_arrays(parsed, 0) == {}
        assert json.dumps(fits_cards(parsed, 0))


# ---- HDF5

HDF5 = sorted(p for p in (FIXTURES / "hdf5").iterdir() if p.suffix in (".h5", ".nwb") and "reject" not in p.name)


def test_the_hdf5_hierarchy_is_the_one_refkv_s_own_output_presents(copy_threshold):
    """Every accepted HDF5 fixture, read as a file or as a store of the files it names: the HDF5
    convention's hierarchy has refkv's own output's nodes, attributes without its declarations,
    source metadata, array documents and chunks, an edge chunk refkv copies a `Copy` of the bytes
    it holds; and its storage elements are the description's."""
    seen = dict.fromkeys(("store", "copied", "element"), 0)
    for path in HDF5:
        with read(str(path)) as parsed:
            hierarchy = hdf5_hierarchy(parsed)
            pr = project(parsed)
            assert sorted(hierarchy.nodes()) == sorted(pr.nodes()), path.name
            seen["store"] += not parsed.plain_file
            for node in hierarchy.nodes():
                at = f"{path.name}: /{node}"
                assert hierarchy.node_type(node) == pr.node_type(node), at
                assert hierarchy.attributes(node) == pr.attributes(node), at
                assert hierarchy.source_metadata(node) == pr.source_metadata(node), at
                if hierarchy.node_type(node) != "array":
                    continue
                doc, chunks = hierarchy.array_entries(node)
                theirs, their_chunks = pr.array_entries(node)
                assert doc == {**theirs, "attributes": pr.attributes(node)}, at
                assert chunks.keys() == their_chunks.keys(), at
                for key, value in chunks.items():
                    seen["copied"] += isinstance(their_chunks[key], bytes)
                    assert filled(value) == their_chunks[key], f"{at} {key}"
                element = hierarchy.element(node)
                if element is not None:
                    seen["element"] += 1
                    # the element is in its dataset's object, under its file's object in a store
                    assert re.match(r"(.+/)?objects/[0-9]+/(chunks|data)$", parsed.ir.path(element)), at
    assert seen["store"], seen
    assert seen["element"], seen
    assert seen["copied"] or copy_threshold is None, seen


def test_hdf5_layouts_ask_for_no_output_of_refkv_s(monkeypatch):
    """`refkv.virtualizarr`, `xarray-v2` and `hdf5-rechunk` present an HDF5 file, and a file read as
    a store, from the HDF5 convention's hierarchy, with refkv's own output made to raise: they are
    its peers (the plan of d-v-b/refkv#278). That what they write is unchanged, their own tests show."""
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("refkv's own output was asked for")

    layouts = [(VirtualiZarr(), {"nonfinite": "token"}), (XarrayV2(), {}), (Hdf5Rechunk(), {"default": [7]})]
    for name in ("hdf5_strings.h5", "hdf5_layouts.h5", "nwb_ecephys.nwb"):
        for layout, given in layouts:
            with read(str(FIXTURES / "hdf5" / name)) as parsed:
                monkeypatch.setattr(Parsed, "_output_async", refuse)
                monkeypatch.setattr(Parsed, "_output", refuse)
                try:
                    out = layout.write([parsed], layout.options(given))
                except Rejected as e:  # a dataset xarray-v2 has no form of: the layout's own refusal
                    assert isinstance(layout, XarrayV2), e  # noqa: PT017
                    continue
                assert any(k in out for k in ("zarr.json", ".zmetadata")), name
                seal(out, [parsed])
                monkeypatch.undo()


def test_hdf5_hierarchy_refuses_an_input_that_is_not_hdf5():
    with read(str(FITS[0])) as parsed, pytest.raises(ValueError, match="an HDF5 routine reads an HDF5 input"):
        hdf5_hierarchy(parsed)


def test_hdf5_hierarchy_rejects_what_the_convention_rejects():
    """A string the convention presents names no heap collection: the parse describes the file,
    and the hierarchy rejects it, with `zarrify`'s message."""
    with read(str(FIXTURES / "hdf5" / "hdf5_reject_vlen_collection.h5")) as parsed, \
            pytest.raises(Rejected, match="/ascii: element 0 names no global heap collection"):
        hdf5_hierarchy(parsed)


def test_hdf5_hierarchy_refuses_a_path_that_is_no_array():
    with read(str(FIXTURES / "hdf5" / "hdf5_strings.h5")) as parsed:
        hierarchy = hdf5_hierarchy(parsed)
        with pytest.raises(KeyError):
            hierarchy.element("")
        with pytest.raises(KeyError):
            hierarchy.array_entries("")


def test_hdf5_hierarchy_files_each_chunk_under_its_array_whatever_its_nodes_are_named(tmp_path):
    """A group and arrays named `c`, the segment a chunk key starts with: each array's chunks are
    its own, as refkv's own output holds them."""
    path = tmp_path / "c.h5"
    with h5py.File(path, "w") as f:
        f["c/c"] = np.arange(6, dtype="<i4")
        f["c/x"] = np.arange(4, dtype="<i2")
        f["x"] = np.arange(3, dtype="<i8")
    with read(str(path)) as parsed:
        hierarchy, pr = hdf5_hierarchy(parsed), project(parsed)
        for node in ("c/c", "c/x", "x"):
            chunks = hierarchy.array_entries(node)[1]
            assert chunks.keys() == pr.array_entries(node)[1].keys() != set(), node


# ---- netCDF3

NETCDF3 = sorted(p for p in (FIXTURES / "netcdf3").iterdir() if p.suffix == ".nc" and "reject" not in p.name)


def netcdf3_edged(tmp_path: Path) -> Path:
    """A netCDF3 file with a fixed-size variable of more than a chunk's bytes whose rows do not
    divide evenly into chunks, so that its last chunk is an edge chunk, padded or copied, beside a
    record variable: no fixture has one."""
    path = tmp_path / "edged.nc"
    with netCDF4.Dataset(path, "w", format="NETCDF3_CLASSIC") as ds:
        ds.createDimension("y", 1031)
        ds.createDimension("x", 1100)
        ds.createDimension("t", None)
        ds.createVariable("img", "i1", ("y", "x"))[:] = np.arange(1031 * 1100).astype(np.int8).reshape(1031, 1100)
        ds.createVariable("t", "f8", ("t",), fill_value=np.nan)[:] = [1, 2, 3]
    return path


def test_the_netcdf3_hierarchy_is_the_one_refkv_s_own_output_presents(tmp_path, copy_threshold):
    """Every accepted netCDF3 fixture, and a file with an edge chunk: the netCDF3 convention's
    hierarchy has refkv's own output's nodes, attributes without its declarations, source metadata,
    array documents and chunks, an edge chunk refkv copies a `Copy` of the bytes it holds."""
    seen = dict.fromkeys(("arrays", "records", "copied"), 0)
    for path in [*NETCDF3, netcdf3_edged(tmp_path)]:
        with read(str(path)) as parsed:
            hierarchy, pr = netcdf3_hierarchy(parsed), project(parsed)
            assert sorted(hierarchy.nodes()) == sorted(pr.nodes()), path.name
            for node in hierarchy.nodes():
                at = f"{path.name}: /{node}"
                assert hierarchy.node_type(node) == pr.node_type(node), at
                assert hierarchy.attributes(node) == pr.attributes(node), at
                assert hierarchy.source_metadata(node) == pr.source_metadata(node), at
                if hierarchy.node_type(node) != "array":
                    continue
                doc, chunks = hierarchy.array_entries(node)
                theirs, their_chunks = pr.array_entries(node)
                assert doc == {**theirs, "attributes": pr.attributes(node)}, at
                assert chunks.keys() == their_chunks.keys(), at
                for key, value in chunks.items():
                    seen["copied"] += isinstance(their_chunks[key], bytes)
                    assert filled(value) == their_chunks[key], f"{at} {key}"
                seen["arrays"] += 1
                seen["records"] += hierarchy.source_metadata(node)["record"] and bool(chunks)
    assert seen["arrays"], seen
    assert seen["records"], seen
    assert seen["copied"] or copy_threshold is None, seen


def test_the_netcdf3_layout_asks_for_no_output_of_refkv_s(monkeypatch):
    """`refkv.virtualizarr` presents every accepted netCDF3 fixture from the netCDF3 convention's
    hierarchy, with refkv's own output made to raise: it is its peer (the plan of d-v-b/refkv#278).
    That what it writes is unchanged, its own tests show."""
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("refkv's own output was asked for")

    layout = VirtualiZarr()
    for path in NETCDF3:
        for nonfinite in ("token", "text"):
            with read(str(path)) as parsed:
                monkeypatch.setattr(Parsed, "_output_async", refuse)
                monkeypatch.setattr(Parsed, "_output", refuse)
                out = layout.write([parsed], layout.options({"nonfinite": nonfinite}))
                assert "zarr.json" in out, path.name
                seal(out, [parsed])
                monkeypatch.undo()


def test_netcdf3_hierarchy_refuses_an_input_that_is_not_netcdf3():
    with read(str(FITS[0])) as parsed, pytest.raises(ValueError, match="a netCDF3 routine reads a netCDF3 file"):
        netcdf3_hierarchy(parsed)


def test_netcdf3_hierarchy_refuses_a_path_that_is_no_array():
    with read(str(FIXTURES / "netcdf3" / "netcdf3_classic.nc")) as parsed:
        hierarchy = netcdf3_hierarchy(parsed)
        with pytest.raises(KeyError):
            hierarchy.array_entries("")
