"""Checks the browser NIfTI virtualizer's output against nibabel.

Each fixtures/nifti/*.nii is served over local HTTP and virtualized
by js/conformance/virtualize.ts (the browser code, run under Node). The
array is read through the reference reader and zarr-python and must equal
nibabel's unscaled data (`img.dataobj.get_unscaled()`), with nibabel's
(x, y, z, t, dimension 5) axes put in the output's order (t, c, z, y, x), and
a color type's samples as the channel axis. A nontrivial scaling must be
recorded as nibabel reads it, and every header field as nibabel reads the
header. The file must be rebuilt byte for byte from the archive's IR mirror
(vzip_source, spec/virtualize/nifti.md §5.2
<https://github.com/d-v-b/vzip/blob/main/spec/virtualize/nifti.md#52-the-ir-mirror>). The
extension chain is parsed here, independently of nibabel (which fails on some
chains) and of the virtualizers: the extender and each extension the view shows
must be the file's (§5.3
<https://github.com/d-v-b/vzip/blob/main/spec/virtualize/nifti.md#53-elements>), and the
chain also matches nibabel's where nibabel reads it. `nifti_reject_*` files must be rejected.

Usage: uv run python js/test/nifti/verify.py [<dir of .nii files>]
"""

from __future__ import annotations

import base64
import io
import json
import math
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import nibabel as nib
import numpy as np
import zarr

HERE = Path(__file__).parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "conformance" / "archive"))
from http_server import Server  # noqa: E402

from vzip.policy import Policy  # noqa: E402
import vzip.zarr  # noqa: E402  (a vzip archive as a zarr store)

sys.path.insert(0, str(HERE.parent))
from ir_mirror import mirror_problems  # noqa: E402

# The sources are served on 127.0.0.1, which spec §6.7
# <https://github.com/d-v-b/vzip/blob/main/spec/archive.md#67-reader-policy> rule 3 refuses by
# default.
LOOPBACK_SOURCES = Policy(allow_private_hosts=True)


def expected(img) -> tuple[np.ndarray, list[str]]:
    """nibabel's raw voxels as a (t, c, z, y, x) array, and the axes present."""
    n = int(img.header["dim"][0])
    data = np.asarray(img.dataobj.get_unscaled())
    color = data.dtype.names is not None
    if color:
        data = np.stack([data[name] for name in data.dtype.names], axis=-1)
    else:
        data = data[..., np.newaxis]  # the samples
    sizes = list(data.shape[:-1]) + [1] * (7 - data.ndim + 1)
    data = data.reshape(sizes[:5] + [data.shape[-1]])  # x, y, z, t, k, samples
    # Color samples are the channel axis; dimension 5 is otherwise.
    data = data[..., 0, :] if color else data[..., 0]
    axes = (["t"] if n >= 4 else []) + (["c"] if n >= 5 or color else []) + (["z"] if n >= 3 else []) + ["y", "x"]
    full = data.transpose(3, 4, 2, 1, 0)  # t, c, z, y, x
    drop = tuple(i for i, a in enumerate("tczyx") if a not in axes)
    return full.squeeze(axis=drop), axes


def raw_header(data: bytes):
    """nibabel's reading of the header alone, without the extensions."""
    klass = nib.Nifti2Header if data[4:7] == b"n+2" else nib.Nifti1Header
    length = 540 if klass is nib.Nifti2Header else 348
    return klass.from_fileobj(io.BytesIO(data[:length] + bytes(4)), check=False)


def load(nii: Path, tmp: str):
    """nibabel's image. nibabel refuses a valid scl_slope with a non-finite
    scl_inter, which the profile (like nifti1_io) takes as 0, and fails on some
    extension chains (an esize below 8) that the profile reads up to where they
    stop; such a file is loaded from a copy with scl_inter set to 0 and the
    extender's first byte to 0."""
    try:
        return nib.load(nii)
    except nib.spatialimages.HeaderDataError as e:
        if "invalid intercept" not in str(e) and "extension" not in str(e):
            raise
        return load_copy(nii, tmp)
    except ValueError as e:  # nibabel's error for an esize below 8
        if "read length" not in str(e):
            raise
    return load_copy(nii, tmp)


def load_copy(nii: Path, tmp: str):
    data = nii.read_bytes()
    hdr = raw_header(data)
    if not (math.isfinite(float(hdr["scl_inter"]))):
        hdr["scl_inter"] = 0
    copy = Path(tmp) / nii.name
    block = hdr.binaryblock
    copy.write_bytes(block + b"\0" + data[len(block) + 1:])
    return nib.load(copy)


