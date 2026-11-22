"""The description of every accepted IMS fixture (spec/zarrify/ims.md §7.10
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ims.md#710-the-description>), compared with
h5py, the reference reader: each level's image size, chunk shape and filters; the datatype; the
time points and channels; one entry per Data dataset; and the metadata's attributes, read as text
by the rule of §2.1 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ims.md#21-attributes-as-text>.
Each description is valid by its JSON Schema, and holds none of the output's decisions."""

import json
from pathlib import Path
from typing import Any

import h5py
import jsonschema
import numpy as np
import pytest
from ir_helpers import FIXTURES, ROOT

from refkv.build import read

FILES = sorted(p for p in (FIXTURES / "ims").glob("*.ims") if "reject" not in p.name)
SCHEMA = json.loads((ROOT / "spec" / "zarrify" / "ims" / "description.json").read_text())
# members a description never has: the output's decisions
DECISIONS = {"axes", "units", "scales", "name", "translations", "omero", "dtype", "arrays", "summary"}


def text(value: Any) -> str | None:
    """An attribute's text: its bytes up to the first NUL, UTF-8 or else Latin-1."""
    if value is None:
        return None
    if isinstance(value, str):
        data = value.encode()
    elif isinstance(value, bytes):
        data = value
    else:
        a = np.asarray(value)
        data = b"".join(x if isinstance(x, bytes) else str(x).encode() for x in a.reshape(-1))
    data = data.split(b"\0", 1)[0]
    try:
        return data.decode()
    except UnicodeDecodeError:
        return data.decode("latin-1")


def attr(group: Any, name: str) -> str | None:
    return text(group.attrs[name]) if group is not None and name in group.attrs else None


def optional(group: Any, name: str) -> str | None:
    """An attribute read as text that never rejects the input: None unless its datatype is a
    string."""
    if group is None or name not in group.attrs:
        return None
    if group.attrs.get_id(name).get_type().get_class() != h5py.h5t.STRING:
        return None
    return attr(group, name)


def decimals(t: str | None, n: int) -> list[float] | None:
    """`n` decimals separated by whitespace, else None."""
    if t is None:
        return None
    parts = t.split()
    if len(parts) != n:
        return None
    try:
        values = [float(p) for p in parts]
    except ValueError:
        return None
    return values if all(np.isfinite(values)) else None


def filters(ds: Any) -> list[int]:
    plist = ds.id.get_create_plist()
    return [plist.get_filter(i)[0] for i in range(plist.get_nfilters())]


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_the_description_is_h5pys_reading(path: Path):
    with read(path) as parsed:
        d: Any = parsed.description
    jsonschema.validate(d, SCHEMA)
    assert not DECISIONS & set(d), DECISIONS & set(d)
    with h5py.File(path, "r") as f:
        dataset = f["DataSet"]
        level0 = dataset["ResolutionLevel 0"]
        times = 0
        while f"TimePoint {times}" in level0:
            times += 1
        channels = 0
        while f"Channel {channels}" in level0["TimePoint 0"]:
            channels += 1
        assert (d["times"], d["channels"]) == (times, channels)
        assert len(d["levels"]) == sum(1 for k in dataset if k.startswith("ResolutionLevel "))
        for r, level in enumerate(d["levels"]):
            group = dataset[f"ResolutionLevel {r}"]["TimePoint 0"]["Channel 0"]
            data = group["Data"]
            assert level["image_size"] == [int(text(group.attrs[f"ImageSize{a}"]) or "") for a in "ZYX"], r
            assert level["chunk"] == list(data.chunks), r
            assert level["filters"] == filters(data), r
        dt = level0["TimePoint 0"]["Channel 0"]["Data"].dtype
        assert d["datatype"] == {
            "class": 1 if dt.kind == "f" else 0, "size": dt.itemsize, "signed": dt.kind == "i",
            "byte_order": "big" if dt.byteorder == ">" else "little",
        }
        assert sorted((e["level"], e["time"], e["channel"]) for e in d["data"]) == [
            (r, t, c) for r in range(len(d["levels"])) for t in range(times) for c in range(channels)]
        for e in d["data"]:
            ds = dataset[f"ResolutionLevel {e['level']}"][f"TimePoint {e['time']}"][f"Channel {e['channel']}"]["Data"]
            assert e["grid"] == [-(-s // k) for s, k in zip(ds.shape, ds.chunks, strict=True)], e
            assert (e["chunks"] is None) == (ds.id.get_num_chunks() == 0), e
        info = f.get("DataSetInfo")
        image = info.get("Image") if info is not None else None
        im = d["info"]["image"]
        assert im["unit"] == attr(image, "Unit")
        assert im["name"] == attr(image, "Name")
        for i in range(3):
            assert im["ext_min"][i] == (decimals(attr(image, f"ExtMin{i}"), 1) or [None])[0], i
            assert im["ext_max"][i] == (decimals(attr(image, f"ExtMax{i}"), 1) or [None])[0], i
        if channels <= 64:
            assert len(d["info"]["channels"]) == channels
            for c, ch in enumerate(d["info"]["channels"]):
                g = info.get(f"Channel {c}") if info is not None else None
                assert ch["name"] == attr(g, "Name"), c
                assert ch["color"] == decimals(attr(g, "Color"), 3), c
                assert ch["color_range"] == decimals(attr(g, "ColorRange"), 2), c
                assert ch["color_mode"] == optional(g, "ColorMode"), c
                assert ch["color_table_length"] == optional(g, "ColorTableLength"), c
                if ch["color_table"] is not None and g is not None and "ColorTable" in g.attrs:
                    assert ch["color_table"] == attr(g, "ColorTable"), c
        else:
            assert d["info"]["channels"] == []
        tp = d["info"]["time_points"]
        if times > 1:
            ti = info.get("TimeInfo") if info is not None else None
            assert tp == {"first": attr(ti, "TimePoint1"), "last": attr(ti, f"TimePoint{times}")}
        else:
            assert tp is None
