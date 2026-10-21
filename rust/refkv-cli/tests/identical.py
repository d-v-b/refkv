"""The Rust CLI (`vzip convert`, rust/refkv-cli) against `python -m refkv.virtualize`: both convert
every input of the fixture corpus, and must give the same exit status and, on success, the same
archive, byte for byte, and the same summary.

The corpus is conformance/virtualize/compare.py's synthetic one: every file under fixtures/
(TIFF, NDPI, ND2, CZI, DICOM, NIfTI, MRC, IMS, SAFE zip, VSI with its tile files), and every store directory under
fixtures/{n5,zarr2,ome-zarr,safe} (the rejected inputs included: both must reject them). Each
input is converted:

- `local`: from its path (a store with `--url`, the URL it would be served from);
- `http`: from a server on 127.0.0.1 (with `--allow-private-hosts`), which serves the files with a
  strong ETag, ranges and multipart/byteranges, and the stores with an S3 ListObjectsV2 listing of
  7 keys a page (so that listings take several pages; conformance/virtualize/proxy.py's); one input
  in four through a redirect, and one in four named by another `--url`;
- `ranges`: the files of `http` again, with REFKV_WHOLE_BELOW=0: the read planner then reads
  them by ranges, several in one request, where it would read a file of the corpus (each under 1
  MiB) whole;
- `checksums`: `local` and `http` again, with `--checksums`;
- `errors`: command lines that fail (a usage error, an input the reader policy refuses, a source
  it would refuse, a missing file or object, an invalid store URL, an empty file), whose exit
  statuses must be the same.

    uv run python rust/refkv-cli/tests/identical.py [--vzip rust/refkv-cli/target/release/vzip]
        [--only <substring>] [--modes local,http,ranges,checksums,errors] [--jobs N]

The summaries are compared without the read planner's counts and model (`planner`), which depend on
the time requests take, for an input read over HTTP. Exits with status 1 if any input differs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "fixtures"
sys.path.insert(0, str(ROOT / "conformance" / "virtualize"))

from proxy import Upstream, make_handler  # noqa: E402

STORE_FORMATS = ("n5", "zarr2", "ome-zarr", "safe")
SUFFIXES = (".tif", ".ndpi", ".nd2", ".dcm", ".nii", ".mrc", ".ims", ".zip", ".czi", ".vsi")
PAGE = 7  # keys a listing page holds


def corpus() -> tuple[list[str], list[str]]:
    """(files, stores), as paths relative to fixtures/ with `/` separators."""
    stores = sorted(d for f in STORE_FORMATS if (FIXTURES / f).is_dir() for d in (FIXTURES / f).iterdir() if d.is_dir())
    files = sorted(p for p in FIXTURES.rglob("*") if p.suffix in SUFFIXES and p.is_file()
                   and not any(p.is_relative_to(s) for s in stores))
    return [p.relative_to(FIXTURES).as_posix() for p in files], [s.relative_to(FIXTURES).as_posix() for s in stores]


def server(cache: Path):
    """A server of the fixtures on 127.0.0.1: proxy.py's (files by single ranges, stores by S3
    listings), and besides: a strong ETag on every file, several ranges in one multipart/byteranges
    answer, and `/r/<path>`, a redirect to `/<path>`."""
    base = make_handler(FIXTURES, Upstream(cache), page_size=PAGE)

    class Handler(base):
        def do_GET(self):
            if self.path.startswith("/r/"):
                return self._send(302, {"Location": self.path[2:]})
            if self._listing():
                return
            t = self._target()
            rng = self.headers.get("Range", "")
            if t is None or not rng.startswith("bytes=") or "," not in rng:
                return super().do_GET()
            size, read = t
            spans = []
            for part in rng[6:].split(","):
                a, _, b = part.strip().partition("-")
                spans.append((int(a), min(size, int(b) + 1)))
            boundary = "refkvboundary"
            body = b""
            for a, b in spans:
                body += (f"--{boundary}\r\nContent-Type: application/octet-stream\r\n"
                         f"Content-Range: bytes {a}-{b - 1}/{size}\r\n\r\n").encode() + read(a, b) + b"\r\n"
            body += f"--{boundary}--\r\n".encode()
            self._send(206, {"Content-Type": f"multipart/byteranges; boundary={boundary}"}, body)

        def _send(self, status, headers, body=b""):
            parts = self.path.split("?")[0].split("/")
            if status in (200, 206) and len(parts) >= 3 and parts[1] == "f" and not self.path.endswith("/"):
                path = FIXTURES / urllib.parse.unquote("/".join(parts[2:]))
                if path.is_file():
                    headers = {**headers, "ETag": '"' + hashlib.sha1(path.read_bytes()).hexdigest()[:16] + '"'}
            super()._send(status, headers, body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/"


def cases(files: list[str], stores: list[str], base: str, modes: set[str]) -> list[tuple[str, list[str], bool, dict]]:
    """(name, arguments but the output, remote, environment) of each conversion."""
    out = []
    for i, f in enumerate(files):
        if "local" in modes:
            out.append((f"local {f}", [f"fixtures/{f}"], False, {}))
        url = base + ("r/f/" if i % 4 == 1 else "f/") + urllib.parse.quote(f)
        extra = ["--url", f"https://data.example/{f}"] if i % 4 == 2 else []
        if "http" in modes:
            out.append((f"http {f}", [url, "--allow-private-hosts", *extra], True, {}))
        if "ranges" in modes:
            out.append((f"ranges {f}", [url, "--allow-private-hosts", *extra], True, {"REFKV_WHOLE_BELOW": "0"}))
    for i, s in enumerate(stores):
        url = f"https://data.example/{urllib.parse.quote(s)}/"
        if "local" in modes:
            out.append((f"local {s}/", [f"fixtures/{s}", "--url", url], False, {}))
        if "http" in modes:
            extra = ["--url", url] if i % 4 == 2 else []
            out.append((f"http {s}/", [base + "f/" + urllib.parse.quote(s) + "/", "--allow-private-hosts", *extra], True, {}))
    if "checksums" in modes:
        out += [(f"checksums {n}", [*args, "--checksums"], remote, env)
                for n, args, remote, env in cases(files, stores, base, {"local", "http"})]
    if "errors" in modes:
        f, s = f"fixtures/{files[0]}", f"fixtures/{stores[0]}"
        out += [(f"errors {n}", args, False, {}) for n, args in [
            ("no output", [NO_OUTPUT, f]),
            ("two inputs", [f, f]),
            ("a flag without its value", [f, "--url"]),
            ("a value that is not a number", [f, "--workers", "many"]),
            ("an unknown flag", [f, "--bogus"]),
            ("a directory without --url", [s]),
            ("a private host", [base + "f/" + urllib.parse.quote(files[0])]),
            ("a private store", [base + "f/" + urllib.parse.quote(stores[0]) + "/"]),
            ("a private source", [f, "--url", "http://127.0.0.1/x.tif"]),
            ("a source that is no URI reference", [f, "--url", "a b.tif"]),
            ("a missing file", ["fixtures/no such file.tif"]),
            ("a missing object", [base + "f/tiff/none.tif", "--allow-private-hosts"]),
            ("a store with nothing", [base + "f/none/", "--allow-private-hosts"]),
            ("a store URL without its slash", [s, "--url", "https://data.example/s"]),
            ("an empty file", [str(EMPTY)]),
        ]]
    return out


# a case without the output argument
NO_OUTPUT = "<no output>"
# an empty file (`main` makes it)
EMPTY = Path(tempfile.gettempdir()) / f"refkv-identical-empty-{os.getpid()}.tif"


def summary(stdout: str, remote: bool):
    """The printed summary, without the planner's counts for an input read over HTTP."""
    try:
        info = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout
    if remote:
        info.pop("planner", None)
    return info


