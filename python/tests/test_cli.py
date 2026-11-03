"""The command lines' usage: `-h` and `--help` print it to stdout with status 0, and a command
line without its two arguments prints it to stderr with status 2."""

import json
import subprocess
import sys
from pathlib import Path

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


def _write_invalid(directory: Path, out: Path) -> subprocess.CompletedProcess[str]:
    """The harness's `write` of a description the writer refuses partway: a key used twice."""
    entry = {"key": "a", "bytes": "00"}
    description = directory / "description.json"
    description.write_text(json.dumps({"entries": [entry, entry]}))
    return _run("refkv.vzip.cli", "write", str(description), str(out))


def test_vzip_write_failing_keeps_the_previous_file(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    path = out / "a.vzip"
    path.write_bytes(b"previous")
    p = _write_invalid(tmp_path, path)
    assert p.returncode != 0, p.stderr
    assert "duplicate key" in p.stderr
    assert path.read_bytes() == b"previous"
    assert [x.name for x in out.iterdir()] == ["a.vzip"]


def test_vzip_write_failing_on_a_new_path_leaves_nothing(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    p = _write_invalid(tmp_path, out / "a.vzip")
    assert p.returncode != 0, p.stderr
    assert "duplicate key" in p.stderr
    assert list(out.iterdir()) == []
