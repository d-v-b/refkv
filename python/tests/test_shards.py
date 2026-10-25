import itertools
from typing import Literal

import pytest
import xarray as xr
from helpers import zarrify

import refkv.zarr
from refkv.pb import Concat
from refkv.shards import write_vzip_sharded
from refkv.vzip.store import VZipStore


def test_virtual_shards_roundtrip(netcdf_files, tmp_path):
    paths, expected = netcdf_files
    vds = xr.concat([zarrify(p) for p in paths], dim="time", coords="minimal",
                    compat="override", data_vars="all")
    locations: list[Literal["end", "start"]] = ["end", "start"]
    cases = itertools.product([False, True], [None, 128], locations)
    for inline, page_size, loc in cases:
        out = tmp_path / f"s{inline}{page_size}{loc}.vzip"
        # temp: one shard per file (4 time steps x the full 2x2 spatial chunk grid)
        write_vzip_sharded(vds, out, {"temp": (4, 2, 2), "mask": (4, 3, 4)},
                           inline_index=inline, page_size=page_size, index_location=loc)
        zs = refkv.zarr.open(out)
        ds = xr.open_zarr(zs, consolidated=True, zarr_format=3)
        xr.testing.assert_identical(ds.load(), expected.load())
        s = zs.reader
        assert isinstance(s, VZipStore)
        refs = [k for k in s.list() if s.classify(k) == "reference"]
        e = s.entry("temp/c/0/0/0")
        assert e is not None
        assert isinstance(e.ref, Concat)
        assert e.ref.parts[0 if loc == "start" else 1].size == 16 * 16
        assert sorted(refs) == [f"{v}/c/{i}/0/0" for v in ("mask", "temp") for i in range(3)]


def test_shard_spanning_files_rejected(netcdf_files, tmp_path):
    paths, _ = netcdf_files
    vds = xr.concat([zarrify(p) for p in paths], dim="time", coords="minimal",
                    compat="override", data_vars="all")
    with pytest.raises(ValueError, match="spans 2 files"):
        write_vzip_sharded(vds, tmp_path / "x.vzip", {"temp": (8, 2, 2), "mask": (4, 3, 4)})
