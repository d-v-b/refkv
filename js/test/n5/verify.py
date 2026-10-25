"""Checks the browser N5 zarrifier's output against an independent N5 reader.

Each store in fixtures/n5/ is served by the harness proxy (with its
S3 listing) and zarrified by js/conformance/zarrify.ts (the browser
code, run under Node). Every array of the archive is read through refkv.zarr
and zarr-python (with refkv.zarr.codecs' n5_default codec) and must equal:

- the dataset as read by `read_n5` below, a block reader written from the N5
  specification alone (header, column-major big-endian elements, each
  compression), which places each block, truncated or padded, into an array
  of the dataset's dimensions (missing and empty blocks read as 0); and
- the dataset as zarr-n5 reads it (an independent n5_default implementation),
  where zarr-n5 supports it.

`n5_reject_*` stores must be rejected; reading `n5_edge_varlength_block`'s
array must fail (its varlength block is refused by the codec). The archive's
`refkv_source` is the store's IR mirror (spec/zarrify/n5.md §5.1
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/n5.md#51-the-ir-mirror>): rebuilt from
the archive alone, its listing must be the store's (every object's key and size, the empty ones
included) and every node's attributes.json must be the store's, byte for byte.

With `--remote <store URL> <array path>`, a public store is zarrified
directly and that array is compared with `read_n5` reading the blocks over
HTTP (the pixel check of conformance/zarrify/corpus_n5.txt).

Usage: uv run python js/test/n5/verify.py [<store dir> ...]
       uv run python js/test/n5/verify.py --remote <store URL> <array path>
"""

from __future__ import annotations

import gzip
import importlib.util
import json
import struct
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
import zlib
from pathlib import Path

import numcodecs
import numpy as np
import zarr
from zarr.storage import LocalStore

HERE = Path(__file__).parent
ROOT = HERE.parents[2]
FIXTURES = ROOT / "fixtures"
sys.path.insert(0, str(ROOT / "conformance" / "zarrify"))
from proxy import Proxy  # noqa: E402

import refkv.zarr  # noqa: E402  (a vzip archive as a zarr store; registers n5_default and zlib)
from refkv import _core  # noqa: E402
from refkv.ir.cmirror import Archive  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # js/test, for verify_common
from verify_common import LOOPBACK_SOURCES, run  # noqa: E402

# The other objects' check is the Zarr v2 verifier's (spec/zarrify/n5.md §6
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/n5.md#6-other-objects> is
# spec/zarrify/zarr2.md §5
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#5-other-objects>).
_spec = importlib.util.spec_from_file_location("zarr2_verify", HERE.parent / "zarr2" / "verify.py")
zarr2_verify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(zarr2_verify)


def decompress(body: bytes, compression: dict) -> bytes:
    t = compression["type"]
    if t == "raw":
        return body
    if t == "gzip":
        return zlib.decompress(body) if compression.get("useZlib") else gzip.decompress(body)
    if t == "zstd":
        return numcodecs.Zstd().decode(body)
    if t == "blosc":
        return numcodecs.Blosc().decode(body)
    raise ValueError(f"compression {t}")


