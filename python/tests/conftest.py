import pytest
import xarray as xr
from helpers import make_netcdf


@pytest.fixture
def netcdf_files(tmp_path):
    paths = [tmp_path / f"f{i}.nc" for i in range(3)]
    expected = xr.concat([make_netcdf(p, 4 * i) for i, p in enumerate(paths)], dim="time")
    return paths, expected
