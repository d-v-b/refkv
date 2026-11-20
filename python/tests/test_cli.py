"""The command lines' usage: `-h` and `--help` print it to stdout with status 0, and a command
line without its two arguments prints it to stderr with status 2."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

import refkv

FILES = refkv.Policy(schemes=frozenset({"file"}))
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
    refkv.ReferenceStore({"k": b"v", "r": refkv.Range(data=b"abc")}).save_vzip(tmp_path / "a.vzip")
    p = _run("refkv", "convert", str(tmp_path / "a.vzip"), str(tmp_path / "b.phkv"))
    assert p.returncode == 0, p.stderr
    assert refkv.open(tmp_path / "b.phkv", binding="phkv").get("r") == b"abc"
    p = _run("refkv", "convert", str(tmp_path / "b.phkv"), str(tmp_path / "c.vzip"), "--page-size", "4096")
    assert p.returncode == 0, p.stderr
    assert refkv.open(tmp_path / "c.vzip").get("k") == b"v"
    (tmp_path / "x").write_bytes((tmp_path / "b.phkv").read_bytes())  # a name that says nothing
    p = _run("refkv", "convert", "--from=phkv", str(tmp_path / "x"), str(tmp_path / "d.zip"))
    assert p.returncode == 0, p.stderr


def test_convert_names_with_a_hash_or_question_mark(tmp_path):
    out = "x#1.phkv" if sys.platform == "win32" else "x?1.phkv"  # Windows forbids "?" in a file name
    refkv.ReferenceStore({"k": b"v"}).save_vzip(tmp_path / "a#1.vzip")
    p = _run("refkv", "convert", str(tmp_path / "a#1.vzip"), str(tmp_path / out))
    assert p.returncode == 0, p.stderr
    assert refkv.open(tmp_path / out, binding="phkv").get("k") == b"v"


def test_convert_keeps_relative_sources_relative(tmp_path):
    """The folder of the archive and its sources can move after the conversion."""
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "blob.bin").write_bytes(b"0123456789")
    refkv.ReferenceStore({"k": refkv.Range(source=0, offset=2, length=3)}, [refkv.Source(url="blob.bin")],
                         url=(tmp_path / "d" / "a.vzip").as_uri()).save_vzip(tmp_path / "d" / "a.vzip", policy=FILES)
    p = _run("refkv", "convert", str(tmp_path / "d" / "a.vzip"), str(tmp_path / "d" / "a.phkv"),
             "--allow-scheme", "file")
    assert p.returncode == 0, p.stderr
    (tmp_path / "d").rename(tmp_path / "moved")
    assert refkv.open(tmp_path / "moved" / "a.phkv", binding="phkv", policy=FILES).get("k") == b"234"


@pytest.mark.parametrize("args", [["one"], ["a.vzip", "b.vzip"], ["a", "b.phkv", "--to"], ["a", "b", "--from", "kv"],
                                  ["a", "b", "-x"]])
def test_convert_usage_error(args):
    p = _run("refkv", "convert", *args)
    assert p.returncode == 2, p.stderr
    assert p.stderr
    assert p.stdout == ""


def test_convert_of_a_bad_store_fails_and_leaves_no_file(tmp_path):
    (tmp_path / "a.vzip").write_bytes(b"not an archive")
    p = _run("refkv", "convert", str(tmp_path / "a.vzip"), str(tmp_path / "b.phkv"))
    assert p.returncode == 1
    assert "ArchiveError" in p.stderr
    assert not (tmp_path / "b.phkv").exists()


def test_convert_help():
    p = _run("refkv", "convert", "--help")
    assert p.returncode == 0, p.stderr
    assert p.stdout.startswith("usage: refkv convert")


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
