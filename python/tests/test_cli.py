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


def test_convert_round_trip(tmp_path):
    """`refkv convert` takes the binding from the names, or from --from and --to."""
    import refkv  # noqa: PLC0415

    refkv.ReferenceStore({"k": b"v", "r": refkv.Range(data=b"abc")}).save_vzip(tmp_path / "a.vzip")
    p = _run("refkv", "convert", str(tmp_path / "a.vzip"), str(tmp_path / "b.phkv"))
    assert p.returncode == 0, p.stderr
    assert refkv.open(tmp_path / "b.phkv", binding="phkv").get("r") == b"abc"
    p = _run("refkv", "convert", str(tmp_path / "b.phkv"), str(tmp_path / "c.vzip"))
    assert p.returncode == 0, p.stderr
    assert refkv.open(tmp_path / "c.vzip").get("k") == b"v"
    (tmp_path / "x").write_bytes((tmp_path / "b.phkv").read_bytes())  # a name that says nothing
    p = _run("refkv", "convert", "--from", "phkv", str(tmp_path / "x"), str(tmp_path / "d.zip"))
    assert p.returncode == 0, p.stderr


def test_convert_refuses_what_it_cannot_do(tmp_path):
    (tmp_path / "a.vzip").write_bytes(b"not an archive")
    for args in (["one"], ["a.vzip", "b.vzip"], ["a", "b.phkv", "--to"], ["a", "b", "--from", "kv"], ["a", "b", "-x"]):
        p = _run("refkv", "convert", *args)
        assert p.returncode == 2, (args, p.stderr)
        assert p.stderr, args
    p = _run("refkv", "convert", str(tmp_path / "a.vzip"), str(tmp_path / "b.phkv"))
    assert p.returncode == 1
    assert "ArchiveError" in p.stderr
    assert not (tmp_path / "b.phkv").exists()
    assert _run("refkv", "convert", "--help").stdout.startswith("usage: refkv convert")
