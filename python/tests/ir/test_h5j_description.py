"""The `h5j` member of the description of every accepted H5J fixture (spec/zarrify/h5j.md §8.6
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/h5j.md#86-the-description>): each channel's
contiguous data where h5py finds it, and its MP4 description the MP4 profile's of those bytes read
as a file, but for the IR elements it names, which are the H5J file's."""

import copy
from pathlib import Path
from typing import Any

import h5py
import pytest
from ir_helpers import FIXTURES

from refkv.build import read

FILES = sorted(p for p in (FIXTURES / "h5j").glob("*.h5j") if "reject" not in p.name)


def without_elements(d: dict[str, Any]) -> dict[str, Any]:
    d = copy.deepcopy(d)
    for t in d.get("tracks", []):
        t.pop("elements", None)
    return d


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_the_channels_are_h5pys_and_the_mp4_profiles(path: Path, tmp_path: Path):
    with read(path) as parsed:
        if parsed.profile != "h5j":
            pytest.skip(f"{path.name} is the HDF5 profile's")
        d: Any = parsed.description
    channels = d["h5j"]["channels"]
    data = path.read_bytes()
    with h5py.File(path, "r") as f:
        names = sorted(f["Channels"], key=lambda n: int(n.removeprefix("Channel_")))
        assert [c["name"] for c in channels] == names
        for c in channels:
            ds = f["Channels"][c["name"]]
            assert c["path"] == f"Channels/{c['name']}"
            if c["unread"] is not None:
                assert c["mp4"] is None
                continue
            assert (c["offset"], c["length"]) == (ds.id.get_offset(), ds.id.get_storage_size())
            mp4 = tmp_path / f"{c['name']}.mp4"
            mp4.write_bytes(data[c["offset"]:c["offset"] + c["length"]])
            with read(mp4) as alone:
                own = alone.description
            assert own is not None
            assert without_elements(c["mp4"]) == without_elements(own), c["name"]
