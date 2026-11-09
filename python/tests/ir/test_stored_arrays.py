"""The IR's stored arrays (spec/stored-array.md): for every NumPy fixture, the descriptions against
NumPy's own reading of each array's header (a `.npy` file's, and each `.npz` member's, presented
or not), the descriptions' JSON document, valid against spec/stored-array.json, and the chunk map
read through the Arrow PyCapsule interface; the reader's reading of every form of the document,
and one test per form it refuses."""

import ast
import json
import struct
import zipfile
from pathlib import Path

import jsonschema
import numpy as np
import pytest
from ir_helpers import FIXTURES, ROOT

from refkv._narrow import mapping, sequence
from refkv.ir import Ir, parse
from refkv.ir.planner import open_transport
from refkv.ir.stored import (
    Codec,
    Each,
    ElementPlace,
    ExtentPlace,
    Given,
    Grid,
    Listed,
    Source,
    Stored,
    StoredArray,
    UnitParameter,
    Value,
    read,
)

SCHEMA = json.loads((ROOT / "spec" / "stored-array.json").read_text())
NPY = sorted(p for p in (FIXTURES / "npy").iterdir() if "reject" not in p.name)
# NumPy warns of a header Python 2 wrote (npy_python2_long.npy), which it reads all the same
PYTHON2 = "ignore:Reading `.npy` or `.npz` file required additional header parsing"

# (shape, fortran_order, item bits: None for an object or a size of 0, the data's offset)
type Header = tuple[tuple[int, ...], bool, int | None, int]
# a descr NumPy has no type for on this machine (a long double of 16 bytes where its long double has
# 8): its header is read as text, and its item bits are its size's, which NumPy cannot give here
UNTYPED = {"<f16": 128, "<c32": 256}


def numpy_header(f) -> Header | None:
    """NumPy's reading of the NPY header at `f`'s position, or None where NumPy refuses it. Version
    3 lays its header out as version 2 does, in UTF-8 rather than Latin-1 text, which leaves its
    shape, order and item size as they are."""
    start = f.tell()
    try:
        major, _ = np.lib.format.read_magic(f)
    except ValueError:
        return None
    reader = np.lib.format.read_array_header_1_0 if major == 1 else np.lib.format.read_array_header_2_0
    try:
        shape, fortran, dtype = reader(f, max_header_size=1 << 30)
    except ValueError:
        f.seek(start + 8)
        n = struct.unpack("<H" if major == 1 else "<I", f.read(2 if major == 1 else 4))[0]
        try:
            h = ast.literal_eval(f.read(n).decode("latin-1"))
        except (ValueError, SyntaxError):
            return None
        if not isinstance(h, dict) or h.get("descr") not in UNTYPED:
            return None
        return tuple(h["shape"]), bool(h["fortran_order"]), UNTYPED[h["descr"]], f.tell()
    bits = None if dtype.hasobject or dtype.itemsize == 0 else dtype.itemsize * 8
    return tuple(shape), bool(fortran), bits, f.tell()


def described(path: Path) -> tuple[Ir, Stored, dict]:
    """The IR of the NumPy file at `path`, its stored arrays (their document checked against the
    schema), and its facts."""
    core, facts, _ = parse("npy", open_transport(str(path)))
    jsonschema.validate(json.loads(core.stored_arrays()), SCHEMA)
    ir = Ir(core)
    return ir, ir.stored_arrays(), facts


def against_numpy(path: Path, stored: Stored, facts: dict) -> list[tuple[StoredArray, Header]]:
    """Each description with NumPy's reading of its header: a `.npy` file's one, and, of a `.npz`
    archive, each stored member's whose header the parser read (a member the profile does not
    present included); a deflated member has none."""
    by_path = {a.path: a for a in stored.arrays}
    if path.suffix == ".npy":
        with path.open("rb") as f:
            h = numpy_header(f)
        assert h is not None
        assert set(by_path) == {"header"}
        return [(by_path["header"], h)]
    members = [mapping(m) for m in sequence(mapping(facts["own"])["members"])]
    pairs = []
    with zipfile.ZipFile(path) as z, path.open("rb") as f:
        for i, info in enumerate(z.infolist()):
            mine = [a for p, a in by_path.items() if (p or "").startswith(f"entries/{i}/")]
            if info.compress_type != zipfile.ZIP_STORED or "header" not in members[i]:
                assert not mine, info.filename
                continue
            f.seek(info.header_offset)
            n, e = struct.unpack("<HH", f.read(30)[26:30])
            f.seek(info.header_offset + 30 + n + e)
            h = numpy_header(f)
            assert h is not None, info.filename
            assert [a.path for a in mine] == [f"entries/{i}/data/header"], info.filename
            pairs.append((mine[0], h))
    assert len(pairs) == len(stored.arrays)
    return pairs


