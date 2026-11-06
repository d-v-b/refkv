import os

import pytest
import xarray as xr
import zarr
from helpers import make_netcdf

# zarr-n5, a development dependency, registers an `n5_default` too: the tests read N5 arrays with
# refkv's, as a reader with both installed chooses it.
zarr.config.set({"codecs.n5_default": "refkv.zarr.codecs.n5_default.N5DefaultCodec"})


@pytest.fixture
def netcdf_files(tmp_path):
    paths = [tmp_path / f"f{i}.nc" for i in range(3)]
    expected = xr.concat([make_netcdf(p, 4 * i) for i, p in enumerate(paths)], dim="time")
    return paths, expected


# TEMPORARY (CI diagnosis): name each test on stderr, unbuffered, before it runs
def pytest_runtest_logstart(nodeid):
    capture = _config.pluginmanager.getplugin("capturemanager")
    with capture.global_and_fixture_disabled():
        os.write(2, f"\n>>> {nodeid}\n".encode())


def pytest_configure(config):
    global _config  # noqa: PLW0603
    _config = config
