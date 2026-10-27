"""Checks the browser ND2 zarrifier's output on the synthetic ND2 files.

Each fixtures/nd2/*.nd2 is served over local HTTP and zarrified by
js/conformance/zarrify.ts (the browser code, run under Node). Every chunk
of the archive is read through the reference reader and zarr-python and must
equal the pixels write_fixtures.py wrote (<name>.npz); chunks absent from the
npz (missing frames) must read as the fill value. `nd2_reject_*` files must be
rejected.

Usage: uv run python js/test/nd2/verify.py
"""

from __future__ import annotations

import json
import math
import re
import struct
import sys
import zlib
from pathlib import Path

import nd2 as nd2lib
import numpy as np
import zarr
from nd2._readers import ModernReader

HERE = Path(__file__).parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "conformance" / "vzip"))

import refkv.zarr  # noqa: E402  (a vzip archive as a zarr store)
from refkv._narrow import at, ndarray, sequence, string  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # js/test, for verify_common
from verify_common import LOOPBACK_SOURCES, metadata_v3, run, serve  # noqa: E402

sys.path.insert(0, str(HERE.parent))
from ir_mirror import is_mirror, mirror_problems  # noqa: E402

# the frozen reference twin of the ND2 profile (conformance/zarrify/reference)
sys.path.insert(0, str(ROOT / "conformance" / "zarrify" / "reference"))
from refkv_reference.nd2.lv import decode_lv  # noqa: E402
from refkv_reference.nd2.virtualize import flatten_experiment  # noqa: E402


def lv_norm(v):
    """The nd2 package's decoding of an LV value, as spec/zarrify/nd2.md §5
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nd2.md#5-source-metadata> writes it."""
    if isinstance(v, dict):
        if v and all(re.fullmatch(r"i[0-9]{10}", k) for k in v):
            return [lv_norm(x) for x in v.values()]
        return {k: lv_norm(x) for k, x in v.items()}
    if isinstance(v, (bytes, bytearray)):
        return list(v)
    if isinstance(v, (list, tuple)):
        return [lv_norm(x) for x in v]
    if isinstance(v, float) and not math.isfinite(v):
        return "NaN" if math.isnan(v) else "Infinity" if v > 0 else "-Infinity"
    return v


TAGS = ("utf16", "int", "float")


def ours_norm(v):
    """Our JSON (spec/zarrify/nd2.md §5.1
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nd2.md#51-decoded-chunks>) as the nd2
    package reads it: pairs
    as an object (first position, last value), or as a list when their names are empty;
    an int tag as its integer and a float tag as lv_norm writes the value (-0 as -0.0)."""
    if isinstance(v, dict) and len(v) == 1 and next(iter(v)) in ("int", "float"):
        (tag, x), = v.items()
        return int(x) if tag == "int" else -0.0 if x == "-0" else x
    if isinstance(v, list) and v and all(isinstance(x, list) and len(x) == 2 and isinstance(x[0], str) for x in v):
        if all(n == "" for n, _ in v):
            return [ours_norm(x) for _, x in v]
        out = {}
        for n, x in v:
            out[n] = ours_norm(x)
        return out
    if isinstance(v, list):
        return [ours_norm(x) for x in v]
    if isinstance(v, dict):
        return {k: ours_norm(x) for k, x in v.items()}
    return v


def text_bytes(t) -> bytes:
    """A text value's bytes (spec/conventions.md §6
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#6-source-metadata-as-json>)."""
    return t["latin1"].encode("latin-1") if isinstance(t, dict) else t.encode("utf-8")


def read_text(b: bytes) -> str:
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("latin-1")


def node_path(name: str) -> str:
    return name[:-1].replace("|", "/") if name.endswith("!") else name.replace("|", "/")


def family_member(node, i: int) -> bytes | None:
    """Member `i` of a family of byte values (spec/conventions.md §7
    <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#7-source-values-as-arrays>)."""
    if hasattr(node, "dtype"):
        return bytes(node[i]) if i < node.shape[0] else None
    offsets = node["offsets"][...]
    if i + 1 >= len(offsets):
        return None
    return bytes(node["data"][offsets[i]:offsets[i + 1]]) if offsets[i + 1] > offsets[i] else b""


def modern_reader(f: nd2lib.ND2File) -> ModernReader:
    """`f`'s reader, whose chunk map, loader and decoder the checks read: the modern one (the
    fixtures are not legacy JPEG 2000 files)."""
    r = f._rdr
    if not isinstance(r, ModernReader):
        raise TypeError(f"expected a ModernReader, got {type(r).__name__}")
    return r


