"""Requests and bytes that refkv's Rust core and third-party readers spend finding every chunk of a
remote file (design/THIRD_PARTY_READERS.md).

A local HTTP server sits in front of the remote files: it answers `HEAD` and ranged `GET`s (one
range, or several as multipart/byteranges, which the core asks for) from 64 KiB blocks it
fetched once and cached on disk, waits `--rtt-ms` before each answer (a wide-area round trip,
so that sequential and concurrent readers differ as they would over the network), and counts
each request and the bytes of the ranges it answers. Each reader runs against it in turn:

- `core`: `refkv.zarrify.zarrify` (the Rust core, driven by its read planner), every chunk's
  byte range in the archive it builds;
- `tifffile`: `tifffile.TiffFile`, the byte ranges of every series and level from their IFDs,
  each level then written as references with `ZarrTiffStore.write_fsspec` (what kerchunk's TIFF
  reader does);
- `nd2`: `nd2.ND2File`, every frame's offset (its private `_frame_offsets`: nd2 has no public
  API for byte ranges), also opening the URL itself (fsspec with its own 512 KB blocks, or the
  whole file when it is at most 32 MB);
- `czifile`: `czifile.CziFile`, every subblock's data offset and size;
- `h5py` (IMS): `h5py.File`, every chunk of every chunked dataset (`get_chunk_info`).

Each package reads a binary file through each of `TRANSPORTS`: fsspec's HTTP file with no cache,
read-ahead or an LRU block cache, at 64 KiB and 1 MiB blocks, and obstore's buffered reader at
the same sizes. Usage: `uv run python design/experiments/third_party/measure.py [--rtt-ms 20]
[--only tiff]`; prints one Markdown table row per (file, reader). Run it twice and keep the
second run's times: on a cold cache, the first reader of each file also waits for the upstream
server (requests and bytes are the same either way).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import sys
import threading
import time
import urllib.request
from collections.abc import Buffer, Callable
from contextlib import AbstractContextManager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import IO, cast, override

import czifile
import fsspec
import h5py
import nd2
import obstore
import tifffile
from obstore.store import HTTPStore

from refkv.policy import Policy
from refkv.zarrify import zarrify

BLOCK = 1 << 16
SEP = "refkv-measure-boundary"
CACHE = Path(__file__).parent / ".cache"

FILES = {
    "ome-tiff": ("tiff", ("https://ftp.ebi.ac.uk/pub/databases/IDR/idr0096-tratwal-marrowquant/"
                          "20210609-ftp-ome-tiffs/4000_d11_m5_LT_2%20(20x_01).ome.tiff")),
    "svs": ("tiff", "https://zenodo.org/api/records/7189465/files/TCGA-CM-4752.svs/content"),
    "ndpi": ("tiff", "https://zenodo.org/api/records/18302140/files/ki67_lymphoma.ndpi/content"),
    "qptiff": ("tiff", "https://zenodo.org/api/records/16569043/files/2025-07-15_13_13_34.qptiff/content"),
    "nd2-a1": ("nd2", ("https://ftp.ebi.ac.uk/biostudies/fire/S-BIAD/077/S-BIAD2077/Files/"
                       "373_230614_A1_Blk_Reg2_40x.nd2")),
    "nd2-pollen": ("nd2", "https://zenodo.org/api/records/15493140/files/169_Cropped.nd2/content"),
    "czi-axioscan": ("czi", ("https://downloads.openmicroscopy.org/images/Zeiss-CZI/zenodo-10577186/"
                             "2023_11_30__RecognizedCode-27.czi")),
    "czi-idr0011": ("czi", ("https://downloads.openmicroscopy.org/images/Zeiss-CZI/idr0011/"
                            "Plate1-Blue-A_TS-Stinger/Plate1-Blue-A-02-Scene-1-P2-E1-01.czi")),
    "ims": ("ims", "https://downloads.openmicroscopy.org/images/Imaris-IMS/davemason/Convallaria_3C_10T_confocal.ims"),
}


class Upstream:
    """A remote file, read in cached 64 KiB blocks."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.dir = CACHE / hashlib.sha256(url.encode()).hexdigest()[:16]
        self.dir.mkdir(parents=True, exist_ok=True)
        size_file = self.dir / "size"
        if not size_file.exists():
            req = urllib.request.Request(url, method="HEAD")  # noqa: S310 (an https URL of FILES)
            with urllib.request.urlopen(req) as r:  # noqa: S310
                size_file.write_text(r.headers["Content-Length"])
        self.size = int(size_file.read_text())

    def blocks(self, first: int, last: int) -> bytes:
        """Blocks [first, last], the missing ones fetched in one request per run of them."""
        out = []
        i = first
        while i <= last:
            path = self.dir / str(i)
            if path.exists():
                out.append(path.read_bytes())
                i += 1
                continue
            j = i
            while j < last and not (self.dir / str(j + 1)).exists():
                j += 1
            end = min(self.size, (j + 1) * BLOCK) - 1
            req = urllib.request.Request(self.url, headers={"Range": f"bytes={i * BLOCK}-{end}"})  # noqa: S310
            with urllib.request.urlopen(req) as r:  # noqa: S310
                status, data = r.status, r.read()
            if status != 206 or len(data) != end + 1 - i * BLOCK:  # nothing cached that is not the range
                raise OSError(f"{self.url}: HTTP {status} with {len(data)} bytes for bytes={i * BLOCK}-{end}")
            for k in range(i, j + 1):
                part = data[(k - i) * BLOCK:(k - i + 1) * BLOCK]
                # written whole, then renamed into place: a concurrent request sees the block
                # complete or not at all (two may fetch it; the bytes are the same)
                tmp = self.dir / f"{k}.{threading.get_ident()}.tmp"
                tmp.write_bytes(part)
                tmp.replace(self.dir / str(k))
                out.append(part)
            i = j + 1
        return b"".join(out)

    def read(self, start: int, end: int) -> bytes:
        """Bytes [start, end)."""
        base = start // BLOCK * BLOCK
        return self.blocks(start // BLOCK, (end - 1) // BLOCK)[start - base:end - base]


class Counter:
    def __init__(self) -> None:
        self.requests = 0
        self.bytes = 0
        self.lock = threading.Lock()

    def add(self, n: int) -> None:
        with self.lock:
            self.requests += 1
            self.bytes += n


def serve(upstreams: dict[str, Upstream], counter: Counter, rtt: float) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        @override
        def log_message(self, format: str, *args: object) -> None:
            pass

        def _upstream(self) -> Upstream:
            return upstreams[self.path.split("/")[1]]

        def do_HEAD(self) -> None:
            up = self._upstream()
            time.sleep(rtt)
            counter.add(0)
            self.send_response(200)
            self.send_header("Content-Length", str(up.size))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("ETag", '"fixed"')
            self.end_headers()

        def do_GET(self) -> None:
            up = self._upstream()
            header = self.headers.get("Range", "")
            spans = []
            if header.startswith("bytes="):
                for part in header[6:].split(","):
                    a, _, b = part.strip().partition("-")
                    if not a:  # a suffix range, the last b bytes
                        spans.append((max(0, up.size - int(b)), up.size))
                    else:
                        spans.append((int(a), min(up.size, int(b) + 1) if b else up.size))
            time.sleep(rtt)
            if any(a >= b for a, b in spans):  # unsatisfiable: past the end, or empty
                counter.add(0)
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{up.size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if len(spans) <= 1:
                start, end = spans[0] if spans else (0, up.size)
                data = up.read(start, end)
                counter.add(len(data))
                self.send_response(206 if spans else 200)
                if spans:
                    self.send_header("Content-Range", f"bytes {start}-{end - 1}/{up.size}")
            else:  # several ranges: multipart/byteranges, as the core asks for when it batches
                parts = []
                for a, b in spans:
                    parts.append(f"--{SEP}\r\nContent-Type: application/octet-stream\r\n"
                                 f"Content-Range: bytes {a}-{b - 1}/{up.size}\r\n\r\n".encode())
                    parts.append(up.read(a, b) + b"\r\n")
                parts.append(f"--{SEP}--\r\n".encode())
                data = b"".join(parts)
                counter.add(sum(b - a for a, b in spans))
                self.send_response(206)
                self.send_header("Content-Type", f"multipart/byteranges; boundary={SEP}")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("ETag", '"fixed"')
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def run_core(url: str) -> str:
    store = zarrify(url, policy=Policy(allow_private_hosts=True))
    refs = [k for k, v in store.mapping.items() if not isinstance(v, bytes)]
    return str(sum(1 for k in refs if not k.startswith("refkv_source/")))


class ObstoreFile(io.RawIOBase):
    """obstore's buffered reader (`obstore.open_reader`) as a Python binary file, with the
    `readinto` h5py calls; it reads nothing of its own."""

    def __init__(self, url: str, buffer_size: int) -> None:
        base, _, path = url.rpartition("/")
        store = HTTPStore.from_url(base + "/", client_options={"allow_http": True})
        self.f = obstore.open_reader(store, path, buffer_size=buffer_size)

    @override
    def readable(self) -> bool:
        return True

    @override
    def seekable(self) -> bool:
        return True

    @override
    def seek(self, offset: int, whence: int = 0) -> int:
        return self.f.seek(offset, whence)

    @override
    def tell(self) -> int:
        return self.f.tell()

    @override
    def read(self, size: int = -1) -> bytes:
        return bytes(self.f.read(None if size < 0 else size))

    @override
    def readinto(self, buffer: Buffer) -> int:
        view = memoryview(buffer).cast("B")
        data = self.read(len(view))
        view[:len(data)] = data
        return len(data)


type File = IO[bytes]
"""A binary file a package reads: fsspec's, or obstore's (`ObstoreFile`, which has its methods)."""

type Opener = Callable[[str], AbstractContextManager[File]]
"""Opens a URL as a binary file: one of the transports a package reads through."""


def fsspec_file(cache_type: str, block_size: int) -> Opener:
    return lambda url: fsspec.filesystem("http").open(url, "rb", cache_type=cache_type, block_size=block_size)


def obstore_file(buffer_size: int) -> Opener:
    return lambda url: cast("AbstractContextManager[File]", ObstoreFile(url, buffer_size))


# The transports each package reads through: fsspec's HTTP file with each of its caches for
# random access (`none`: every read a request; `readahead`: one block after the last read;
# `blockcache`: an LRU of blocks, which keeps blocks a reader seeks back to), at 64 KiB and
# 1 MiB blocks, and obstore's buffered reader, whose buffer is dropped on every seek.
TRANSPORTS: list[tuple[str, Opener]] = [
    ("fsspec none", fsspec_file("none", BLOCK)),
    ("fsspec readahead 64 KiB", fsspec_file("readahead", BLOCK)),
    ("fsspec readahead 1 MiB", fsspec_file("readahead", 1 << 20)),
    ("fsspec blockcache 64 KiB", fsspec_file("blockcache", BLOCK)),
    ("fsspec blockcache 1 MiB", fsspec_file("blockcache", 1 << 20)),
    ("obstore 64 KiB", obstore_file(BLOCK)),
    ("obstore 1 MiB", obstore_file(1 << 20)),
]


def run_tifffile(fh: File, url: str) -> str:
    """The byte ranges of every chunk of every series and level, from their pages' (IFDs')
    offsets and byte counts; then each level written as references by `write_fsspec`, as kerchunk
    does, which some levels fail (counted)."""
    n = failed = 0
    with tifffile.TiffFile(fh) as tif:
        for series in tif.series:
            for level in series.levels:
                n += sum(len(page.dataoffsets) for page in level.pages if page is not None)
                try:
                    with level.aszarr() as z:
                        z.write_fsspec(io.StringIO(), url)
                except ValueError:
                    failed += 1
    return f"{n}" + (f" ({failed} levels fail write_fsspec)" if failed else "")


def run_nd2(fh: File | str, _url: str) -> str:
    """Every frame's offset (given a URL, nd2 opens it with fsspec itself: 512 KB blocks, or the
    whole file when it is at most 32 MB)."""
    with nd2.ND2File(fh) as f:  # pyrefly: ignore[bad-argument-type] (a file it reads and seeks)
        # the modern reader's; the legacy (JPEG 2000) reader has none
        return str(len(f._rdr._frame_offsets))  # pyrefly: ignore[missing-attribute]


def run_czifile(fh: File, _url: str) -> str:
    """Every subblock's data offset and size, which czifile reads from each subblock's header."""
    with czifile.CziFile(fh) as czi:
        return str(sum(1 for sb in czi.subblocks() if sb.data_size >= 0))


def run_h5py(fh: File, _url: str) -> str:
    """Every chunk's byte offset and size (`get_chunk_info`) of every chunked dataset."""
    datasets: list[h5py.Dataset] = []
    with h5py.File(fh, "r") as f:
        f.visititems(lambda _name, obj: datasets.append(obj) if isinstance(obj, h5py.Dataset) else None)
        n = 0
        for ds in datasets:
            if ds.chunks is None:
                continue
            for i in range(ds.id.get_num_chunks()):
                ds.id.get_chunk_info(i)
                n += 1
        return str(n)


PACKAGES: dict[str, tuple[str, Callable[[File, str], str]]] = {
    "tiff": ("tifffile", run_tifffile),
    "nd2": ("nd2", run_nd2),
    "czi": ("czifile", run_czifile),
    "ims": ("h5py", run_h5py),
}


def readers(kind: str) -> list[tuple[str, Callable[[str], str]]]:
    """The core, then the format's package through each transport (nd2 also opening the URL
    itself)."""
    package, run = PACKAGES[kind]

    def through(opener: Opener) -> Callable[[str], str]:
        def go(url: str) -> str:
            with opener(url) as fh:
                return run(fh, url)
        return go

    out: list[tuple[str, Callable[[str], str]]] = [("core", run_core)]
    if kind == "nd2":
        out.append(("nd2, its own fsspec file", lambda u: run_nd2(u, u)))
    out += [(f"{package}, {label}", through(opener)) for label, opener in TRANSPORTS]
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--rtt-ms", type=float, default=20)
    p.add_argument("--only", nargs="*")
    args = p.parse_args()
    names = [n for n in FILES if not args.only or n in args.only or FILES[n][0] in args.only]
    upstreams = {n: Upstream(FILES[n][1]) for n in names}
    counter = Counter()
    server = serve(upstreams, counter, args.rtt_ms / 1000)
    base = f"http://127.0.0.1:{server.server_port}"
    print(f"| file | size | reader | requests | bytes read | wall time at {args.rtt_ms:g} ms RTT | chunks |")
    print("|---|---:|---|---:|---:|---:|---:|")
    for name in names:
        kind = FILES[name][0]
        url = f"{base}/{name}/file"
        for label, run in readers(kind):
            counter.requests = counter.bytes = 0
            t = time.perf_counter()
            try:
                chunks = run(url)
            except Exception as e:  # noqa: BLE001 (reported in the table)
                chunks = f"failed: {type(e).__name__}: {str(e)[:80]}"
            dt = time.perf_counter() - t
            print(f"| {name} | {upstreams[name].size / 1e6:.0f} MB | {label} | {counter.requests} | "
                  f"{counter.bytes / 1e3:,.0f} kB | {dt:.1f} s | {chunks} |", flush=True)
    server.shutdown()


if __name__ == "__main__":
    sys.exit(main())
