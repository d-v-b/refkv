import subprocess
import sys
from pathlib import Path

import pytest
from ir_helpers import ROOT


@pytest.fixture(scope="session")
def probes(tmp_path_factory) -> Path:
    """The round-3 probes at the tests' sizes (python/tests/ir/gen_probes.py --small)."""
    out = tmp_path_factory.mktemp("probes")
    subprocess.run([sys.executable, "python/tests/ir/gen_probes.py", str(out), "--small"], cwd=ROOT,
                   check=True, capture_output=True)
    return out


@pytest.fixture(scope="session")
def ims_probes(tmp_path_factory) -> Path:
    """The IMS probes at the tests' sizes (python/tests/ir/gen_ims_probes.py --small)."""
    out = tmp_path_factory.mktemp("ims_probes")
    p = subprocess.run([sys.executable, "python/tests/ir/gen_ims_probes.py", str(out), "--small"], cwd=ROOT,
                       capture_output=True, text=True, check=False)
    if p.returncode:
        raise RuntimeError(f"gen_ims_probes.py failed: {p.stderr[-2000:]}")
    return out
