"""`refkv.build.stored_array`: an array the IR describes as a Zarr array of the units the source
stores, built with no output of refkv's (the design of d-v-b/refkv#278: a layout is a projection of
the IR). NumPy is the first format whose description a caller reads: its files are presented here
from their stored arrays and headers, and read back against `numpy.load`."""

import base64
import math
import zipfile
import zlib
from pathlib import Path
from typing import Any, cast

import h5py
import numpy as np
import pytest
import zarr
import zarr.storage

import refkv.zarr
from refkv.build import Array, Parsed, Written, read, seal, stored_array, zarr_codecs
from refkv.ir.stored import Codec, Each, ElementPlace, Source, StoredArray
from refkv.ir.stored import Value as ParamValue
from refkv.zarrify.coding import NoCodec, npy_array_type
from refkv.zarrify.layouts import dump

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
NPY = sorted(p for p in (FIXTURES / "npy").iterdir() if p.suffix in (".npy", ".npz") and "reject" not in p.name)
HDF5 = sorted(p for p in (FIXTURES / "hdf5").iterdir() if p.suffix in (".h5", ".nwb") and "reject" not in p.name)
# h5py's layout classes (H5D_COMPACT, H5D_CONTIGUOUS, H5D_CHUNKED, H5D_VIRTUAL) by the description's names
LAYOUTS = {"compact": 0, "contiguous": 1, "chunked": 2, "virtual": 3}
# h5py's link types (H5L_TYPE_HARD, H5L_TYPE_SOFT, H5L_TYPE_EXTERNAL; h5py.h5l, which has no stubs)
HARD, SOFT, EXTERNAL = 0, 1, 64


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
    """HDF5's Fletcher-32 checksum, which numcodecs does not frame as HDF5 does."""
    with read(FIXTURES / "hdf5" / "hdf5_filters.h5") as parsed:
        stored = parsed.ir.stored_arrays()
        encoded = [k for k, a in enumerate(stored.arrays)
                   if a.grid is not None and [c.name for c in stored.chains[a.chains[0]]] == ["h5_fletcher32"]]
        with pytest.raises(ValueError, match="no Zarr codec yet for the IR's codec h5_fletcher32"):
            stored_array(parsed, encoded[0], "uint32")


def test_zarr_codecs_refuses_a_source_codec_that_takes_an_array():
    """A Zarr v2 filter (numcodecs' Delta) takes the array's elements, not bytes: it cannot follow
    `bytes`."""
    delta = Codec("source", {"format": ParamValue("zarr2"), "id": ParamValue("delta"),
                             "parameters": ParamValue({"dtype": "<i4"})})
    with pytest.raises(ValueError, match="no Zarr codec after bytes for the source codec 'delta' of 'zarr2'"):
        zarr_codecs([delta])


def test_zarr_codecs_refuses_a_shuffle_whose_element_size_lies_in_the_source():
    shuffle = Codec("shuffle", {"element_size": Source(ElementPlace(3, None, None))})
    with pytest.raises(ValueError, match="no Zarr codec yet for the IR's codec shuffle"):
        zarr_codecs([shuffle])


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


def test_a_parsed_fits_file_describes_its_hdus_as_their_headers_declare_them():
    """`Parsed.description` of a FITS file holds each HDU as its header gives it, with none of the
    output's decisions: its index, kind, `BITPIX` and `NAXISn` as astropy reads them, and the IR
    element of its header, which keys its stored arrays."""
    astropy_fits = pytest.importorskip("astropy.io.fits")
    path = FIXTURES / "fits" / "fits_mef_sci_err_dq.fits"
    with read(path) as parsed, astropy_fits.open(path) as hdul:
        description: Any = parsed.description
        assert set(description) == {"hdus"}
        assert len(description["hdus"]) == len(hdul)
        keys = {a.element for a in parsed.ir.stored_arrays().arrays}
        for i, (h, theirs) in enumerate(zip(description["hdus"], hdul, strict=True)):
            assert "data_type" not in h
            assert h["index"] == i
            assert h["bitpix"] == theirs.header["BITPIX"]
            assert h["naxis"] == [theirs.header[f"NAXIS{n}"] for n in range(1, theirs.header["NAXIS"] + 1)]
            header = h["elements"]["header"]
            assert parsed.ir.path(header) == f"hdus/{i}/header"
            assert (header in keys) == (h["kind"] != "empty")


