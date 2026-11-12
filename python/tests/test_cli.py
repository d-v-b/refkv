"""The command lines' usage: `-h` and `--help` print it to stdout with status 0, and a command
line without its two arguments prints it to stderr with status 2."""

import asyncio
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


@pytest.mark.parametrize("module", ["refkv.zarrify", "refkv.ir"])
def test_format_phkv_writes_a_phkv_store(module, tmp_path):
    """`--format phkv` writes a phkv store, which `refkv.open` reads by its name; the default is vzip."""
    import refkv  # noqa: PLC0415

    src = Path(__file__).resolve().parents[2] / "fixtures" / "npy" / "npy_c_float64.npy"
    outs = {}
    for fmt in ("phkv", "vzip"):
        out = tmp_path / f"o.{fmt}"
        p = _run(module, str(src), str(out), "--format", fmt, "--allow-private-hosts")
        assert p.returncode == 0, p.stderr
        outs[fmt] = refkv.open(str(out))
    assert type(outs["phkv"]).__name__ == "PhkvStore"
    assert type(outs["vzip"]).__name__ == "VZipStore"
    assert asyncio.run(outs["phkv"].get_async("zarr.json")) == asyncio.run(outs["vzip"].get_async("zarr.json"))


@pytest.mark.parametrize("module", ["refkv.zarrify", "refkv.ir"])
def test_unknown_format_is_a_usage_error(module):
    p = _run(module, "in.tif", "out", "--format", "hybrid")
    assert p.returncode == 2
    assert p.stderr.startswith("unknown --format")
