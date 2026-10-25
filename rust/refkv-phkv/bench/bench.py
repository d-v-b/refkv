"""Benchmarks phkv against vzip on a local file system and over HTTP, counting requests and bytes
and timing lookups. See README.md. Needs the repository's environment (`uv run`, for refkv) and a
built `examples/bench` (`cargo build --release --example bench`).

    uv run python rust/refkv-phkv/bench/bench.py [--out DIR] [--rtt-ms 0 20] [--sizes 1000 10000]
"""
from __future__ import annotations

import argparse
import json
import random
import socket
import statistics
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import override

from refkv.policy import Policy
from refkv.reader import Stats
from refkv.vzip.store import VZipStore

HERE = Path(__file__).resolve().parent
BENCH = HERE.parent / "target" / "release" / "examples" / ("bench.exe" if sys.platform == "win32" else "bench")


def key(i: int) -> str:
    return f"arr/c/{i // 10000}/{(i // 100) % 100}/{i % 100}"


class Server:
    """Serves a directory over HTTP/1.1 with `Range` requests, each after a delay of `rtt` seconds,
    counting the requests and the bytes of their bodies."""

    def __init__(self, root: Path):
        server = self
        self.rtt = 0.0
        self.bandwidth = 0.0  # bytes per second of each connection; 0 for no limit
        self.requests = 0
        self.bytes = 0
        self.lock = threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            wbufsize = 1 << 20  # headers and body leave in one send: two sends stall on Nagle and delayed ACK (40 ms)

            @override
            def setup(self):
                super().setup()
                self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

            @override
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                path = root / self.path.lstrip("/")
                size = path.stat().st_size
                spec = (self.headers.get("Range") or "bytes=0-").removeprefix("bytes=")
                a, _, b = spec.partition("-")
                start, end = ((max(0, size - int(b)), size) if a == ""
                              else (int(a), min(size, int(b) + 1) if b else size))
                with open(path, "rb") as f:
                    f.seek(start)
                    body = f.read(end - start)
                delay = server.rtt + (len(body) / server.bandwidth if server.bandwidth else 0)
                if delay:
                    time.sleep(delay)
                with server.lock:
                    server.requests += 1
                    server.bytes += len(body)
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end - 1}/{size}")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def reset(self):
        with self.lock:
            self.requests = self.bytes = 0

    def totals(self):
        with self.lock:
            return self.requests, self.bytes


