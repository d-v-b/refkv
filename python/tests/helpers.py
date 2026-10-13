"""Helpers shared by the test modules (a plain module: test modules import it, not conftest)."""

import numpy as np
import pandas as pd
import xarray as xr
from obspec_utils.registry import ObjectStoreRegistry
from obstore.store import LocalStore
from virtualizarr import open_virtual_dataset
from virtualizarr.parsers import HDFParser

from refkv.uri import file_uri


def make_netcdf(path, t0: int, nt: int = 4) -> xr.Dataset:
    rng = np.random.default_rng(t0)
    ds = xr.Dataset(
        {
            "temp": (("time", "lat", "lon"), rng.standard_normal((nt, 30, 40)).astype("f4")),
            "mask": (("lat", "lon"), (rng.random((30, 40)) > 0.5).astype("i1")),
        },
        coords={
            "time": pd.date_range("2000-01-01", periods=nt, freq="D") + pd.Timedelta(days=t0),
            "lat": np.linspace(-90, 90, 30),
            "lon": np.linspace(0, 360, 40, endpoint=False),
        },
        attrs={"title": "synthetic", "source": f"file {t0}"},
    )
    ds["temp"].attrs = {"units": "K", "long_name": "temperature"}
    enc = {
        "temp": {"chunksizes": (1, 15, 20), "zlib": True, "shuffle": True, "complevel": 4},
        "mask": {"chunksizes": (10, 10)},
    }
    ds.to_netcdf(path, engine="h5netcdf", encoding=enc)
    return ds


def virtualize(path, loadable_variables=("time", "lat", "lon")) -> xr.Dataset:
    registry = ObjectStoreRegistry({"file://": LocalStore()})
    return open_virtual_dataset(
        url=file_uri(path),
        registry=registry,
        parser=HDFParser(),
        loadable_variables=list(loadable_variables),
    )