def region(data: bytes, raw) -> tuple[bytes, list[tuple[int, bytes]], int, int]:
    """The extender, the extension chain (ecode, data), where the chain ends, and
    the voxel offset, read from the file (spec/virtualize/nifti.md §5
    <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/nifti.md#5-source-metadata>)."""
    length = raw.structarr.itemsize
    order = raw.endianness
    v = int(raw.structarr["vox_offset"])
    extender, exts, q = data[length:length + 4], [], length + 4
    if extender[0]:
        while q + 8 <= v:
            esize, ecode = struct.unpack_from(order + "ii", data, q)
            if esize < 8 or q + esize > v:
                break
            exts.append((ecode, data[q + 8:q + esize]))
            q += esize
    return extender, exts, q, v


CANONICAL_NAN = {4: 0x7FC00000, 8: 0x7FF8000000000000}


def as_json(v):
    """A header value as spec/conventions.md §6
    <https://github.com/d-v-b/vzip/blob/main/spec/conventions.md#6-source-metadata-as-json> and
    spec/virtualize/nifti.md §5
    <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/nifti.md#5-source-metadata> write
    it, from nibabel's numpy value: a negative zero, or a NaN other than the canonical
    one, as {"bits": hex}."""
    if isinstance(v, np.ndarray) and v.dtype.kind != "S":
        return [as_json(x) for x in v]
    if isinstance(v, np.floating):
        size = v.dtype.itemsize
        bits = int(np.asarray(v).astype(v.dtype.newbyteorder("=")).view(f"=u{size}"))
        if bits == 1 << (8 * size - 1) or (v != v and bits != CANONICAL_NAN[size]):
            return {"bits": f"{bits:0{2 * size}x}"}
    if isinstance(v, (bytes, np.bytes_)):
        b = bytes(v).split(b"\0", 1)[0]
        try:
            return b.decode("utf-8")
        except UnicodeDecodeError:
            return {"latin1": b.decode("latin-1")}
    v = v.item() if hasattr(v, "item") else v
    if isinstance(v, float):
        return "NaN" if v != v else "Infinity" if v == float("inf") else "-Infinity" if v == float("-inf") else v
    return v if abs(v) <= 2**53 - 1 else str(v)


def header_problems(nii: Path, meta: dict, out: Path) -> list[str]:
    """Every recorded header field against nibabel's reading of the raw header; the file
    rebuilt from the mirror; and the extender and extensions the view shows against the
    file's chain, and nibabel's."""
    data = nii.read_bytes()
    two = data[4:7] == b"n+2"
    raw = raw_header(data)
    want = {}
    for name in raw.structarr.dtype.names:
        if name == "eol_check":  # nibabel splits NIfTI-2's char magic[8]; the text stops at its NUL
            continue
        want[name] = as_json(raw.structarr[name][()] if raw.structarr[name].shape == () else raw.structarr[name])
    problems = []
    if list(meta["header"]) != list(want):
        problems.append(f"header fields {list(meta['header'])} != nibabel's {list(want)}")
    for name, value in want.items():
        if meta["header"].get(name) != value:
            problems.append(f"header {name} {meta['header'].get(name)!r} != nibabel's {value!r}")
    if meta["nifti_version"] != (2 if two else 1) or meta["byte_order"] != ("little" if raw.endianness == "<" else "big"):
        problems.append(f"version or byte order {meta['nifti_version']}, {meta['byte_order']}")
    problems += mirror_problems(out, nii)

    with zipfile.ZipFile(out) as z:
        view = json.loads(z.read("vzip_source/tree/zarr.json"))["attributes"]["vzip_virtualized"]["nifti"]
    extender, chain, _, _ = region(data, raw)
    if view.get("extender") != list(extender):
        problems.append(f"extender {view.get('extender')} != {list(extender)}")
    shown = {k: v for k, v in view.items() if k.startswith("extensions/")}
    expected = {}
    if extender[0]:
        for i, (code, d) in enumerate(chain):
            expected[f"extensions/{i}"] = (code, len(d) + 8, d)
    # the view's budget may stop the document ($partial) or spill a value ({"$vz": "element"})
    if not set(shown) <= set(expected) or (set(shown) != set(expected) and not view.get("$partial")):
        problems.append(f"extensions shown {sorted(shown)[:5]} != the chain's {sorted(expected)[:5]}")
    for k, (code, esize, d) in expected.items():
        v = shown.get(k)
        if v is None or (v.get("$vz") == "element" and esize > 1024):
            continue  # over the view's budget, or a value the view does not show (over 1024 bytes)
        got = shown_bytes(v.get("data")) if d else b""  # text without the NULs that pad it
        if (v.get("ecode"), v.get("esize")) != (code, esize) or d[:len(got)] != got or d[len(got):].strip(b"\0"):
            problems.append(f"{k}: {v} is not ({code}, {esize}, {d[:16]!r})")
    if extender[0]:
        try:  # nibabel's reading, where it reads the chain
            full = type(raw).from_fileobj(io.BytesIO(data), check=False)
            nib_exts = [(e.get_code(), e._raw.rstrip(b"\0")) for e in full.extensions]
            if [(c, d.rstrip(b"\0")) for c, d in chain] != nib_exts:  # nibabel drops trailing NULs
                problems.append(f"the chain {[(c, len(d)) for c, d in chain]} != nibabel's {[(c, len(r)) for c, r in nib_exts]}")
        except (ValueError, nib.spatialimages.HeaderDataError):
            pass  # nibabel cannot read the chain
    return problems