def read_python(url: str, workload: str, n: int, reps: int, lookups: int = 200) -> dict:
    """The vzip reader (refkv.vzip.VZipStore), measured as bench.rs measures phkv's."""
    refs = workload == "refs"
    rng = random.Random(1)
    policy = Policy(allow_private_hosts=True)

    def new():
        stats = Stats()
        return VZipStore(url, resolve=False, stats=stats, policy=policy), stats

    def lookup(s, k):
        v = s.raw(k) if refs else s.get(k)
        assert v is not None, k

    first_ms, fr, fb = [], 0, 0
    for _ in range(reps):
        s, st = new()
        k = key(rng.randrange(n))
        t = time.perf_counter()
        lookup(s, k)
        first_ms.append((time.perf_counter() - t) * 1e3)
        fr, fb = st.archive_requests, st.archive_bytes
    s, st = new()
    lookup(s, key(rng.randrange(n)))
    lat, r, b = [], 0, 0
    for _ in range(lookups):
        k = key(rng.randrange(n))
        r0, b0 = st.archive_requests, st.archive_bytes
        t = time.perf_counter()
        lookup(s, k)
        lat.append((time.perf_counter() - t) * 1e3)
        r += st.archive_requests - r0
        b += st.archive_bytes - b0
    mean = sum(lat) / len(lat)
    lat.sort()
    a = rng.randrange(n) // 100
    prefix = f"arr/c/{a // 100}/{a % 100}/"
    list_ms, lr, lb = [], 0, 0
    for _ in range(reps):
        s, st = new()
        t = time.perf_counter()
        keys = s.list(prefix)
        list_ms.append((time.perf_counter() - t) * 1e3)
        assert len(keys) == min(100, n - a * 100), (prefix, len(keys))
        lr, lb = st.archive_requests, st.archive_bytes
    return {
        "open_ms": None, "open_requests": None, "open_bytes": None,
        "first_get_ms": statistics.median(first_ms), "first_get_requests": fr, "first_get_bytes": fb,
        "warm_ms_median": lat[len(lat) // 2], "warm_ms_mean": mean, "warm_ms_p95": lat[len(lat) * 95 // 100],
        "warm_requests_per_get": r / lookups, "warm_bytes_per_get": b / lookups,
        "list_ms": statistics.median(list_ms), "list_requests": lr, "list_bytes": lb,
    }


def read_rust(target: str, workload: str, n: int, reps: int, *limits: int) -> dict:
    """`limits`: the reader's tail, record_read, slots_at_open and slot_block, then the number of
    later lookups, if not the defaults."""
    out = subprocess.run([str(BENCH), "read", target, workload, str(n), str(reps), *map(str, limits)], check=True,
                         capture_output=True, text=True).stdout
    d = json.loads(out)
    # the Rust reader opens and then looks up; the Python one does both in its first lookup
    d["first_get_ms"] += d["open_ms"]
    d["first_get_requests"] += d["open_requests"]
    d["first_get_bytes"] += d["open_bytes"]
    return d


FORMATS = [  # (label, file suffix, reader)
    ("vzip", "vzip", "py"),
    ("vzip, paged", "paged.vzip", "py"),
    ("phkv", "phkv", "rs"),
    ("hybrid, read as vzip", "hybrid", "py"),
    ("hybrid, read as phkv", "hybrid", "rs"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=HERE / "data", help="where the test files and results go")
    ap.add_argument("--sizes", type=int, nargs="+", default=[1_000, 10_000, 100_000, 1_000_000])
    ap.add_argument("--bytes-sizes", type=int, nargs="+", default=[1_000, 10_000, 100_000])
    ap.add_argument("--value-bytes", type=int, default=1024)
    ap.add_argument("--rtt-ms", type=float, nargs="+", default=[0, 20])
    ap.add_argument("--reps", type=int, default=5)
    args = ap.parse_args()
    if not BENCH.exists():
        sys.exit("build the example first: cargo build --release --example bench "
                 f"--manifest-path {HERE.parent}/Cargo.toml")
    server = Server(args.out)
    rows = []
    plan = [("refs", n) for n in args.sizes] + [("bytes", n) for n in args.bytes_sizes]
    for workload, n in plan:
        d = args.out / f"{workload}"
        if not (d / f"{n}.phkv").exists():
            print(f"writing {workload} {n}", flush=True)
            subprocess.run([str(BENCH), "build", str(d), str(n), workload, str(args.value_bytes)], check=True)
        reps = args.reps if n <= 100_000 else 3
        for label, suffix, reader in FORMATS:
            path = d / f"{n}.{suffix}"
            size = path.stat().st_size
            for transport in ["local"] + [f"http {r:g} ms" for r in args.rtt_ms]:
                if transport == "local":
                    target = str(path)
                else:
                    server.rtt = float(transport.split()[1]) / 1e3
                    target = f"http://127.0.0.1:{server.port}/{workload}/{n}.{suffix}"
                server.reset()
                t = time.time()
                res = (read_python if reader == "py" else read_rust)(target, workload, n, reps)
                row = {"workload": workload, "n": n, "format": label, "reader": "python" if reader == "py" else "rust",
                       "transport": transport, "file_bytes": size, "server_requests": server.totals()[0], **res}
                rows.append(row)
                print(f"{workload:5} {n:>8} {label:22} {transport:12} cold {row['first_get_requests']}req "
                      f"{row['first_get_bytes']:>9}B {row['first_get_ms']:9.2f}ms"
                      f" | warm {row['warm_requests_per_get']:.2f}req {row['warm_bytes_per_get']:8.0f}B "
                      f"{row['warm_ms_median']:8.3f}ms"
                      f" | list {row['list_requests']}req {row['list_bytes']:>8}B {row['list_ms']:8.2f}ms  "
                      f"({time.time() - t:.0f}s)", flush=True)
        (args.out / "results.json").write_text(json.dumps(rows, indent=1))


if __name__ == "__main__":
    main()