def frame_problems(root, loops: list[dict], frames: dict[int, bytes], attrs: dict) -> list[str]:
    """Each placed frame's pixels in the image, and its timestamp in ImageDataSeq,
    against its chunk's data (spec/zarrify/nd2.md §4.3
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nd2.md#43-output>, §5.3
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nd2.md#53-elements>)."""
    h, w, comp = attrs["uiHeight"], attrs["uiWidth"], attrs["uiComp"]
    row, width_bytes = w * comp * attrs["uiBpcInMemory"] // 8, attrs["uiWidthBytes"]
    if not frames:
        return []
    times = root["refkv_source/ImageDataSeq"]
    problems = []
    for f, raw in sorted(frames.items()):
        coords, rest = {}, f
        for loop in reversed(loops):
            c = rest % loop["count"]
            coords[loop["kind"]] = loop["count"] - 1 - c if loop.get("flip") else c
            rest //= loop["count"]
        arr = root[f"{coords.get('p', 0)}/0"]
        axes = arr.metadata.dimension_names
        got = arr[tuple(coords.get(a, 0) if a in ("t", "z") else slice(None) for a in axes)]
        if attrs.get("eCompression", 2) == 0:
            data = zlib.decompress(raw[8:])
        else:
            data = b"".join(raw[8 + r * width_bytes:8 + r * width_bytes + row] for r in range(h))
        want = np.frombuffer(data, dtype=arr.dtype).reshape(h, w, comp)
        want = np.moveaxis(want, -1, 0) if "c" in axes else want[:, :, 0]
        if not np.array_equal(got, want):
            problems.append(f"frame {f}: pixels differ from its chunk")
        grid = tuple(coords[loop["kind"]] for loop in loops) or (0,)
        if struct.pack("<d", times[grid]) != raw[:8]:
            problems.append(f"frame {f}: timestamp differs from its chunk")
    return problems


def mirror_chunk_problems(path: Path, root, out: Path) -> list[str]:
    """An archive of the IR: the root's decoded chunks (spec/zarrify/nd2.md §5.1
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nd2.md#51-decoded-chunks>)
    against the nd2 package's decoder, and the source rebuilt from the mirror byte for
    byte (the frames' pixels are checked against the expected arrays)."""
    problems = mirror_problems(out, path)
    decoded = root.attrs["refkv_zarrified"]["nd2"]["chunks"]
    with nd2lib.ND2File(path) as f:
        r = modern_reader(f)
        for k in r.chunkmap:
            name = read_text(k)
            if name in decoded and (k.endswith(b"LV!") or b"LV|" in k):
                try:
                    theirs = lv_norm(r._decode_chunk(k, strip_prefix=False))
                except UnicodeDecodeError:
                    continue  # nd2 cannot decode unpaired surrogates, which ours keep
                if ours_norm(decoded[name]) != theirs:
                    problems.append(f"{name}: {str(decoded[name])[:60]} vs {str(theirs)[:60]}")
    return problems