def h5py_name(segment: Any) -> str:
    """A link name of the description (a text value) as h5py gives it: a name that is not UTF-8,
    `{"latin1": ...}`, decoded with its bytes kept as surrogates."""
    if isinstance(segment, str):
        return segment
    return segment["latin1"].encode("latin-1").decode("utf-8", "surrogateescape")


def h5py_objects(f: h5py.File) -> dict[str, Any]:
    """Each object of an HDF5 file h5py reaches by hard links, once, at the path its visit gives
    (in ascending order of link names, as the canonical path), by that path, a name that is not
    UTF-8 with its bytes kept as surrogates; the root at the empty path."""
    out: dict[str, Any] = {"": f}

    def visit(name: str | bytes, obj: Any) -> None:
        out.setdefault(name.decode("utf-8", "surrogateescape") if isinstance(name, bytes) else name, obj)

    f.visititems(visit)
    return out


def test_an_hdf5_files_description_is_what_h5py_reads():
    """Every accepted HDF5 fixture read as a file: the description's objects are those h5py
    reaches, each of its kind; a group's links h5py's; an attribute's names and shapes h5py's; a
    dataset's dataspace, layout, chunk shape, filters, fill value and dimension scales h5py's, and a
    dataset of strings or references its elements, read from the spans and paths given."""
    seen = dict.fromkeys(("described", "soft", "filters", "fill", "strings", "references", "scales"), 0)
    for path in HDF5:
        with read(path) as parsed:
            description: Any = parsed.description
        if description is None:
            # a file that names siblings (its external links and data) is read as a store, whose
            # description `read` does not give
            continue
        seen["described"] += 1
        data = path.read_bytes()
        with h5py.File(path, "r") as f:
            objects = h5py_objects(f)
            described = {"/".join(map(h5py_name, o["segments"])): o for o in description["objects"]}
            assert described.keys() == objects.keys(), path.name
            for key, o in described.items():
                at = f"{path.name}: /{key}"
                obj = objects[key]
                kind = ("group" if isinstance(obj, h5py.Group) else
                        "dataset" if isinstance(obj, h5py.Dataset) else "named_datatype")
                assert o["kind"] == kind, at
                attrs = {a["name"]: a for a in o["attributes"]}
                assert sorted(attrs, key=str.encode) == [a["name"] for a in o["attributes"]], at
                assert attrs.keys() == set(obj.attrs.keys()), at
                for name, a in attrs.items():
                    if "dims" in a:
                        shape = obj.attrs.get_id(name).shape
                        assert a["dims"] == (None if shape is None else list(shape)), f"{at} {name}"
                if kind == "group":
                    for link in o["links"]:
                        # h5py's low-level links, which take a name that is not UTF-8
                        name = h5py_name(link["name"]).encode("utf-8", "surrogateescape")
                        kind = obj.id.links.get_info(name).type
                        if "soft" in link:
                            assert kind == SOFT, at
                            assert obj.id.links.get_val(name).decode() == link["soft"], at
                            seen["soft"] += 1
                        elif "hard" in link:
                            assert kind == HARD, at
                        else:
                            assert kind == link["type"] == EXTERNAL, at
                            file, target = (v.decode() for v in obj.id.links.get_val(name))
                            assert (file, target) == (link["file"], link["path"]), at
                    continue
                if kind != "dataset":
                    continue
                plist = obj.id.get_create_plist()
                assert o["dims"] == (None if obj.shape is None else list(obj.shape)), at
                if obj.shape is None:
                    assert o["layout"] == "null", at
                    continue
                assert LAYOUTS[o["layout"]] == plist.get_layout(), at
                assert o.get("chunk") == (None if obj.chunks is None else list(obj.chunks)), at
                filters = [plist.get_filter(k)[:3:2] for k in range(plist.get_nfilters())]
                assert [(f["id"], tuple(f["values"])) for f in o["filters"]] == filters, at
                seen["filters"] += bool(filters)
                if "fill" in o and obj.dtype.kind in "iufb" and not obj.dtype.fields:
                    want = np.zeros((), obj.dtype)
                    plist.get_fill_value(want)
                    assert bytes.fromhex(o["fill"]) == want.tobytes(), at
                    seen["fill"] += 1
                if obj.attrs.get("CLASS") == b"DIMENSION_SCALE":
                    assert o["dimension_scale"]["CLASS"] == "DIMENSION_SCALE", at
                    seen["scales"] += 1
                scales = o.get("dimension_list", {}).get("scales")
                if scales is not None:
                    assert [len(s) for s in scales] == [len(d) for d in obj.dims], at
                values = o.get("values", {})
                if "spans" in values:
                    spans = values["spans"]
                    texts = [data[a:a + n].decode() for a, n in zip(spans[::2], spans[1::2], strict=True)]
                    elements = np.asarray(obj[()], dtype=object).reshape(-1)
                    assert texts == [t.decode() if isinstance(t, bytes) else t for t in elements], at
                    assert values["count"] == obj.size, at
                    seen["strings"] += 1
                if "paths" in values:
                    want = [f[r].name if r else "" for r in np.asarray(obj[()], dtype=object).reshape(-1)]
                    assert values["paths"] == want, at
                    seen["references"] += 1
    assert all(seen.values()), seen
    assert seen["described"] >= 18, seen


