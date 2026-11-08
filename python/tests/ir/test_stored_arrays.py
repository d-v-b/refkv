"""The IR's stored arrays (spec/stored-array.md): for every NumPy fixture, the descriptions' JSON
document, valid against spec/stored-array.json and read by refkv.ir.stored, and the chunk map read
through the Arrow PyCapsule interface; the reader's reading of every form of the document, and one
test per form it refuses."""

import json

import jsonschema
import pytest
from ir_helpers import FIXTURES, ROOT

from refkv._narrow import integer, mapping, sequence
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


@pytest.mark.filterwarnings("ignore:Reading `.npy` or `.npz` file required additional header parsing")
@pytest.mark.parametrize("path", NPY, ids=[p.name for p in NPY])
def test_npy_arrays_cross_the_bindings(path):
    """The document is the schema's and reads; the chunk map, as a record batch and as a stream,
    holds a row for each presented array's data element, of the schema spec/stored-array.md §4.1
    <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#41-the-arrow-schema> gives."""
    pa = pytest.importorskip("pyarrow")
    core, facts, _ = parse("npy", open_transport(str(path)))
    ir = Ir(core)
    jsonschema.validate(json.loads(core.stored_arrays()), SCHEMA)
    stored = ir.stored_arrays()
    presented = [a for a in stored.arrays if a.grid is not None]
    held = [mapping(f) for f in sequence(facts["arrays"]) if "deflate" not in mapping(f)]
    assert len(presented) == len(held)
    for a, f in zip(presented, held, strict=True):
        assert a.declarations == (ElementPlace(a.element, None, None),)
        assert a.path == ir.path(a.element)
        assert (a.path or "").endswith("header")
        assert a.item_bits == integer(f["item"]) * 8
        assert a.fill == "undefined"
        assert a.chains == (0,)
    assert stored.chains == (((),) if held else ())
    cm = ir.chunk_map()
    batch = pa.record_batch(cm)
    assert batch.equals(pa.RecordBatchReader.from_stream(cm).read_next_batch())
    assert len(cm) == batch.num_rows
    assert batch.schema.metadata == {b"refkv.chunk_map": b"0"}
    assert batch.schema.names == ["array", "coords", "element", "member", "ranges", "chain", "skips"]
    assert batch.schema.field("ranges").type == pa.list_(pa.field("item", pa.struct([
        pa.field("object", pa.dictionary(pa.int32(), pa.utf8())),
        pa.field("offset", pa.uint64(), nullable=False),
        pa.field("length", pa.uint64(), nullable=False),
    ]), nullable=False))
    rows = batch.to_pylist()
    data = [(k, f["data"]) for k, f in zip([stored.arrays.index(a) for a in presented], held, strict=True)
            if integer(f["length"]) > 0]
    assert [(r["array"], r["element"]) for r in rows] == data
    assert all(r["coords"] == [0] * len(stored.arrays[r["array"]].shape) for r in rows)
    assert all(r["ranges"] is None and r["chain"] is None and r["skips"] is None for r in rows)


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
        "shape": [], "axes": None, "item_bits": None, "pad_from": None, "grid": None, "chains": [],
        "fill": "format_default", "unit_parameters": [],
    }
    source = {"source": {"element": 9, "member": None, "range": None}}
    chain = [{"name": "source", "parameters": {"id": {"value": 7}, "parameters": source}}]
    return {"chains": [chain], "arrays": [array | changes, plain]}


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
                element=4, path=None, index=None, declarations=(ExtentPlace("x.bin", 0, 1),), shape=(), axes=None,
                item_bits=None, pad_from=None, grid=None, chains=(), fill="format_default", unit_parameters=(),
            ),
        ),
    )
    jsonschema.validate(document(), SCHEMA)


def test_refuses_a_value_of_another_type():
    with pytest.raises(TypeError, match=r"arrays\[0\]\.shape\[0\]: expected an integer"):
        read(json.dumps(document(shape=["25", 12])))


def test_refuses_a_missing_member():
    doc = document()
    del doc["arrays"][0]["fill"]
    with pytest.raises(KeyError, match=r"arrays\[0\]\.fill: missing"):
        read(json.dumps(doc))


def test_refuses_a_place_of_neither_element_nor_object():
    with pytest.raises(ValueError, match="names neither an element nor an object"):
        read(json.dumps(document(declarations=[{"offset": 0, "length": 1}])))


def test_refuses_a_parameter_of_neither_value_nor_source():
    with pytest.raises(ValueError, match="neither a value nor a source"):
        read(json.dumps(document(fill={"given": {"bytes": "AAA="}})))


def test_refuses_an_axis_of_neither_each_nor_list():
    with pytest.raises(ValueError, match="neither each nor list"):
        read(json.dumps(document(grid={"axes": [{"every": 5}, {"each": 8}], "edge": "full"})))


def test_refuses_an_edge_of_neither_full_nor_short():
    with pytest.raises(ValueError, match="neither 'full' nor 'short'"):
        read(json.dumps(document(grid={"axes": [{"each": 5}, {"each": 8}], "edge": "padded"})))


def test_refuses_a_fill_of_no_form():
    with pytest.raises(ValueError, match="'zero' is not a fill"):
        read(json.dumps(document(fill="zero")))


def test_refuses_a_range_that_is_not_a_pair():
    with pytest.raises(ValueError, match="expected two integers, got 3"):
        read(json.dumps(document(declarations=[{"element": 1, "member": None, "range": [0, 4, 8]}])))
