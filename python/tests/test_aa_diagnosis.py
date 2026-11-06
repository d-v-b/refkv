"""TEMPORARY (CI diagnosis): the xarray-v2 example's made.nc case crashes the Linux job only after
earlier tests. Two subprocesses with faulthandler, their output written to the log: (a) the suite
without the tests that start a JVM, which hides faulthandler's report; (b) those tests, then the
case."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CASE = ("python/tests/test_example_xarray_v2.py::"
        "test_files_as_zarr_v2_for_xarray[made.nc-options1-codes1-True-left_out1]")
JVM = ["python/tests/ir/test_vsi.py", "python/tests/reference/test_virtualize_ome_zarr.py"]
RUNS = {
    "without-jvm": ["python/tests", "-x", "--ignore=python/tests/test_aa_diagnosis.py",
                    *(f"--ignore={p}" for p in JVM)],
    "jvm-then-case": [*JVM, CASE],
}


@pytest.mark.parametrize("name", list(RUNS))
def test_the_crash_in_a_subprocess(capsys, name):
    run = subprocess.run([sys.executable, "-X", "faulthandler", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                          *RUNS[name]], cwd=ROOT, capture_output=True, text=True, check=False)
    with capsys.disabled():
        os.write(2, f"\n>>> DIAGNOSIS {name} exit {run.returncode}\n{run.stdout[-6000:]}\n"
                    f"{run.stderr[-30000:]}\n>>> END {name}\n".encode())
