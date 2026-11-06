"""TEMPORARY (CI diagnosis): the xarray-v2 example's made.nc case, which crashes the Linux job,
run before any JVM starts, in a subprocess with faulthandler, its output written to the log."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CASE = ("python/tests/test_example_xarray_v2.py::"
        "test_files_as_zarr_v2_for_xarray[made.nc-options1-codes1-True-left_out1]")


def test_the_made_nc_case_in_a_subprocess(capsys):
    run = subprocess.run([sys.executable, "-X", "faulthandler", "-m", "pytest", "-q", "-s", "-p", "no:cacheprovider",
                          CASE], cwd=ROOT, capture_output=True, text=True, check=False)
    with capsys.disabled():
        os.write(2, f"\n>>> DIAGNOSIS exit {run.returncode}\n{run.stdout[-20000:]}\n{run.stderr[-20000:]}\n".encode())