@pytest.mark.filterwarnings(PYTHON2)
@pytest.mark.parametrize("path", NPY, ids=[p.name for p in NPY])
def test_npy_arrays_are_described_as_numpy_reads_them(path):
    """Each described array has NumPy's shape (reversed, with `axes`, for a Fortran-order array of
    two or more axes), its dtype's item size (none for an object array), and its header, the one
    declaration, ends where NumPy's data starts."""
    ir, stored, facts = described(path)
    for d, (shape, fortran, bits, offset) in against_numpy(path, stored, facts):
        reverse = fortran and len(shape) >= 2
        assert d.shape == (shape[::-1] if reverse else shape), d.path
        assert d.axes == (tuple(range(len(shape)))[::-1] if reverse else None), d.path
        assert d.item_bits == bits, d.path
        assert d.declarations == (ElementPlace(d.element, None, None),), d.path
        start, length = ir.extent(d.element)
        assert start + length == offset, d.path
        assert d.fill == "undefined"


@pytest.mark.filterwarnings(PYTHON2)
@pytest.mark.parametrize("path", NPY, ids=[p.name for p in NPY])
def test_npy_chunk_map_crosses_the_capsule_interface(path):
    """The chunk map, as a record batch and as a stream, of the schema spec/stored-array.md §4.1
    <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#41-the-arrow-schema> gives,
    holds one row for each presented array of data: its unit, an element starting where NumPy's
    data does, as many bytes long as NumPy's array."""
    ir, stored, facts = described(path)
    pa = pytest.importorskip("pyarrow")
    cm = ir.chunk_map()
    batch = pa.record_batch(cm)
    assert batch.equals(pa.RecordBatchReader.from_stream(cm).read_next_batch())
    assert len(cm) == batch.num_rows
    assert batch.schema.metadata == {b"refkv.chunk_map": b"0"}
    assert batch.schema.names == ["array", "coords", "element", "member", "range", "ranges", "chain", "skips"]
    assert batch.schema.field("range").type == pa.struct([
        pa.field("offset", pa.uint64(), nullable=False), pa.field("length", pa.uint64(), nullable=False),
    ])
    assert batch.schema.field("coords").type == pa.large_list(pa.field("item", pa.uint64(), nullable=False))
    assert batch.schema.field("ranges").type == pa.large_list(pa.field("item", pa.struct([
        pa.field("object", pa.dictionary(pa.int32(), pa.utf8())),
        pa.field("offset", pa.uint64(), nullable=False),
        pa.field("length", pa.uint64(), nullable=False),
    ]), nullable=False))
    rows = {r["array"]: r for r in batch.to_pylist()}
    assert len(rows) == batch.num_rows
    units = 0
    for d, (shape, _, bits, offset) in against_numpy(path, stored, facts):
        k = stored.arrays.index(d)
        size = (bits or 0) // 8 * int(np.prod(shape, dtype=object))
        if d.grid is None or size == 0:
            assert k not in rows, d.path
            continue
        r = rows[k]
        assert ir.extent(r["element"]) == (offset, size), d.path
        assert r["coords"] == [0] * len(shape), d.path
        assert (r["member"], r["range"], r["ranges"], r["chain"], r["skips"]) == (None,) * 5, d.path
        units += 1
    assert units == batch.num_rows


def document(**changes: object) -> dict:
    """A document of every form the reader reads, its first description's members replaced by
    `changes`."""
    array = {
        "element": 3, "path": "a/b", "index": 0,
        "declarations": [{"element": 1, "member": 2, "range": [0, 4]}, {"object": None, "offset": 8, "length": 2}],
        "shape": [25, 12], "axes": [1, 0], "item_bits": 12, "pad_from": 1,
        "grid": {"axes": [{"list": [5, [10, 2]]}, {"each": 8}], "edge": "full"},
        "chains": [0], "fill": {"given": {"value": "AAA="}},
        "unit_parameters": [{"chain": 0, "codec": 0, "name": "scale"}],
    }
    plain = {
        "element": 4, "path": None, "index": None, "declarations": [{"object": "x.bin", "offset": 0, "length": 1}],
        "shape": None, "axes": None, "item_bits": None, "pad_from": None, "grid": None, "chains": [],
        "fill": "format_default", "unit_parameters": [],
    }
    source = {"source": {"element": 9, "member": None, "range": None}}
    chain = [{"name": "source", "parameters": {"id": {"value": 7}, "parameters": source}}]
    return {"chains": [chain], "arrays": [array | changes, plain], "undescribed": False}


def refused(error: type[Exception], match: str, doc: dict) -> None:
    assert not jsonschema.Draft202012Validator(SCHEMA).is_valid(doc)
    with pytest.raises(error, match=match):
        read(json.dumps(doc))