def test_a_parsed_hdf5_file_describes_every_object_and_no_decision():
    """`Parsed.description` of an HDF5 file holds its objects and none of the hierarchy's
    decisions: an object the hierarchy does not present is described as any other, and no array
    has a Zarr data type."""
    with read(FIXTURES / "hdf5" / "hdf5_netcdf4.h5") as parsed:
        description: Any = parsed.description
        assert set(description) == {"superblock", "user_block", "objects"}
        hidden = [o for o in description["objects"]
                  if o.get("dimension_scale", {}).get("NAME", "").startswith("This is a netCDF dimension")]
        assert hidden
        assert all(o["kind"] == "dataset" and "storage" in o for o in hidden)
        assert not any("data_type" in o or "codecs" in o for o in description["objects"])


ZARR = sorted(p for d in ("zarr2", "zarr3") for p in (FIXTURES / d).iterdir() if "reject" not in p.name)


def zarr_node(path: Path, array: StoredArray) -> str:
    """The node a Zarr store's stored array is keyed by: its document's directory, by the path of
    the document's object, or, in the zip form, of the document's entry, by the entry's name."""
    key = cast("str", array.path)
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as z:
            key = z.infolist()[int(key.removeprefix("entries/"))].filename
    return key.rpartition("/")[0]


def grid_units(a: StoredArray) -> int:
    """The units of a description's grid: along an `Each` axis as many as cover it, along a listed
    axis as many as it lists."""
    grid, shape = cast("Any", a.grid), cast("tuple[int, ...]", a.shape)
    return math.prod(-(-n // axis.length) if isinstance(axis, Each)
                     else sum(r[1] if isinstance(r, tuple) else 1 for r in axis.runs)
                     for axis, n in zip(grid.axes, shape, strict=True))


def has_empty_object(path: Path, node: str) -> bool:
    """Whether a store, listed or zipped, has an object of no bytes below the node."""
    prefix = f"{node}/" if node else ""
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as z:
            return any(i.filename.startswith(prefix) and i.file_size == 0 and not i.is_dir() for i in z.infolist())
    return any(f.is_file() and f.stat().st_size == 0 for f in (path / node).rglob("*"))


def numpy_type(t: Any) -> Any:
    """A Zarr v2 `dtype` as NumPy takes it: a type string, or a structure's fields, each a list."""
    return t if isinstance(t, str) else [(f[0], numpy_type(f[1]), *f[2:]) for f in t]


def stored_type(document: dict[str, Any]) -> np.dtype | None:
    """The NumPy type of an array's elements as the store holds them, of its document: a Zarr v2
    `dtype`; a Zarr v3 core data type in the byte order of its `bytes` codec. None for a type
    of no fixed size."""
    if "dtype" in document:
        return None if document["dtype"] == "|O" else np.dtype(numpy_type(document["dtype"]))
    if not isinstance(document["data_type"], str) or document["data_type"] == "string":
        return None
    serializer = next(c for c in document["codecs"] if c["name"] != "transpose")
    if serializer["name"] == "sharding_indexed":
        serializer = next(c for c in serializer["configuration"]["codecs"] if c["name"] != "transpose")
    endian = serializer.get("configuration", {}).get("endian")
    return np.dtype(document["data_type"]).newbyteorder(">" if endian == "big" else "<" if endian == "little" else "=")


def test_zarr_stores_read_from_their_stored_arrays_as_zarr_python_reads_them(monkeypatch):
    """Every accepted Zarr v2 and v3 fixture, listed and zipped: each stored array of an element
    of whole bytes whose codecs Zarr can follow with `bytes`, written with `stored_array` as raw
    bytes of the element's size (the description names no data type) and with the description's
    fill value, reads back, byte for byte, what zarr-python reads of the store's own array in the
    type the store holds, chunks it leaves out included. The description's fill value is the bytes
    of zarr-python's. refkv's own output is never made."""
    async def no_output(_self):
        raise AssertionError("refkv's own output was built")

    monkeypatch.setattr(Parsed, "_output_async", no_output)
    seen = dict.fromkeys(("zarr2", "zarr3", "zip", "compressed", "fortran", "transposed", "missing", "fill",
                          "not followed", "sharded", "no item_bits", "no grid", "zarr-python refuses",
                          "empty chunk"), 0)
    for path in ZARR:
        fmt = 2 if path.parent.name == "zarr2" else 3
        with read(path) as parsed:
            stored = parsed.ir.stored_arrays()
            built: dict[str, tuple[Array, bytes | None]] = {}
            for k, a in enumerate(stored.arrays):
                chain = stored.chains[a.chains[0]] if a.chains else ()
                seen["sharded"] += any(c.parameters.get("id") == ParamValue("sharding_indexed") for c in chain)
                seen["no grid"] += a.grid is None
                seen["no item_bits"] += a.grid is not None and a.item_bits is None
                if a.grid is None or a.item_bits is None:
                    continue
                try:
                    zarr_codecs(chain)
                except ValueError:  # a filter or a serializer: Zarr does not follow `bytes` with it
                    seen["not followed"] += 1
                    continue
                fill = a.grid.sparse.fill if a.grid.sparse else None
                given = base64.b64decode(cast("Any", fill).param.value) if fill is not None else None
                n = a.item_bits // 8
                raw = {"name": "raw_bytes", "configuration": {"length_bytes": n}}
                array = stored_array(parsed, k, raw, fill_value=base64.b64encode(given or bytes(n)).decode())
                built[zarr_node(path, a)] = (array, given)
                seen["missing"] += len(parsed.ir.units(k)) < grid_units(a)
                seen["compressed"] += bool(chain)
                seen["fortran" if fmt == 2 else "transposed"] += a.axes is not None
            out = Written({"zarr.json": dump({"zarr_format": 3, "node_type": "group", "attributes": {}})})
            for node, (array, _) in built.items():
                out |= array.entries(to=node)
            ours = refkv.zarr.store(seal(out, [parsed]))
        theirs: Any = (zarr.storage.ZipStore(path, mode="r") if path.suffix == ".zip"
                       else zarr.storage.LocalStore(path, read_only=True))
        for node, (_, given) in built.items():
            at = f"{path.name}: /{node}"
            if has_empty_object(path, node):
                # which zarr-python decodes as a chunk of no bytes, and the description holds no
                # unit of
                seen["empty chunk"] += 1
                continue
            try:
                original = zarr.open_array(theirs, path=node, mode="r", zarr_format=fmt)
            except (KeyError, TypeError, ValueError):
                # a document zarr-python does not read: a codec's member numcodecs does not take
                # (lz4's `writer`), a data type it has not (a nested structure), no fill value
                seen["zarr-python refuses"] += 1
                continue
            values = np.asarray(original[...])
            held = stored_type(original.metadata.to_dict())
            assert held is not None, at
            got = zarr.open_array(ours, path=node, mode="r", zarr_format=3)
            assert got.shape == original.shape, at
            want = np.ascontiguousarray(values.astype(held)).tobytes()
            assert np.ascontiguousarray(got[...]).tobytes() == want, at
            if given is not None:
                assert np.asarray(original.metadata.fill_value).astype(held).tobytes() == given, at
            seen["fill"] += given is not None
            seen["zip"] += path.suffix == ".zip"
            seen[f"zarr{fmt}"] += 1
    assert all(seen.values()), seen
