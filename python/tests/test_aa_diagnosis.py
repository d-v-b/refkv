"""TEMPORARY (CI diagnosis): the xarray-v2 example's made.nc case crashes the Linux job once a JVM
has started (bioio-bioformats', in the VSI tests), and the JVM hides faulthandler's report. A
subprocess starts the JVM, then runs the case, writing each line of the test it executes to the
log: the last is the one that crashes."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CASE = ("python/tests/test_example_xarray_v2.py::"
        "test_files_as_zarr_v2_for_xarray[made.nc-options1-codes1-True-left_out1]")
SCRIPT = f"""
import os, sys
from bioio_bioformats import Reader
Reader({str(ROOT / "fixtures/vsi/vsi_no_stacks.vsi")!r})
os.write(2, b">>> JVM started\\n")

def trace(frame, event, arg):
    name = frame.f_code.co_filename
    if name.endswith(("test_example_xarray_v2.py", "examples/xarray_v2.py")):
        if event == "line":
            os.write(2, f">>> {{os.path.basename(name)}}:{{frame.f_lineno}}\\n".encode())
        return trace
    return None

sys.settrace(trace)
import pytest
sys.exit(pytest.main(["-q", "-s", "-p", "no:cacheprovider", {CASE!r}]))
"""


def test_the_crash_after_a_jvm(capsys):
    run = subprocess.run([sys.executable, "-X", "faulthandler", "-c", SCRIPT], cwd=ROOT, capture_output=True,
                         text=True, check=False)
    with capsys.disabled():
        os.write(2, f"\n>>> DIAGNOSIS exit {run.returncode}\n{run.stdout[-4000:]}\n"
                    f"{run.stderr[-12000:]}\n>>> END\n".encode())
