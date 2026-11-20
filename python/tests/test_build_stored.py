"""`refkv.build.stored_array`: an array the IR describes as a Zarr array of the units the source
stores, built with no output of refkv's (the design of d-v-b/refkv#278: a layout is a projection of
the IR). NumPy is the first format whose description a caller reads: its files are presented here
from their stored arrays and headers, and read back against `numpy.load`."""

import zlib
from pathlib import Path
from typing import Any, cast

import numpy as np
import pytest
import zarr

import refkv.zarr
from refkv.build import Parsed, Written, read, seal, stored_array, zarr_codecs
from refkv.zarrify.coding import NoCodec, npy_array_type
from refkv.zarrify.layouts import dump

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
NPY = sorted(p for p in (FIXTURES / "npy").iterdir() if p.suffix in (".npy", ".npz") and "reject" not in p.name)


def as_stored(parsed: Parsed) -> Written:
    """A NumPy file's arrays as the source stores them, each one chunk, from the IR's descriptions
    and the headers' declarations: what a layout that asks for no output of refkv's can write."""
    description = parsed.description
    assert description is not None
    position = {a.element: k for k, a in enumerate(parsed.ir.stored_arrays().arrays)}
    out = Written({"zarr.json": dump({"zarr_format": 3, "node_type": "group", "attributes": {}})})
    for entry in description["arrays"]:
        if "deflate" in entry:  # a deflated member: its data lies in what its stream inflates to
            continue
        declared = npy_array_type(entry["header"])
        array = stored_array(parsed, position[entry["stored"]], declared["data_type"], endian=declared["endian"],
                             inner_shape=[declared["trailing"]] if declared["trailing"] else (),
                             fill_value=declared["fill_value"])
        out |= array.entries(to=entry["path"])
    return out


def expected(path: Path, name: str) -> np.ndarray:
    """What NumPy reads of the file: its array, at `array`, or the member `name`'s."""
    loaded = np.load(path, allow_pickle=False)
    return loaded if isinstance(loaded, np.ndarray) else loaded[name]


def same_values(got: np.ndarray, want: np.ndarray) -> bool:
    """Whether an array read back holds NumPy's values: as they are, or, for fixed-length bytes,
    which the NumPy profile presents as uint8 with a trailing axis, byte for byte."""
    if got.ndim == want.ndim + 1:  # a trailing axis of an element's bytes
        return got.tobytes() == np.ascontiguousarray(want).tobytes() and got.shape == (*want.shape, want.dtype.itemsize)
    if got.shape != want.shape:
        return False
    if want.dtype.fields is not None:  # a structure: field for field
        return all(same_values(got[name], want[name]) for name in want.dtype.names or ())
    return bool(np.array_equal(got, want.astype(got.dtype), equal_nan=want.dtype.kind in "fc"))


def test_numpy_files_read_from_their_stored_arrays_as_numpy_reads_them(monkeypatch):
    """Every accepted NumPy fixture's stored arrays, each written as a Zarr array of its one unit
    with `stored_array` and the header's declared type, read back the values `numpy.load` gives:
    C and Fortran order, either byte order, fixed-length bytes, scalars, arrays of no elements,
    and the stored members of archives. refkv's own output is never made."""
    async def no_output(_self):
        raise AssertionError("refkv's own output was built")

    monkeypatch.setattr(Parsed, "_output_async", no_output)
    seen: dict[str, int] = {}
    for path in NPY:
        with read(path) as parsed:
            assert parsed.profile == "npy"
            description: Any = parsed.description
            presented = [e for e in description["arrays"] if "deflate" not in e]
            store = refkv.zarr.store(seal(as_stored(parsed), [parsed]))
        for entry in presented:
            declared = npy_array_type(entry["header"])
            array = zarr.open_array(store, path=entry["path"], mode="r", zarr_format=3)
            assert same_values(np.asarray(array[...]), expected(path, entry["path"])), f"{path.name}: {entry['path']}"
            assert array.shape[:len(declared["shape"])] == tuple(declared["shape"]), path.name
            codecs = [type(c).__name__ for c in cast("Any", array.metadata).codecs]
            kinds = {"fortran": declared["fortran_order"] and len(declared["shape"]) > 1,
                     "big-endian": declared["endian"] == "big", "bytes": declared["trailing"] is not None,
                     "scalar": declared["shape"] == [], "empty": 0 in declared["shape"],
                     "member": path.suffix == ".npz", "transposed": "TransposeCodec" in codecs}
            for kind, holds in kinds.items():
                seen[kind] = seen.get(kind, 0) + bool(holds)
    assert all(count > 0 for count in seen.values()), seen
    assert seen["transposed"] == seen["fortran"]


def test_a_parsed_numpy_file_describes_what_its_parser_found():
    """`Parsed.description` is the NumPy parser's: the source metadata, and each presented array's
    path, header text and stored array's key; a copy, and None for a profile that has none yet."""
    with read(FIXTURES / "npy" / "npy_c_float64.npy") as parsed:
        description: Any = parsed.description
        [entry] = description["arrays"]
        assert set(description) == {"own", "arrays"}
        assert description["own"]["kind"] == "npy"
        assert (entry["path"], entry["stored"]) == ("array", parsed.ir.at("header"))
        assert npy_array_type(entry["header"])["data_type"] == "float64"
        description["arrays"].clear()
        again: Any = parsed.description
        assert len(again["arrays"]) == 1
    with read(FIXTURES / "tiff" / "strips_uint8.tif") as other:
        assert other.description is None


