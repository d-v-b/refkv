"""The description of every accepted N5 fixture (spec/zarrify/n5.md §8.3
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/n5.md#83-the-description>), compared with
tensorstore's N5 driver, the reference reader, and with Python's `json`: each node's document as
parsed is what `json` reads of its `attributes.json`; a node is a dataset where tensorstore opens it
as one, with the dimensions, block size, data type and compression the document gives, and a group
where tensorstore finds no dataset; and every `attributes.json` outside a dataset is a node's. A second
test asserts that the description holds none of the output's decisions."""

import json
from pathlib import Path
from typing import Any

import jsonschema
import tensorstore as ts
from ir_helpers import FIXTURES, ROOT

from refkv.build import read

STORES = sorted(p for p in (FIXTURES / "n5").iterdir() if p.is_dir() and "reject" not in p.name)
SCHEMA = json.loads((ROOT / "spec" / "zarrify" / "n5" / "description.json").read_text())


def tensorstore_metadata(path: Path) -> dict[str, Any] | str | None:
    """The metadata tensorstore's N5 driver reads of the dataset at `path`; None where it opens no
    dataset there (the document has no `dimensions`); else why it refuses the document."""
    try:
        t = ts.open({"driver": "n5", "kvstore": {"driver": "file", "path": f"{path}/"}}, open=True).result()
    except ValueError as e:
        why = str(e).split(" [tensorstore_spec")[0]
        return None if 'member "dimensions": Expected array, but member is missing' in why else why
    return t.spec().to_json()["metadata"]


def as_tensorstore_reads(doc: dict[str, Any]) -> dict[str, Any]:
    """A dataset's document as tensorstore's N5 driver takes one: N5 1.0.0's `compression` object
    for a `compressionType` (N5 before 1.0.0, which tensorstore does not read), and without the
    compression's members tensorstore does not know (`nthreads`, `nbWorkers`: settings of a writer,
    which N5's own codecs take and tensorstore refuses)."""
    out = dict(doc)
    compression = dict(doc.get("compression", {"type": doc.get("compressionType")}))
    for k in ("nthreads", "nbWorkers"):
        compression.pop(k, None)
    out["compression"] = compression
    out.pop("compressionType", None)
    return out


def outside_datasets(store: Path, datasets: set[str]) -> set[str]:
    """The paths of the `attributes.json` documents of `store` that are not inside a dataset."""
    found = set()
    for doc in store.rglob("attributes.json"):
        path = "" if doc.parent == store else doc.parent.relative_to(store).as_posix()
        if not any(path.startswith(f"{d}/") or (d == "" and path) for d in datasets):
            found.add(path)
    return found


def test_an_n5_stores_description_is_what_tensorstore_and_json_read(tmp_path):
    """Every accepted N5 fixture: the description is valid by its schema; each node's `json` is
    Python's `json` reading of its `attributes.json`; a node is a dataset where tensorstore's N5
    driver opens one, whose dimensions, block size, data type and compression (`compressionType`
    of a document before N5 1.0.0 as `{"type": t}`) are its document's (a document it refuses,
    being stricter than N5, read again as it takes one), and a group where it opens none; the
    nodes are every `attributes.json` outside a dataset; and `documents` names each node's."""
    seen = dict.fromkeys(("dataset", "group", "legacy", "normalized", "below a dataset"), 0)
    for store in STORES:
        with read(store) as parsed:
            assert parsed.profile == "n5", store.name
            description: Any = parsed.description
        assert not list(jsonschema.Draft202012Validator(SCHEMA).iter_errors(description)), store.name
        nodes = description["nodes"]
        for n in nodes:
            at = f"{store.name}: {n['path'] or '/'}"
            folder = store / n["path"] if n["path"] else store
            doc = json.loads(n["json"])
            assert doc == json.loads((folder / "attributes.json").read_bytes()), at
            theirs = tensorstore_metadata(folder)
            if n["kind"] == "group":
                assert theirs is None, at
                seen["group"] += 1
                continue
            assert theirs is not None, at
            if isinstance(theirs, str):
                # tensorstore's reader is stricter than N5's: read the document as it takes one
                seen["normalized"] += 1
                (tmp_path / "attributes.json").write_text(json.dumps(as_tensorstore_reads(doc)))
                theirs = tensorstore_metadata(tmp_path)
                assert isinstance(theirs, dict), f"{at}: {theirs}"
            assert theirs["dimensions"] == doc["dimensions"], at
            assert theirs["blockSize"] == doc["blockSize"], at
            assert theirs["dataType"] == doc["dataType"], at
            compression = doc.get("compression", {"type": doc.get("compressionType")})
            assert theirs["compression"]["type"] == compression["type"], at
            for k, v in compression.items():
                if k in theirs["compression"] and k != "type":
                    assert theirs["compression"][k] == v, f"{at}: {k}"
            seen["dataset"] += 1
            seen["legacy"] += "compression" not in doc
        datasets = {n["path"] for n in nodes if n["kind"] == "dataset"}
        paths = [n["path"] for n in nodes]
        assert set(paths) == outside_datasets(store, datasets), store.name
        assert len(paths) == len(set(paths)), store.name
        seen["below a dataset"] += len(paths) < len(list(store.rglob("attributes.json")))
        keys = [f"{p}/attributes.json" if p else "attributes.json" for p in paths]
        assert description["documents"] == keys, store.name
    assert all(seen.values()), seen


def test_a_parsed_n5_store_describes_its_nodes_and_no_decision():
    """`Parsed.description` of an N5 store holds its nodes and their documents, and none of the
    hierarchy's decisions: no `zarr.json`, Zarr data type, codec, chunk grid, source metadata or
    recognized image."""
    with read(FIXTURES / "n5" / "n5_cosem") as parsed:
        description: Any = parsed.description
    assert set(description) == {"nodes", "documents"}
    decisions = {"zarr", "data_type", "codecs", "chunk_grid", "own", "ome", "images", "array"}
    assert not any(decisions & set(n) for n in description["nodes"])
    assert {n["kind"] for n in description["nodes"]} == {"dataset", "group"}
