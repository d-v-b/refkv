"""Virtual shards of an HDF5-and-Zarr array, with the shard index stored three ways, in vzip and phkv
archives, and the archives' sizes (design/NATIVE_SHARD_INDEX.md).

Every shard of https://s3.janelia.org/shroff-lab/s0_hdf5/c/ is an HDF5 file that is also a Zarr v3
shard (sharding_indexed, 64^3 inner chunks, index at the end, crc32c). The file's `zarrindex` dataset
is the shard index; the HDF5 chunk index of its `zarrshard` dataset (a Fixed Array, written with
libver="latest") holds the same 16-byte entries.

A shard's value is `[file bytes 0..end] ++ [index]`, and the index is, per variant:

  inline    the 8192 index bytes and the 4 CRC-32C bytes written into the reference (what
            refkv.shards.write_vzip_sharded(inline_index=True) does)
  internal  the same 8196 bytes in an entry of the archive, which the reference points at
            (vzip: a hidden DEFLATE entry; phkv: an ordinary key, deflated)
  native    no index bytes stored: a range of the HDF5 file's own Fixed Array data block, whose entries
            are the Zarr index's, and the 4 CRC-32C bytes inline

Usage (phkv needs refkv with PR 141, refkv.phkv):

    uv run python design/experiments/native_shard_index/compare.py OUT_DIR [--verify]

It reads the first 16 KiB and the last 8196 bytes of every shard, over HTTP, and writes nine archives
into OUT_DIR. `--verify` decodes four regions through zarr-python from every archive and compares them.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import struct
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from refkv.phkv import PhkvWriter
from refkv.pb import Range
from refkv.policy import crc32c
from refkv.vzip.archive import VZipWriter

BUCKET = "https://s3.janelia.org/shroff-lab"
PREFIX = "s0_hdf5"
HEAD = 16384
UNDEF = 2**64 - 1
INDEX = 8192


def http(url: str, rng: str | None = None, method: str = "GET") -> tuple[bytes, dict[str, str]]:
    req = urllib.request.Request(url, method=method, headers={"Range": rng} if rng else {})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read(), {k.lower(): v for k, v in r.headers.items()}


def jenkins(data: bytes) -> int:
    """The checksum of HDF5 metadata (lookup3 hashlittle, initval 0)."""
    m = 0xFFFFFFFF
    rot = lambda x, k: ((x << k) | (x >> (32 - k))) & m  # noqa: E731

    def mix(a, b, c):
        a = (a - c) & m; a ^= rot(c, 4); c = (c + b) & m
        b = (b - a) & m; b ^= rot(a, 6); a = (a + c) & m
        c = (c - b) & m; c ^= rot(b, 8); b = (b + a) & m
        a = (a - c) & m; a ^= rot(c, 16); c = (c + b) & m
        b = (b - a) & m; b ^= rot(a, 19); a = (a + c) & m
        c = (c - b) & m; c ^= rot(b, 4); b = (b + a) & m
        return a, b, c

    n = len(data)
    a = b = c = (0xDEADBEEF + n) & m
    i = 0
    while n - i > 12:
        a = (a + int.from_bytes(data[i:i + 4], "little")) & m
        b = (b + int.from_bytes(data[i + 4:i + 8], "little")) & m
        c = (c + int.from_bytes(data[i + 8:i + 12], "little")) & m
        a, b, c = mix(a, b, c)
        i += 12
    if n - i == 0:
        return c
    t = data[i:] + b"\0" * 12
    a = (a + int.from_bytes(t[0:4], "little")) & m
    b = (b + int.from_bytes(t[4:8], "little")) & m
    c = (c + int.from_bytes(t[8:12], "little")) & m
    c ^= b; c = (c - rot(b, 14)) & m; a ^= c; a = (a - rot(c, 11)) & m; b ^= a; b = (b - rot(a, 25)) & m
    c ^= b; c = (c - rot(b, 16)) & m; a ^= c; a = (a - rot(c, 4)) & m; b ^= a; b = (b - rot(a, 14)) & m
    c ^= b; c = (c - rot(b, 24)) & m
    return c


@dataclass
class Shard:
    key: str            # c/z/y/x
    url: str
    size: int           # file size
    end: int            # end of the last chunk: where zarrindex starts
    entries_off: int    # file offset of the Fixed Array data block's entries
    entries: bytes      # those 8192 bytes
    zindex: bytes       # the zarrindex dataset: 8192 bytes of entries + CRC-32C
    present: int        # chunks allocated

    @property
    def same_entries(self) -> bool:
        return self.entries == self.zindex[:INDEX]


def parse_fixed_array(head: bytes) -> tuple[int, bytes]:
    """(file offset of the entries, the entries) of the one fixed array chunk index in `head`."""
    i = 0
    while (i := head.find(b"OHDR", i)) != -1:
        v, fl = head[i + 4], head[i + 5]
        o = i + 6
        i += 4
        if v != 2:
            continue
        if fl & 0x20:
            o += 16
        if fl & 0x10:
            o += 4
        n = 1 << (fl & 3)
        end = o + n + int.from_bytes(head[o:o + n], "little")
        o += n
        tr = 2 if fl & 4 else 0
        while o + 4 + tr <= end:
            t, ms = head[o], struct.unpack("<H", head[o + 1:o + 3])[0]
            q = o + 4 + tr
            if t == 8 and head[q] == 4 and head[q + 1] == 2:  # layout v4, chunked
                nd, enc = head[q + 3], head[q + 4]
                r = q + 5 + nd * enc
                if head[r] != 3:
                    raise ValueError(f"chunk index type {head[r]}, not a fixed array")
                fahd = int.from_bytes(head[r + 2:r + 10], "little")
                if head[fahd:fahd + 4] != b"FAHD":
                    raise ValueError("no FAHD")
                _, cid, esz, pb = head[fahd + 4:fahd + 8]
                nent = int.from_bytes(head[fahd + 8:fahd + 16], "little")
                fadb = int.from_bytes(head[fahd + 16:fahd + 24], "little")
                if struct.unpack("<I", head[fahd + 24:fahd + 28])[0] != jenkins(head[fahd:fahd + 24]):
                    raise ValueError("FAHD checksum")
                if (cid, esz, nent) != (1, 16, 512) or nent > 1 << pb:
                    raise ValueError(f"unexpected FA: client {cid} entry {esz} n {nent} page bits {pb}")
                if head[fadb:fadb + 4] != b"FADB":
                    raise ValueError("no FADB")
                off = fadb + 14  # signature, version, client id, header address
                ents = head[off:off + esz * nent]
                if struct.unpack("<I", head[off + len(ents):off + len(ents) + 4])[0] != jenkins(head[fadb:off + len(ents)]):
                    raise ValueError("FADB checksum")
                return off, ents
            o = q + ms
    raise ValueError("no layout message")


def load_shard(key: str) -> Shard:
    url = f"{BUCKET}/{PREFIX}/{key}"
    _, h = http(url, method="HEAD")
    size = int(h["content-length"])
    head, _ = http(url, f"bytes=0-{HEAD - 1}")
    off, ents = parse_fixed_array(head)
    zi, _ = http(url, f"bytes={size - INDEX - 4}-{size - 1}")
    if struct.unpack("<I", zi[INDEX:])[0] != crc32c(zi[:INDEX]):
        raise ValueError(f"{key}: zarrindex crc32c")
    # address (8), compressed size (4), filter mask (4)
    rows = [struct.unpack("<QII", ents[16 * k:16 * k + 16]) for k in range(512)]
    alloc = [(a, s, m) for a, s, m in rows if a != UNDEF]
    if any(m for _, _, m in alloc):
        raise ValueError(f"{key}: a chunk skips a filter; its mask is not Zarr's zero high bytes")
    end = max(a + s for a, s, _ in alloc)
    if end != size - INDEX - 4:
        raise ValueError(f"{key}: the chunks end at {end}, the zarrindex starts at {size - INDEX - 4}")
    s = Shard(key, url, size, end, off, ents, zi, len(alloc))
    for k in range(512):  # the entries agree with Zarr's, but for the size of an empty slot
        e, z = ents[16 * k:16 * k + 16], zi[16 * k:16 * k + 16]
        if e != z and not (e[:8] == z[:8] == b"\xff" * 8):
            raise ValueError(f"{key}: slot {k} differs: {e.hex()} vs {z.hex()}")
    return s


def list_shards() -> list[str]:
    body, _ = http(f"https://s3.janelia.org/shroff-lab/?list-type=2&prefix={PREFIX}/c/&max-keys=1000")
    keys = re.findall(r"<Key>([^<]+)</Key>", body.decode())
    return sorted(k[len(PREFIX) + 1:] for k in keys)


# --- the archives --------------------------------------------------------------------------


def _ranges(w, sh: Shard, variant: str, key: str) -> list[Range]:
    src = w.url(sh.url)
    file_range = Range(source=src, offset=0, length=sh.end)
    if variant == "inline":
        return [file_range, Range(data=sh.zindex)]
    if variant == "internal":
        if isinstance(w, VZipWriter):
            index_key = w.add_hidden(f"idx/{key}", sh.zindex)  # DEFLATE
        else:
            index_key = f"idx/{key}"
            w.add_bytes(index_key, sh.zindex, codec="deflate")
        return [file_range, Range(source=w.internal(index_key), length=len(sh.zindex))]
    if variant == "native":
        crc = struct.pack("<I", crc32c(sh.entries))
        return [file_range, Range(source=src, offset=sh.entries_off, length=INDEX), Range(data=crc)]
    raise ValueError(variant)


def write(path: Path, binding: str, variant: str, shards: list[Shard], meta: bytes, **opts) -> int:
    with open(path, "wb") as f:
        w = VZipWriter(f, **opts) if binding == "vzip" else PhkvWriter(f, **opts)
        with w:
            w.add_bytes("zarr.json", meta)
            for sh in sorted(shards, key=lambda s: s.key):
                w.add_ranges(sh.key, _ranges(w, sh, variant, sh.key))
    return path.stat().st_size


REGIONS = {
    "full shard c/0/0/0": (slice(0, 64), slice(0, 64), slice(0, 64)),
    "full shard c/0/0/5": (slice(64, 128), slice(128, 192), slice(2624, 2688)),
    "partial shard c/0/0/23": (slice(0, 128), slice(0, 128), slice(11776, 12000)),
    "shard c/0/1/0": (slice(0, 64), slice(512, 576), slice(0, 64)),
}


def verify(out: Path) -> bool:
    """Decode REGIONS through zarr-python from every archive of `out`; True if all agree."""
    import hashlib  # noqa: PLC0415

    import refkv.zarr  # noqa: PLC0415
    import zarr  # noqa: PLC0415

    seen: dict[str, dict[str, str]] = {}
    for p in sorted(out.iterdir()):
        arr = zarr.open_array(refkv.zarr.open(str(p), **({"binding": "phkv"} if p.suffix == ".phkv" else {})), mode="r")
        seen[p.name] = {name: hashlib.md5(arr[sl].tobytes()).hexdigest()[:10] for name, sl in REGIONS.items()}  # noqa: S324
    ok = True
    for name in REGIONS:
        digests = {v[name] for v in seen.values()}
        ok &= len(digests) == 1
        print(f"{name:<24} identical across {len(seen)} archives: {len(digests) == 1}  {sorted(digests)}")
    return ok


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out", type=Path, help="directory for the archives")
    ap.add_argument("--verify", action="store_true", help="decode regions from every archive and compare them")
    args = ap.parse_args()
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    meta, _ = http(f"{BUCKET}/{PREFIX}/zarr.json")
    codecs = json.loads(meta)["codecs"][0]["configuration"]
    assert codecs["index_location"] == "end" and [c["name"] for c in codecs["index_codecs"]] == ["bytes", "crc32c"]
    with cf.ThreadPoolExecutor(8) as ex:
        shards = list(ex.map(load_shard, list_shards()))
    full = [s for s in shards if s.present == 512]
    print(f"{len(shards)} shards, {len(full)} full, {len(shards) - len(full)} partial "
          f"(chunks allocated: {sorted({s.present for s in shards})}); "
          f"entries identical to Zarr's in {sum(s.same_entries for s in shards)}")
    rows = []
    for binding, ext, optsets in (
        ("vzip", ".vzip", {"": {}, " (mirror_refs=False)": {"mirror_refs": False}}),
        ("phkv", ".phkv", {"": {}}),
    ):
        for note, opts in optsets.items():
            for variant in ("inline", "internal", "native"):
                p = out / f"{binding}{'-nomirror' if note else ''}-{variant}{ext}"
                rows.append((f"{binding}{note}", variant, write(p, binding, variant, shards, meta, **opts)))
    print()
    print(f"{'binding':<26}{'index':<10}{'bytes':>10}{'per shard':>11}{'vs inline':>12}{'vs internal':>13}")
    base = {}
    for b, v, n in rows:
        base.setdefault((b, v), n)
        i, t = base.get((b, "inline"), n), base.get((b, "internal"), n)
        print(f"{b:<26}{v:<10}{n:>10,}{n / len(shards):>11,.0f}{n - i:>+12,}{n - t:>+13,}")
    if args.verify:
        print()
        sys.exit(0 if verify(out) else 1)


if __name__ == "__main__":
    main()