def read_n5(attrs: dict, get) -> np.ndarray:
    """The dataset with attributes `attrs`, whose block at key "i/j/..." is
    `get(key)` (None if missing). Indexed in N5 dimension order."""
    dims, block = attrs["dimensions"], attrs["blockSize"]
    compression = attrs.get("compression") or {"type": attrs["compressionType"]}
    dtype = np.dtype(attrs["dataType"]).newbyteorder(">")
    out = np.zeros(dims, dtype=dtype.newbyteorder("="))
    for idx in np.ndindex(*[-(-d // b) for d, b in zip(dims, block, strict=True)]):
        raw = get("/".join(map(str, idx)))
        if not raw:
            continue
        mode, ndim = struct.unpack_from(">HH", raw)
        if mode != 0:
            raise ValueError(f"block {idx} has mode {mode}")
        shape = struct.unpack_from(f">{ndim}I", raw, 4)
        body = decompress(raw[4 + 4 * ndim :], compression)
        n = int(np.prod(shape))
        # Column-major: the first dimension varies fastest.
        values = np.frombuffer(body, dtype=dtype, count=n).reshape(shape[::-1]).T
        dest = tuple(slice(i * b, min((i + 1) * b, d)) for i, b, d in zip(idx, block, dims, strict=True))
        take = tuple(slice(0, min(s.stop - s.start, m)) for s, m in zip(dest, shape, strict=True))
        dest = tuple(slice(s.start, s.start + t.stop) for s, t in zip(dest, take, strict=True))
        out[dest] = values[take]
    return out


def zarr_n5_read(store_dir: Path, path: str) -> np.ndarray | None:
    """The dataset as zarr-n5 reads it, or None if it cannot."""
    try:
        from zarr_n5 import N5WrapperStore  # noqa: PLC0415 (optional: the check is skipped without it)
    except ImportError:
        return None
    with zarr.config.set({"codecs.n5_default": "zarr_n5.codec.default.N5DefaultCodec"}):
        try:
            a = zarr.open_array(N5WrapperStore(LocalStore(store_dir, read_only=True)), path=path, mode="r",
                                zarr_format=3)
            return a[...]
        except Exception:  # noqa: BLE001  (unsupported by zarr-n5: legacy compressionType, ...)
            return None


def arrays_of(out: Path) -> list[str]:
    """The arrays of the hierarchy (the mirror's, under refkv_source/, are not datasets)."""
    z = zipfile.ZipFile(out)
    paths = []
    for name in z.namelist():
        if name.startswith("refkv_source/"):
            continue
        if name.endswith("zarr.json") and json.loads(z.read(name)).get("node_type") == "array":
            paths.append(name[: -len("zarr.json")].rstrip("/"))
    return sorted(paths)


def zarrify(url: str, out: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["node", str(ROOT / "js" / "conformance" / "zarrify.ts"), "--allow-private-hosts", url,
                           str(out)], capture_output=True, text=True, check=False)


def check_store(store_dir: Path, out: Path) -> list[str]:
    problems = []
    vz = refkv.zarr.open(str(out), policy=LOOPBACK_SOURCES)
    for path in arrays_of(out):
        attrs = json.loads((store_dir / path / "attributes.json").read_text())
        arr = zarr.open_array(vz, path=path, mode="r", zarr_format=3)
        if store_dir.name == "n5_edge_varlength_block":
            try:
                arr[...]
            except Exception:  # noqa: BLE001, S112 - the read must fail, by any error
                continue
            problems.append(f"{path}: the varlength block was read")
            continue
        got = arr[...]

        def get(key, base=store_dir / path):
            f = base / key
            return f.read_bytes() if f.is_file() else None

        want = read_n5(attrs, get)
        if got.dtype != want.dtype or got.shape != want.shape or not np.array_equal(got, want, equal_nan=True):
            problems.append(f"{path}: differs from read_n5 ({got.dtype}{got.shape} vs {want.dtype}{want.shape})")
        other = zarr_n5_read(store_dir, path)
        if other is not None and not np.array_equal(other, want, equal_nan=True):
            problems.append(f"{path}: zarr-n5 differs from read_n5")
    return problems


def mirror_problems(store_dir: Path, out: Path) -> list[str]:
    """The store IR's mirror against the store: its listing is the store's objects and sizes, and
    each object it holds (a node's attributes.json) is the store's, byte for byte."""
    def never(_o: int, _n: int) -> bytes:
        raise AssertionError("a store's mirror reads no source")

    stored = {f.relative_to(store_dir).as_posix(): f for f in store_dir.rglob("*") if f.is_file()}
    rebuilt = _core.rebuild_store(Archive(str(out), never).get)
    problems = []
    if {k: n for k, n, _ in rebuilt} != {k: f.stat().st_size for k, f in stored.items()}:
        problems.append("the mirror's listing is not the store's")
    held = {k for k, _, data in rebuilt if data is not None}
    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
    documents = {k for k, n, _ in rebuilt if (k == "attributes.json" or k.endswith("/attributes.json"))
                 and k.removesuffix("attributes.json") + "zarr.json" in names}
    if held != documents:
        problems.append(f"the mirror holds {sorted(held ^ documents)[:3]} wrongly")
    for k, _, data in rebuilt:
        if data is not None and (k not in stored or stored[k].read_bytes() != data):
            problems.append(f"the mirror's {k} is not the store's")
    return problems


def remote(store_url: str, path: str) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "remote.vzip"
        p = zarrify(store_url, out)
        if p.returncode:
            print(f"FAILED: {p.stderr[-300:]}")
            return 1
        print(p.stdout.strip()[:300])
        base = store_url + path + "/"
        # the store URL given on the command line
        attrs = json.loads(urllib.request.urlopen(base + "attributes.json", timeout=60).read())  # noqa: S310

        def get(key):
            try:
                return urllib.request.urlopen(base + key, timeout=120).read()  # noqa: S310
            except urllib.error.HTTPError as e:
                if e.code in (403, 404):
                    return None
                raise

        want = read_n5(attrs, get)
        got = zarr.open_group(refkv.zarr.open(str(out), policy=LOOPBACK_SOURCES), mode="r", zarr_format=3)[path][...]
        ok = got.shape == want.shape and np.array_equal(got, want)
        print(f"{path}: {got.dtype}{got.shape}, {'equal to read_n5' if ok else 'DIFFERS from read_n5'}, "
              f"{int(np.count_nonzero(want))} nonzero values")
        return 0 if ok else 1


def main(argv: list[str]) -> int:
    if argv[:1] == ["--remote"]:
        return remote(argv[1], argv[2])
    stores = [Path(a) for a in argv] or sorted(d for d in (FIXTURES / "n5").iterdir() if d.is_dir())
    proxy = Proxy(FIXTURES, Path(tempfile.mkdtemp()))

    def check(store_dir: Path, out: Path, _) -> tuple[list, str]:
        # objects_problems checks a store mirror's listing and held bytes too; mirror_problems
        # that it holds exactly the nodes' documents
        objects = zarr2_verify.objects_problems(store_dir, out)
        problems = check_store(store_dir, out) + objects + mirror_problems(store_dir, out)
        return problems[:3], f"({len(arrays_of(out))} arrays)"

    return run(stores, check, url=lambda d: proxy.local_store(d.resolve().relative_to(FIXTURES.resolve()).as_posix()),
               width=40)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