def test_reads_every_form_of_the_document():
    assert read(json.dumps(document())) == Stored(
        chains=((Codec("source", {"id": Value(7), "parameters": Source(ElementPlace(9, None, None))}),),),
        arrays=(
            StoredArray(
                element=3, path="a/b", index=0,
                declarations=(ElementPlace(1, 2, (0, 4)), ExtentPlace(None, 8, 2)),
                shape=(25, 12), axes=(1, 0), item_bits=12, pad_from=1,
                grid=Grid((Listed((5, (10, 2))), Each(8)), "full"), chains=(0,), fill=Given(Value("AAA=")),
                unit_parameters=(UnitParameter(0, 0, "scale"),),
            ),
            StoredArray(
                element=4, path=None, index=None, declarations=(ExtentPlace("x.bin", 0, 1),), shape=None, axes=None,
                item_bits=None, pad_from=None, grid=None, chains=(), fill="format_default", unit_parameters=(),
            ),
        ),
        undescribed=False,
    )
    jsonschema.validate(document(), SCHEMA)
    marked = {"chains": [], "arrays": [], "undescribed": True}
    assert read(json.dumps(marked)) == Stored((), (), True)
    listed = {"chains": [], "arrays": [], "undescribed": ["a/f.bin", "g.bin"]}
    assert read(json.dumps(listed)) == Stored((), (), ("a/f.bin", "g.bin"))
    for doc in (marked, listed):
        jsonschema.validate(doc, SCHEMA)


def test_refuses_a_value_of_another_type():
    refused(TypeError, r"arrays\[0\]\.shape\[0\]: expected an integer", document(shape=["25", 12]))


def test_refuses_a_missing_member():
    doc = document()
    del doc["arrays"][0]["fill"]
    refused(ValueError, r"arrays\[0\]: missing fill", doc)


def test_refuses_an_unknown_member_of_a_description():
    refused(ValueError, r"arrays\[0\]: unknown extra", document(extra=1))


def test_refuses_an_unknown_member_of_the_document():
    refused(ValueError, "stored arrays: unknown version", {**document(), "version": 0})


def test_refuses_an_undescribed_mark_of_another_type():
    refused(TypeError, "undescribed: expected", {**document(), "undescribed": 1})


def test_refuses_an_undescribed_key_that_is_not_a_string():
    refused(TypeError, r"undescribed\[0\]: expected", {**document(), "undescribed": [1]})


def test_refuses_a_negative_integer():
    refused(ValueError, r"arrays\[0\]\.shape\[0\]: -1 is below 0", document(shape=[-1, 12]))


def test_refuses_item_bits_of_0():
    refused(ValueError, r"arrays\[0\]\.item_bits: 0 is below 1", document(item_bits=0))


def test_refuses_units_0_long():
    grid = {"axes": [{"list": [5, [10, 2]]}, {"each": 0}], "edge": "full"}
    refused(ValueError, r"axes\[1\]\.each: 0 is below 1", document(grid=grid))


def test_refuses_a_listed_unit_0_long():
    grid = {"axes": [{"list": [0, [10, 2]]}, {"each": 8}], "edge": "full"}
    refused(ValueError, r"list\[0\]: 0 is below 1", document(grid=grid))


def test_refuses_a_run_of_one_unit():
    grid = {"axes": [{"list": [5, [10, 1]]}, {"each": 8}], "edge": "full"}
    refused(ValueError, r"list\[1\]\[1\]: 1 is below 2", document(grid=grid))


def test_refuses_a_place_of_neither_element_nor_object():
    refused(ValueError, "names neither an element nor an object", document(declarations=[{"offset": 0, "length": 1}]))


def test_refuses_a_place_with_an_unknown_member():
    place = {"element": 1, "member": None, "range": None, "extra": 1}
    refused(ValueError, r"declarations\[0\]: unknown extra", document(declarations=[place]))


def test_refuses_a_place_of_both_element_and_object():
    place = {"element": 1, "member": None, "range": None, "object": None}
    refused(ValueError, r"declarations\[0\]: unknown object", document(declarations=[place]))


def test_refuses_a_parameter_of_neither_value_nor_source():
    refused(ValueError, "exactly one of value and source", document(fill={"given": {"bytes": "AAA="}}))


def test_refuses_a_parameter_of_both_value_and_source():
    given = {"value": "AAA=", "source": {"element": 1, "member": None, "range": None}}
    refused(ValueError, "exactly one of value and source", document(fill={"given": given}))


@pytest.mark.parametrize("value", ["A?A=", "==", "AAAA==", "AAA"])
def test_refuses_a_fill_value_that_is_not_base64(value):
    refused(ValueError, r"fill\.given\.value: not base64", document(fill={"given": {"value": value}}))


def test_refuses_an_axis_of_neither_each_nor_list():
    grid = {"axes": [{"every": 5}, {"each": 8}], "edge": "full"}
    refused(ValueError, "exactly one of each and list", document(grid=grid))


def test_refuses_an_edge_of_neither_full_nor_short():
    grid = {"axes": [{"each": 5}, {"each": 8}], "edge": "padded"}
    refused(ValueError, "neither 'full' nor 'short'", document(grid=grid))


def test_refuses_a_fill_of_no_form():
    refused(ValueError, "'zero' is not a fill", document(fill="zero"))


def test_refuses_a_range_that_is_not_a_pair():
    place = {"element": 1, "member": None, "range": [0, 4, 8]}
    refused(ValueError, "expected two integers, got 3", document(declarations=[place]))