def chunk_problems(path: Path, root) -> list[str]:
    """Reconstruction (spec/zarrify/nd2.md §5
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nd2.md#5-source-metadata>): every chunk
    of the map but the placed frames' pixels can be recovered from the hierarchy, against the nd2
    package's chunk map, loader and decoder."""
    meta = root.attrs["refkv_zarrified"]["nd2"]
    src = root.get("refkv_source", None)
    s = src.attrs["refkv_zarrified"]["nd2"] if src is not None and "refkv_zarrified" in src.attrs else {}
    decoded = {**meta["chunks"], **s.get("chunks", {})}

    def names(key: str) -> list:
        """A list of S, or, past the budget, the JSON text in the array it names."""
        v = s.get(key, [])
        return json.loads(bytes(src[v][...])) if isinstance(v, str) else v

    other = {text_bytes(t): i for i, t in enumerate(names("other"))}
    empty = {text_bytes(t) for t in names("empty")}
    attrs = ours_norm(decoded["ImageAttributesLV!"])["SLxImageAttributes"]
    pixels = 8 + attrs["uiHeight"] * attrs["uiWidthBytes"]
    compressed = attrs.get("eCompression", 2) == 0
    frames = int(np.prod(src["ImageDataSeq"].shape)) if src is not None and "ImageDataSeq" in src else 0
    problems = []
    placed_frames: dict[int, bytes] = {}
    with nd2lib.ND2File(path) as f:
        r = modern_reader(f)
        for k in r.chunkmap:
            name = read_text(k)
            raw = r._load_chunk(k)
            frame = re.fullmatch(rb"ImageDataSeq\|(0|[1-9][0-9]*)!", k)
            placed = frame is not None and int(frame[1]) < frames
            if k in other:
                got = bytes(src[f"other/{other[k]}"][...])
                if got != (raw[pixels:] if placed else raw):
                    problems.append(f"{name}: other bytes differ")
                continue
            if k in empty:
                if raw:
                    problems.append(f"{name}: listed as empty")
                continue
            if placed:
                placed_frames[int(frame[1])] = raw
                if (not compressed and len(raw) > pixels
                        and family_member(src["ImageDataSeq.trailing"], int(frame[1])) != raw[pixels:]):
                    problems.append(f"{name}: trailing bytes differ")
                continue
            if frame is not None:
                if family_member(src["ImageDataSeq.beyond"], int(frame[1]) - frames) != raw:
                    problems.append(f"{name}: frame beyond the image differs")
                continue
            if name in decoded:
                if k.endswith(b"LV!") or b"LV|" in k:
                    try:
                        theirs = lv_norm(r._decode_chunk(k, strip_prefix=False))
                    except UnicodeDecodeError:
                        continue  # nd2 cannot decode unpaired surrogates, which ours keep
                    if ours_norm(decoded[name]) != theirs:
                        problems.append(f"{name}: {str(decoded[name])[:60]} vs {str(theirs)[:60]}")
                continue
            m = re.fullmatch(r"CustomDataSeq\|(.+)\|([0-9]+)!", name, re.DOTALL)
            p = f"CustomDataSeq/{node_path(m[1])}" if m else node_path(name)
            if src is None or p not in src:
                problems.append(f"{name}: not in the hierarchy")
                continue
            a = src[p]
            if m:
                got = family_member(a, int(m[2]))
            elif a.dtype == np.uint8:
                got = bytes(a[...])
            else:  # a stream: its values, in the file's frame order, then its rest
                values = a[...].reshape(-1)
                theirs = np.frombuffer(raw, dtype=a.dtype, count=values.size)
                if sorted(theirs.tolist()) != sorted(values.tolist()):
                    problems.append(f"{name}: stream values differ")
                rest = raw[values.size * values.itemsize:]
                if rest and (f"{p}.rest" not in src or bytes(src[f"{p}.rest"][...]) != rest):
                    problems.append(f"{name}: stream rest differs")
                continue
            if got != raw:
                problems.append(f"{name}: bytes differ")
        exp = r._load_chunk(b"ImageMetadataLV!") if b"ImageMetadataLV!" in r.chunkmap else None
    loops = flatten_experiment(decode_lv(exp).get("SLxExperiment") if exp is not None else None)
    return problems + frame_problems(root, loops, placed_frames, attrs)


def check(nd2: Path, out: Path, _) -> tuple[list, str]:
    """A zarrified file's problems: its chunks' metadata, then every chunk against the values the generator
    wrote; and its chunk count and axes."""
    root = zarr.open_group(refkv.zarr.open(str(out), policy=LOOPBACK_SOURCES), mode="r", zarr_format=3)
    meta = mirror_chunk_problems(nd2, root, out) if is_mirror(out) else chunk_problems(nd2, root)
    expected = {k.replace("|", "/"): v for k, v in np.load(nd2.with_suffix(".npz")).items()}
    root = zarr.open_group(refkv.zarr.open(str(out), policy=LOOPBACK_SOURCES), mode="r", zarr_format=3)
    problems, chunks = [], 0
    series_paths = [string(x) for x in sequence(at(root["OME"].attrs, "ome", "series"))]
    for series in series_paths:
        arr = root.get_array(f"{series}/0")
        grid = [s // c for s, c in zip(arr.shape, arr.chunks, strict=True)]
        # Every chunk, or for a large grid the chunks with data and the first ones.
        indices = list(np.ndindex(*grid)) if math.prod(grid) <= 100_000 else sorted(
            {tuple(map(int, k.split("/")[3:])) for k in expected if k.startswith(f"{series}/")}
            | {i for i, _ in zip(np.ndindex(*grid), range(10), strict=False)})
        for index in indices:
            key = f"{series}/0/c/" + "/".join(map(str, index))
            sel = tuple(slice(i * c, (i + 1) * c) for i, c in zip(index, arr.chunks, strict=True))
            got = np.squeeze(ndarray(arr[sel]),
                             axis=tuple(d for d, c in enumerate(arr.chunks) if c == 1 and d < arr.ndim - 2))
            want = expected.get(key)
            chunks += 1
            if want is None:
                if np.any(got != 0):
                    problems.append(f"{key}: expected the fill value")
            elif got.dtype != want.dtype or not np.array_equal(got, want):
                problems.append(f"{key}: differs ({got.dtype} {got.shape} vs {want.dtype} {want.shape})")
    axes = metadata_v3(root.get_array(series_paths[0] + "/0")).dimension_names
    return ([f"CHUNKS: {x}" for x in meta[:3]] + problems[:3],
            f"({chunks} chunks, {len(expected)} with data, axes {axes})")


def main() -> int:
    paths = sorted((ROOT / "fixtures" / "nd2").glob("nd2_*.nd2"))
    return run(paths, check, url=serve(paths), width=34, failure_tail=1500)


if __name__ == "__main__":
    sys.exit(main())
