"""Checks the browser MRC virtualizer's output against mrcfile.

Each fixtures/mrc/*.mrc is served over local HTTP and virtualized by
js/conformance/virtualize.ts (the browser code, run under Node). The array is read
through the reference reader and zarr-python and must equal mrcfile's data, in its
shape (an image, a stack of images, a volume, or a stack of volumes) and type, and
every header field must be mrcfile's reading of the header (spec/virtualize/mrc.md §5
<https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mrc.md#5-source-metadata>). The
file must be rebuilt byte for byte from the archive's IR mirror (refkv_source, §5.2
<https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mrc.md#52-the-ir-mirror>).
`mrc_reject_*` files must be rejected.

Usage: uv run python js/test/mrc/verify.py [<dir of .mrc files>]
"""

from __future__ import annotations

import base64
import struct
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path

import mrcfile
import numpy as np
import zarr

HERE = Path(__file__).parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "conformance" / "vzip"))
from http_server import Server  # noqa: E402

from refkv.policy import Policy  # noqa: E402
import refkv.zarr  # noqa: E402  (a vzip archive as a zarr store)

sys.path.insert(0, str(HERE.parent))
from ir_mirror import mirror_problems  # noqa: E402

# The sources are served on 127.0.0.1, which spec §6.7
# <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy> rule 3 refuses by
# default.
LOOPBACK_SOURCES = Policy(allow_private_hosts=True)
CANONICAL_NAN = 0x7FC00000


def as_json(v):
    """A header value as spec/conventions.md §6
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#6-source-metadata-as-json> and
    spec/virtualize/mrc.md §5
    <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mrc.md#5-source-metadata> write it,
    from mrcfile's numpy value."""
    if isinstance(v, np.ndarray) and v.ndim == 0:
        return as_json(v[()])
    if isinstance(v, np.void) and v.dtype.names:
        return [as_json(v[name]) for name in v.dtype.names]
    if isinstance(v, np.void):
        return base64.b64encode(bytes(v)).decode()
    if isinstance(v, np.ndarray):
        return [as_json(x) for x in v]
    if isinstance(v, np.floating):
        bits = int(np.asarray(v).astype(v.dtype.newbyteorder("=")).view("=u4"))
        if bits == 1 << 31 or (v != v and bits != CANONICAL_NAN):
            return {"bits": f"{bits:08x}"}
    if isinstance(v, (bytes, np.bytes_)):
        b = bytes(v).split(b"\0", 1)[0]
        try:
            return b.decode("utf-8")
        except UnicodeDecodeError:
            return {"latin1": b.decode("latin-1")}
    v = v.item() if hasattr(v, "item") else v
    if isinstance(v, float):
        return "NaN" if v != v else "Infinity" if v == float("inf") else "-Infinity" if v == float("-inf") else v
    return v


IMOD = 1146047817


def load(path: Path):
    """mrcfile's header and data (its warnings, such as a zero grid size, are not failures).
    mrcfile does not read IMOD's unsigned-byte flag (spec/virtualize/mrc.md §3
    <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mrc.md#3-data-types>): a mode 0
    file with IMOD's stamp and bit 0 of its flags is its bytes as uint8."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with mrcfile.open(path, permissive=False) as f:
            header, data = f.header.copy(), np.array(f.data)
    raw = path.read_bytes()
    order = "<" if header.mode.dtype.byteorder in "<=" else ">"
    stamp, flags = struct.unpack(order + "ii", raw[152:160])
    if int(header.mode) == 0 and stamp == IMOD and flags & 1:
        data = data.view(np.uint8)
    return header, data


def problems_of(path: Path, out: Path) -> list[str]:
    header, want = load(path)
    root = zarr.open_group(refkv.zarr.open(str(out), policy=LOOPBACK_SOURCES), mode="r", zarr_format=3)
    arr = root["0"]
    got = arr[...]
    problems = []
    if got.dtype != want.dtype.newbyteorder("="):
        problems.append(f"dtype {got.dtype} != {want.dtype}")
    if got.shape != want.shape or not np.array_equal(got, want, equal_nan=got.dtype.kind in "fc"):
        problems.append(f"values differ ({got.shape} vs {want.shape})")
    meta = root.attrs["refkv_virtualized"]["mrc"]
    if meta["data_type"] != str(want.dtype.newbyteorder("=")):
        problems.append(f"data_type {meta['data_type']} != {want.dtype}")
    if list(meta["header"]) != list(header.dtype.names):
        problems.append(f"header fields {list(meta['header'])} != mrcfile's {list(header.dtype.names)}")
    for name in header.dtype.names:
        if meta["header"].get(name) != as_json(header[name]):
            problems.append(f"header {name} {meta['header'].get(name)!r} != mrcfile's {as_json(header[name])!r}")
    kind = {2: "image", 4: "volume_stack"}.get(want.ndim, "image_stack" if header.ispg == 0 else "volume")
    if meta["kind"] != kind:
        problems.append(f"kind {meta['kind']} != {kind}")
    return problems + mirror_problems(out, path)


def main(fixtures: Path) -> int:
    server = Server(fixtures)
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        for path in sorted(fixtures.glob("*.mrc")):
            out = Path(tmp) / f"{path.stem}.vzip"
            p = subprocess.run(["node", str(ROOT / "js" / "conformance" / "virtualize.ts"), "--allow-private-hosts",
                                server.base + path.name, str(out)], capture_output=True, text=True)
            if path.stem.startswith("mrc_reject"):
                ok = p.returncode == 3 and not out.exists()
                failures += not ok
                print(f"{path.name:40s} {'rejected: ' + p.stderr.strip()[-80:] if ok else 'NOT REJECTED ' + p.stderr[-200:]}")
                continue
            if p.returncode:
                failures += 1
                print(f"{path.name:40s} FAILED: {p.stderr.strip()[-300:]}")
                continue
            problems = problems_of(path, out)
            failures += bool(problems)
            print(f"{path.name:40s} {'ok' if not problems else problems}")
    print(f"\n{failures} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "fixtures" / "mrc"))