def test_the_empty_chain_has_no_codecs():
    assert zarr_codecs([]) == []


def test_npy_array_type_refuses_a_descr_the_profile_does_not_present():
    with pytest.raises(NoCodec):
        npy_array_type("{'descr': '|O', 'fortran_order': False, 'shape': (2,), }")


def test_npy_array_type_refuses_text_that_is_no_header():
    with pytest.raises(ValueError, match="not an NPY header"):
        npy_array_type("not a header")


def test_stored_array_refuses_a_position_the_ir_does_not_describe():
    with read(FIXTURES / "npy" / "npy_c_float64.npy") as parsed, pytest.raises(ValueError, match="no stored array 7"):
        stored_array(parsed, 7, "float64")


def test_stored_array_refuses_an_array_whose_storage_the_ir_does_not_model():
    """An object array's member is described with no grid: there are no units to write."""
    for path in NPY:
        with read(path) as parsed:
            ungridded = [k for k, a in enumerate(parsed.ir.stored_arrays().arrays) if a.grid is None]
            if ungridded:
                with pytest.raises(ValueError, match="does not model this array's storage"):
                    stored_array(parsed, ungridded[0], "uint8")
                return
    pytest.fail("no fixture has an array with no grid")


def test_stored_array_refuses_elements_that_are_not_whole_bytes():
    with read(FIXTURES / "tiff" / "bilevel_tiled.tif") as parsed:
        bits = [k for k, a in enumerate(parsed.ir.stored_arrays().arrays) if a.item_bits == 1]
        with pytest.raises(ValueError, match="not whole bytes"):
            stored_array(parsed, bits[0], "bool")


def test_stored_array_refuses_a_unit_in_a_member_of_a_run():
    """An ND2 file's frames fold into a run: each unit names the run's first member's element and
    its own position among the members, which `stored_array` does not place yet."""
    for path in sorted((FIXTURES / "nd2").glob("*.nd2")):
        with read(path) as parsed:
            stored = parsed.ir.stored_arrays()
            for k, a in enumerate(stored.arrays):
                plain = a.grid is not None and a.item_bits and a.item_bits % 8 == 0 and len(a.chains) == 1
                if plain and not stored.chains[a.chains[0]] and any(u.member is not None for u in parsed.ir.units(k)):
                    with pytest.raises(ValueError, match=r"member .* of a run"):
                        stored_array(parsed, k, "uint16")
                    return
    pytest.fail("no ND2 fixture has an array of the empty chain whose units lie in a run's members")


def test_stored_array_refuses_a_chain_it_has_no_codecs_for():
    with read(FIXTURES / "hdf5" / "hdf5_filters.h5") as parsed:
        stored = parsed.ir.stored_arrays()
        encoded = [k for k, a in enumerate(stored.arrays)
                   if a.grid is not None and len(a.chains) == 1 and stored.chains[a.chains[0]]
                   and a.item_bits and a.item_bits % 8 == 0]
        with pytest.raises(ValueError, match="no Zarr codecs yet"):
            stored_array(parsed, encoded[0], "uint8")


def test_a_deflated_members_stream_is_the_bytes_its_elements_are_derived_from():
    """A deflated `.npz` member's header and data are elements of what its stream inflates to:
    the description keys its stored array, whose one unit is the `data` element there, and
    `Parsed.encoded` gives the stream, which inflates to the preamble, the header and the data."""
    path = FIXTURES / "npy" / "npz_compressed.npz"
    with read(path) as parsed:
        description: Any = parsed.description
        position = {a.element: k for k, a in enumerate(parsed.ir.stored_arrays().arrays)}
        seen = 0
        for entry in description["arrays"]:
            header = entry["stored"]
            stream = parsed.ir.space(header)
            assert stream != 0
            ref = parsed.encoded(stream)
            inflated = zlib.decompress(parsed.bytes((ref.source, ref.offset, ref.length)), wbits=-15)
            assert len(inflated) == entry["deflate"]["size"]
            assert zlib.crc32(inflated) == entry["deflate"]["crc"]
            start, length = parsed.ir.extent(header)
            assert inflated[start:start + length].decode("latin-1") == entry["header"]
            want = expected(path, entry["path"])
            units = parsed.ir.units(position[header])
            if not want.size:  # an array of no elements has no data, and no unit
                assert units == []
                assert start + length == len(inflated)
                continue
            [unit] = units
            assert unit.element is not None
            assert parsed.ir.space(unit.element) == stream
            start, length = parsed.ir.extent(unit.element)
            # the data ends the stream, in the order it is stored in
            assert (start + length, inflated[start:]) == (len(inflated), want.tobytes("A"))
            seen += 1
        assert seen >= 2


def test_stored_array_refuses_a_unit_in_a_derived_space():
    with read(FIXTURES / "npy" / "npz_compressed.npz") as parsed, \
            pytest.raises(ValueError, match="lies in what 'entries/0/data' decodes to"):
        stored_array(parsed, 0, "float64", endian="little")


def test_encoded_refuses_an_element_that_is_not_derived():
    with read(FIXTURES / "npy" / "npz_compressed.npz") as parsed, \
            pytest.raises(ValueError, match="'entries/0/data/header' is not an element derived from bytes"):
        parsed.encoded(parsed.ir.index("entries/0/data/header"))
