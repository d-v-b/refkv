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

# The modules whose tests start a JVM (bioio-bioformats'), which run last: on x86-64 Linux, h5py's
# writes crash a process in which a JVM runs, so no test that writes HDF5 runs after one starts.
JVM = ("ir/test_vsi.py", "reference/test_virtualize_ome_zarr.py")


def pytest_collection_modifyitems(items):
    items.sort(key=lambda item: item.path.as_posix().endswith(JVM))  # stable: the order is otherwise kept
