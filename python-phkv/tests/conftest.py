"""refkv-phkv's tests share refkv's HTTP test server and helpers (python/tests), which they import
by name, as refkv's own tests do: this puts that directory on the path."""

import sys
from pathlib import Path

REFKV_TESTS = str(Path(__file__).resolve().parents[2] / "python" / "tests")
if REFKV_TESTS not in sys.path:
    sys.path.append(REFKV_TESTS)
