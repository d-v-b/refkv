"""Run the phkv conformance suite against one or more implementations.

    uv run python conformance/phkv/run.py \\
        --impl ref="uv run python -m refkv.phkv.cli --allow-private-hosts" \\
        --impl js="node conformance/phkv/impls/js/cli.ts --allow-private-hosts" --out conformance/results/phkv

The same interface and checks as the vzip suite (conformance/vzip/run.py, HARNESS.md), for stores of
the standalone profile of spec/bindings/phkv.md. For every implementation:
  1. read:   read every store written by the reference writer, and the crafted stores
             (malformed ones must fail to open), and compare each query result with the oracle;
  2. write:  write every valid description, validate the store structurally, and read it back with
             every implementation (a cross-read matrix);
  3. reject: every invalid description must be rejected.

`--no-file-sources NAME` (repeatable) exempts an implementation that reads only HTTP sources from the
queries that need a `file:` source or an `http` source on a path the runner does not serve: it
reads, and is read, only through descriptions without such sources.
"""

from __future__ import annotations

import argparse
import fnmatch
import importlib.util
import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "vzip"))

import phkv_cases as cases  # noqa: E402
from http_server import Server  # noqa: E402
from phkv_model import PhkvModel, deflates_to  # noqa: E402
from phkv_validate import validate  # noqa: E402

_spec = importlib.util.spec_from_file_location("vzip_run", HERE.parent / "vzip" / "run.py")
assert _spec is not None
assert _spec.loader is not None
vzip_run = importlib.util.module_from_spec(_spec)
sys.modules["vzip_run"] = vzip_run
_spec.loader.exec_module(vzip_run)

TIMEOUT = vzip_run.TIMEOUT
run_read = vzip_run.run_read
grade = vzip_run.grade
http_accounting = vzip_run.http_accounting
ACCOUNTING = vzip_run.ACCOUNTING


def _matches(result: dict, accept: list[dict]) -> bool:
    """As the vzip suite's, and an acceptable `{"deflates_to": hex}` accepts a raw DEFLATE stream."""
    for a in accept:
        if a.get("ok") is False:
            if result.get("ok") is False and ("class" not in a or result.get("class") == a["class"]):
                return True
        elif "deflates_to" in a:
            if result.get("ok") is True and isinstance(result.get("value"), str) \
                    and deflates_to(result["value"], a["deflates_to"]):
                return True
        elif {k: result.get(k) for k in a} == a and result.get("ok") is True:
            return True
    return False


vzip_run.__dict__["_matches"] = _matches  # grade, in vzip's runner, uses it

# a store that is not valid: opening it must fail with an archive error
# (spec <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#221-opening>
# and <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#223-errors>)
STORE_SUFFIX = ".phkv"


def _flip(buf: bytearray, at: int) -> bytes:
    buf[at] ^= 0x01
    return bytes(buf)


