"""The description of every accepted Neuroglancer precomputed fixture (spec/zarrify/precomputed.md
§11.4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/precomputed.md#114-the-description>),
compared with tensorstore's `neuroglancer_precomputed` driver, the reference reader, and with
Python's `json`: `info` as parsed is what `json` reads of the store's `info`, and each scale
tensorstore opens has the data type, channels, volume type, key, size, voxel offset, chunk size,
resolution, encoding and sharding the description's `info` gives. A second test asserts that the
description holds none of the output's decisions."""

import json
from pathlib import Path
from typing import Any

import jsonschema
import tensorstore as ts
from ir_helpers import FIXTURES, ROOT

from refkv.build import project, read

STORES = sorted(p for p in (FIXTURES / "precomputed").iterdir() if p.is_dir() and "reject" not in p.name)
SCHEMA = json.loads((ROOT / "spec" / "zarrify" / "precomputed" / "description.json").read_text())
# the encodings tensorstore's driver reads; it refuses an info that names another
TENSORSTORE_ENCODINGS = {"raw", "png", "jpeg", "compressed_segmentation"}


def tensorstore_scale(store: Path, index: int) -> dict[str, Any] | str:
    """The spec tensorstore's driver gives the scale `index` of `store`, or why it refuses it."""
    spec = {"driver": "neuroglancer_precomputed", "kvstore": {"driver": "file", "path": f"{store}/"},
            "scale_index": index}
    try:
        t = ts.open(spec, open=True).result()
    except ValueError as e:
        return str(e).split(" [tensorstore_spec")[0]
    return {**t.spec().to_json(), "dtype": str(t.dtype.numpy_dtype), "shape": list(t.domain.shape)}


def alone(info: dict[str, Any], index: int) -> dict[str, Any]:
    """`info` with its scale `index` alone, its encoding named `raw` where tensorstore's driver has
    no decoder for it: the driver refuses an info that has one scale it refuses, or names one
    encoding it lacks, and what it reads of a scale's geometry does not depend on the encoding."""
    scale = info["scales"][index]
    if isinstance(scale, dict) and scale.get("encoding") not in TENSORSTORE_ENCODINGS:
        scale = {**scale, "encoding": "raw"}
    return {**info, "scales": [scale]}


def presented(parsed: Any) -> set[int]:
    """The indexes in `info` of the scales refkv's own output presents."""
    doc = project(parsed).written["zarr.json"]
    assert isinstance(doc, bytes)
    root = json.loads(doc)
    left_out = root["attributes"]["refkv_zarrified"]["precomputed"].get("not_presented", [])
    return set(range(len(json.loads(parsed.description["info"])["scales"]))) - {x["index"] for x in left_out}


def test_a_precomputed_stores_description_is_what_tensorstore_and_json_read(tmp_path):
    """Every accepted precomputed fixture: the description is valid by its schema; `info` is
    Python's `json` reading of the store's `info`; and each scale, opened alone by tensorstore's
    driver (its encoding named `raw` where the driver lacks it), has the description's data type,
    channels, volume type, key, size, voxel offset, chunk size, resolution, encoding and sharding.
    A scale the driver refuses is one refkv does not present, or of a data type the driver lacks."""
    seen = dict.fromkeys(("scales", "sharded", "renamed", "refused", "data type"), 0)
    for store in STORES:
        with read(store) as parsed:
            assert parsed.profile == "precomputed", store.name
            description: Any = parsed.description
            shown = presented(parsed)
        assert not list(jsonschema.Draft202012Validator(SCHEMA).iter_errors(description)), store.name
        info = json.loads(description["info"])
        assert info == json.loads((store / "info").read_bytes()), store.name
        for i, scale in enumerate(info["scales"]):
            at = f"{store.name}: scale {i}"
            opened = tmp_path / f"{store.name}-{i}"
            opened.mkdir()
            (opened / "info").write_text(json.dumps(alone(info, i)))
            seen["renamed"] += isinstance(scale, dict) and scale.get("encoding") not in TENSORSTORE_ENCODINGS
            theirs = tensorstore_scale(opened, 0)
            if isinstance(theirs, str):
                if "data_type" in theirs or "supported data types" in theirs:
                    seen["data type"] += 1
                else:
                    assert i not in shown, f"{at}: {theirs}"
                    seen["refused"] += 1
                continue
            meta, own = theirs["multiscale_metadata"], theirs["scale_metadata"]
            assert theirs["dtype"] == info["data_type"], at
            assert meta["num_channels"] == info["num_channels"], at
            assert meta["type"] == info["type"], at
            for k in ("key", "size", "chunk_size", "resolution"):
                want = scale["chunk_sizes"][0] if k == "chunk_size" else scale[k]
                assert own[k] == want, f"{at}: {k}"
            assert own["voxel_offset"] == scale.get("voxel_offset", [0, 0, 0]), at
            if scale.get("encoding") in TENSORSTORE_ENCODINGS:
                assert own["encoding"] == scale["encoding"], at
            sharding = scale.get("sharding")
            assert (own["sharding"] is None) == (sharding is None), at
            if sharding is not None:
                for k, v in sharding.items():
                    assert own["sharding"].get(k, v) == v, f"{at}: sharding {k}"
                seen["sharded"] += 1
            assert theirs["shape"] == [*scale["size"], info["num_channels"]], at
            seen["scales"] += 1
    assert all(seen.values()), seen


def test_a_parsed_precomputed_store_describes_info_and_no_decision():
    """`Parsed.description` of a precomputed store holds `info` and the documents, and none of the
    hierarchy's decisions: no scale presented or left out, no `zarr.json`, image or summary."""
    with read(FIXTURES / "precomputed" / "precomputed_not_presented") as parsed:
        description: Any = parsed.description
    assert set(description) == {"info", "documents"}
    assert description["documents"] == ["info"]