def shown_bytes(v) -> bytes:
    """The bytes of a value the view shows ({"$vz": "bytes"}, text, or {text, after})."""
    if isinstance(v, str):
        return v.encode("utf-8")
    if isinstance(v, dict) and v.get("$vz") == "bytes":
        return base64.b64decode(v["b64"])
    if isinstance(v, dict) and "latin1" in v:
        return v["latin1"].encode("latin-1")
    if isinstance(v, dict) and "text" in v:
        return shown_bytes(v["text"]) + shown_bytes(v["after"])
    raise ValueError(f"not a shown value: {v!r}")


def main(fixtures: Path) -> int:
    server = Server(fixtures)
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        for nii in sorted(fixtures.glob("*.nii")):
            out = Path(tmp) / f"{nii.stem}.vzip"
            p = subprocess.run(["node", str(ROOT / "js" / "conformance" / "virtualize.ts"), "--allow-private-hosts",
                                server.base + nii.name, str(out)], capture_output=True, text=True)
            if nii.stem.startswith("nifti_reject"):
                ok = p.returncode == 3 and not out.exists()
                failures += not ok
                print(f"{nii.name:44s} {'rejected: ' + p.stderr.strip()[-80:] if ok else 'NOT REJECTED ' + p.stderr[-200:]}")
                continue
            if p.returncode:
                failures += 1
                print(f"{nii.name:44s} FAILED: {p.stderr.strip()[-300:]}")
                continue
            img = load(nii, tmp)
            want, axes = expected(img)
            root = zarr.open_group(vzip.zarr.open(str(out), policy=LOOPBACK_SOURCES), mode="r", zarr_format=3)
            arr = root["0"]
            got = arr[...]
            problems = []
            if list(arr.metadata.dimension_names) != axes:
                problems.append(f"axes {arr.metadata.dimension_names} != {axes}")
            if got.dtype != want.dtype.newbyteorder("="):
                problems.append(f"dtype {got.dtype} != {want.dtype}")
            if got.shape != want.shape or not np.array_equal(got, want, equal_nan=got.dtype.kind == "f"):
                problems.append(f"voxels differ ({got.shape} vs {want.shape})")
            meta = root.attrs["vzip_virtualized"]["nifti"]  # spec/virtualize/nifti.md §5 <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/nifti.md#5-source-metadata>
            slope, inter = img.dataobj.slope, img.dataobj.inter  # the header's are reset on load
            recorded = meta.get("scaling")
            # nibabel keeps a color type's scl_slope but cannot apply it; NIfTI does not scale color.
            if img.get_data_dtype().names is None and (slope, inter) != (1, 0):
                if recorded != {"slope": slope, "inter": inter}:
                    problems.append(f"scaling {recorded} != nibabel's {(slope, inter)}")
            elif recorded is not None:
                problems.append(f"scaling {recorded} recorded, nibabel has none")
            problems += header_problems(nii, meta, out)
            failures += bool(problems)
            print(f"{nii.name:44s} {'ok' if not problems else problems} ({arr.nchunks} chunks, axes {axes})")
    print(f"\n{failures} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "fixtures" / "nifti"))