def convert(cmd: list[str], args: list[str], out: Path, env: dict) -> tuple[int, bytes | None, str, str]:
    argv = args[1:] if args[0] == NO_OUTPUT else [args[0], str(out), *args[1:]]
    p = subprocess.run([*cmd, *argv], cwd=ROOT, capture_output=True, text=True, timeout=900,
                       env={**os.environ, **env})
    data = out.read_bytes() if out.exists() else None
    return p.returncode, data, p.stdout, p.stderr


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vzip", default=str(ROOT / "rust" / "refkv-cli" / "target" / "release" / "vzip"))
    ap.add_argument("--only", default="")
    ap.add_argument("--modes", default="local,http,ranges,checksums,errors")
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    opts = ap.parse_args()
    vzip = Path(opts.vzip)
    if os.name == "nt" and not vzip.suffix:
        vzip = vzip.with_suffix(".exe")
    files, stores = corpus()
    EMPTY.write_bytes(b"")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        srv, base = server(tmp / "cache")
        todo = [c for c in cases(files, stores, base, set(opts.modes.split(","))) if opts.only in c[0]]

        def one(k_case):
            k, (name, args, remote, env) = k_case
            py = convert([sys.executable, "-m", "refkv.virtualize"], args, tmp / f"{k}.py.vzip", env)
            rs = convert([str(vzip), "convert"], args, tmp / f"{k}.rs.vzip", env)
            problems = []
            if py[0] != rs[0]:
                problems.append(f"exit {py[0]} (python) != {rs[0]} (rust)\n  python: {py[3].strip()[-400:]}\n  rust: {rs[3].strip()[-400:]}")
            elif py[0] == 0:
                if py[1] != rs[1]:
                    problems.append(f"the archives differ ({len(py[1] or b'')} and {len(rs[1] or b'')} bytes)")
                if summary(py[2], remote) != summary(rs[2], remote):
                    problems.append(f"the summaries differ\n  python: {py[2].strip()}\n  rust: {rs[2].strip()}")
            for p in (tmp / f"{k}.py.vzip", tmp / f"{k}.rs.vzip"):
                p.unlink(missing_ok=True)
            if py[0] not in (0, 3) and not problems:  # both failed alike: shown, not a difference
                print(f"both exit {py[0]}: {name}\n  python: {py[3].strip()[-300:]}\n  rust: {rs[3].strip()[-300:]}", flush=True)
            return name, py[0], problems

        counts: dict[int, int] = {}
        failed = 0
        with ThreadPoolExecutor(opts.jobs) as pool:
            for name, status, problems in pool.map(one, enumerate(todo)):
                counts[status] = counts.get(status, 0) + 1
                if problems:
                    failed += 1
                    print(f"DIFFERS {name}: " + "; ".join(problems), flush=True)
        srv.shutdown()
    EMPTY.unlink()
    print(f"{len(todo)} conversions ({', '.join(f'{n} exited {s}' for s, n in sorted(counts.items()))}); "
          f"{len(todo) - failed} identical, {failed} differ")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
