"""The command lines' usage: `-h` and `--help` print it to stdout with status 0, and a command
line without its two arguments prints it to stderr with status 2."""

import subprocess
import sys

import pytest

MODULES = ["refkv", "refkv.zarrify", "refkv.ir"]


def _run(module: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", module, *args], capture_output=True, text=True, check=False)


def test_help():
    for module, args in [("refkv", ["--help"]), ("refkv", ["-h"]), ("refkv", ["zarrify", "--help"]),
                         ("refkv.zarrify", ["--help"]), ("refkv.zarrify", ["in.tif", "-h"]),
                         ("refkv.ir", ["--help"]), ("refkv.ir", ["in.tif", "out.vzip", "-h"])]:
        p = _run(module, *args)
        assert p.returncode == 0, (module, args, p.stderr)
        assert p.stdout.startswith("usage: "), (module, args, p.stdout)
        assert p.stderr == "", (module, args, p.stderr)


@pytest.mark.parametrize("module", MODULES)
def test_usage_error_without_arguments(module):
    p = _run(module)
    assert p.returncode == 2, p.stderr
    assert p.stderr.startswith("usage: "), p.stderr
    assert p.stdout == "", p.stdout