def crafted(base: bytes) -> dict[str, bytes]:
    """Malformed stores made from a valid one, each of which must fail to open with an archive error."""
    n = len(base)
    sb = n - 64  # the superblock is the last 64 bytes (distance 0)
    dir_off = int.from_bytes(base[sb + 24:sb + 32], "little")
    return {
        "empty_file": b"",
        "not_a_store": bytes(range(256)) * 8,
        "just_a_magic": b"PHKV" + bytes(60),
        "truncated_by_one": base[:-1],
        "truncated_to_half": base[: n // 2],
        "superblock_crc_flipped": _flip(bytearray(base), sb + 60),
        "superblock_magic_flipped": _flip(bytearray(base), sb),
        "directory_flipped": _flip(bytearray(base), dir_off),
        "version_1": _with_crc(base, sb, 4, (1).to_bytes(2, "little")),
        "unknown_flag": _with_crc(base, sb, 6, (2).to_bytes(2, "little")),
        "trailing_bytes_after_superblock": base + b"\0" * 7,
    }


def _with_crc(base: bytes, sb: int, at: int, value: bytes) -> bytes:
    """`base` with `value` at `at` in its superblock, and the superblock's crc32c made right."""
    from phkv_validate import crc32c  # noqa: PLC0415

    b = bytearray(base)
    b[sb + at:sb + at + len(value)] = value
    b[sb + 60:sb + 64] = crc32c(bytes(b[sb:sb + 60])).to_bytes(4, "little")
    return bytes(b)


def needs_files(desc: dict) -> bool:
    """Whether reading a description's references needs a source that is not served over HTTP or held
    in the store: a `url` that is not http(s)."""
    return any("url" in s and not s["url"].startswith(("http://", "https://")) for s in desc.get("sources", []))


def build_vectors(root: Path, ref_cli: list[str], http: Server, files: bool = True) -> dict[str, dict]:
    vdir = root / "vectors"
    if vdir.exists():
        shutil.rmtree(vdir)
    vdir.mkdir(parents=True)
    cases.write_data_files(vdir)
    vectors = {}
    for name, desc in cases.descriptions(vdir, http_base=http.base).items():
        if needs_files(desc) and not files:
            continue
        dpath = vdir / f"{name}.json"
        dpath.write_text(json.dumps(desc))
        store = vdir / f"{name}{STORE_SUFFIX}"
        p = subprocess.run([*ref_cli, "write", str(dpath), str(store)], capture_output=True, text=True, check=False)
        if p.returncode:
            raise RuntimeError(f"reference writer failed on {name}: {p.stderr}")
        qs = cases.queries_for(name, desc)
        m = PhkvModel(desc, store, http=(http.base, root))
        vectors[name] = {"archive": store, "queries": qs, "accepts": [m.expect(q) for q in qs],
                         "open": "ok", "files": needs_files(desc)}
    base = (vdir / f"selfcontained{STORE_SUFFIX}").read_bytes()
    for name, data in crafted(base).items():
        store = vdir / f"crafted_{name}{STORE_SUFFIX}"
        store.write_bytes(data)
        qs = [{"op": "get", "key": "meta/zarr.json"}]
        vectors[f"crafted/{name}"] = {"archive": store, "queries": qs, "accepts": [[{"ok": False}]],
                                      "open": "fail", "files": False}
    return vectors


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--impl", action="append", required=True, help="name=command")
    ap.add_argument("--out", required=True)
    ap.add_argument("--ref", default="uv run python -m refkv.phkv.cli --allow-private-hosts")
    ap.add_argument("--skip", action="append", default=[], help="skip descriptions by name")
    ap.add_argument("--no-file-sources", action="append", default=[], metavar="NAME")
    ap.add_argument("--known-gaps", metavar="FILE",
                    help="a JSON object of fnmatch patterns of failing case ids, each with its reason: "
                         "failures that do not make the run fail (a pattern that no longer matches one does)")
    ap.add_argument("--no-files", action="store_true",
                    help="skip every description with a `file:` source, for every implementation "
                         "(the oracle's file: URLs are POSIX's: on Windows, use this)")
    args = ap.parse_args()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    impls = {k: shlex.split(v) for k, v in (i.split("=", 1) for i in args.impl)}
    ref_cli = shlex.split(args.ref)
    readers = {"ref": ref_cli, **impls}
    no_files = set(args.no_file_sources)

    http = Server(out)
    vectors = build_vectors(out, ref_cli, http, files=not args.no_files)
    report: dict = {"read": {}, "write": {}, "reject": {}, "cross": {}, "divergence": []}

    def applies(impl: str, desc_needs_files: bool) -> bool:
        return not (desc_needs_files and (args.no_files or impl in no_files))

    # 1. read vectors
    for impl, cli in impls.items():
        for vname, v in vectors.items():
            if not applies(impl, v["files"]):
                continue
            res = run_read(cli, v["archive"], v["queries"], out / "vectors")
            fails = grade(res, v["queries"], v["accepts"], v["open"])
            report["read"][f"{impl}:{vname}"] = {"queries": len(v["queries"]), "failures": fails,
                                                  "seconds": res.get("seconds")}
            print(f"read   {impl:6s} {vname:40s} {len(v['queries']) - len(fails):4d}/{len(v['queries'])}", flush=True)

    # 1b. malformed queries files must make the CLI exit non-zero (HARNESS)
    any_vec = next(iter(vectors.values()))["archive"]
    bad_queries = {
        "unknown_op": [{"op": "frobnicate", "key": "x"}],
        "missing_key": [{"op": "get"}],
        "range_with_two_forms": [{"op": "get", "key": "x", "range": {"offset": 1, "suffix": 2}}],
        "not_an_array": {"op": "get", "key": "x"},
        "range_on_get_raw": [{"op": "get_raw", "key": "x", "range": {"offset": 1}}],
        "list_without_prefix": [{"op": "list"}],
        "range_null": [{"op": "get", "key": "x", "range": None}],
        "partial_range": [{"op": "get", "key": "x", "range": {"start": 1}}],
        "negative_range": [{"op": "get", "key": "x", "range": {"offset": -1}}],
        "duplicate_members": '[{"op": "get", "key": "x", "key": "y"}]',
        "non_string_key": [{"op": "get", "key": 7}],
        "lone_surrogate_key": '[{"op": "get", "key": "a\\ud800"}]',
        "number_2_pow_53": [{"op": "get", "key": "x", "range": {"offset": 2**53}}],
    }
    for impl, cli in impls.items():
        for name, q in bad_queries.items():
            qp = out / "vectors" / f"bad_{name}.json"
            qp.write_text(q if isinstance(q, str) else json.dumps(q))
            p = subprocess.run([*cli, "read", str(any_vec), str(qp)], capture_output=True, text=True,
                               timeout=TIMEOUT, check=False)
            report["reject"][f"{impl}:queries/{name}"] = {
                "ok": p.returncode != 0, "exit": p.returncode, "file_left": False, "stderr": p.stderr.strip()[-200:]}

    # 1c. http profile: request accounting, one query per CLI run
    # (spec <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#52-reading-over-http>)
    if "http_basic" in vectors:
        arc = vectors["http_basic"]["archive"]
        for impl, cli in impls.items():
            report["read"][f"{impl}:http/request_accounting"] = {
                "queries": len(ACCOUNTING), "failures": http_accounting(cli, arc, http, out)}

    # 2. write + validate + cross-read
    for impl, cli in impls.items():
        wdir = out / "written" / impl
        if wdir.exists():
            shutil.rmtree(wdir)
        wdir.mkdir(parents=True)
        cases.write_data_files(wdir)
        for name, desc in cases.descriptions(wdir, http_base=http.base).items():
            if name in args.skip or not applies(impl, needs_files(desc)):
                continue
            dpath = wdir / f"{name}.json"
            dpath.write_text(json.dumps(desc))
            store = wdir / f"{name}{STORE_SUFFIX}"
            p = subprocess.run([*cli, "write", str(dpath), str(store)], capture_output=True, text=True,
                               timeout=TIMEOUT, check=False)
            if p.returncode or not store.exists():
                report["write"][f"{impl}:{name}"] = {"problems": [
                    f"write failed (exit {p.returncode}): {p.stderr.strip()[-500:]}"]}
                print(f"write  {impl:6s} {name:40s} FAILED TO WRITE", flush=True)
                continue
            try:
                problems = validate(store, desc)
            except Exception as e:  # noqa: BLE001 - a broken store must not stop the run
                problems = [f"validator crashed: {type(e).__name__}: {e}"]
            report["write"][f"{impl}:{name}"] = {"problems": problems}
            print(f"write  {impl:6s} {name:40s} {'ok' if not problems else f'{len(problems)} problems'}", flush=True)
            qs = cases.queries_for(name, desc)
            m = PhkvModel(desc, store, http=(http.base, out))
            accepts = [m.expect(q) for q in qs]
            for reader, rcli in readers.items():
                if not applies(reader, needs_files(desc)):
                    continue
                res = run_read(rcli, store, qs, wdir)
                fails = grade(res, qs, accepts, "ok")
                report["cross"][f"{impl}->{reader}:{name}"] = {"queries": len(qs), "failures": fails}
                print(f"cross  {impl:6s} read by {reader:6s} {name:24s} "
                      f"{len(qs) - len(fails):4d}/{len(qs)}", flush=True)

        # 3. invalid descriptions
        for name, desc in cases.invalid_descriptions(wdir).items():
            dpath = wdir / f"invalid_{name}.json"
            dpath.write_text(json.dumps(desc))
            store = wdir / f"invalid_{name}{STORE_SUFFIX}"
            p = subprocess.run([*cli, "write", str(dpath), str(store)], capture_output=True, text=True,
                               timeout=TIMEOUT, check=False)
            ok = p.returncode != 0 and not store.exists()
            report["reject"][f"{impl}:{name}"] = {
                "ok": ok, "exit": p.returncode, "file_left": store.exists(), "stderr": p.stderr.strip()[-300:]}
            print(f"reject {impl:6s} {name:40s} {'ok' if ok else 'NOT REJECTED'}", flush=True)

    (out / "report.json").write_text(json.dumps(report, indent=1, default=str))
    vzip_run._summary(report, impls, out)
    return _verdict(report, Path(args.known_gaps) if args.known_gaps else None, out)


def failing(report: dict) -> set[str]:
    """The ids of the cases that failed: `read:ref:basic`, `cross:ref->js:basic`, `write:js:basic`..."""
    ids = {f"{section}:{k}" for section in ("read", "cross") for k, v in report[section].items() if v["failures"]}
    ids |= {f"write:{k}" for k, v in report["write"].items() if v["problems"]}
    ids |= {f"reject:{k}" for k, v in report["reject"].items() if not v["ok"]}
    return ids


def _verdict(report: dict, known_path: Path | None, out: Path) -> int:
    """Exit status: 1 for a failure that is not a known gap, or a known gap that no longer fails."""
    ids = failing(report)
    known = json.loads(known_path.read_text()) if known_path else {}
    patterns = dict(known)
    expected = {i for i in ids if any(fnmatch.fnmatch(i, p) for p in patterns)}
    stale = [p for p in patterns if not any(fnmatch.fnmatch(i, p) for i in ids)]
    lines = [f"{len(ids)} failing cases, {len(expected)} of them known gaps ({known_path or 'none given'})"]
    lines += [f"UNEXPECTED {i}" for i in sorted(ids - expected)]
    lines += [f"KNOWN {i}: " + next(why for p, why in patterns.items() if fnmatch.fnmatch(i, p))
              for i in sorted(expected)[:40]]
    lines += [f"STALE known gap {p!r} (nothing it names fails now: remove it)" for p in stale]
    (out / "VERDICT.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 1 if (ids - expected) or stale else 0


if __name__ == "__main__":
    sys.exit(main())
