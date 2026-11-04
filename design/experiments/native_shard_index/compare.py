# /// script
# requires-python = ">=3.12"
# dependencies = ["refkv[zarr]", "fsspec", "aiohttp"]
#
# [tool.uv]
# # what the lock file resolved, and nothing published after it
# exclude-newer = "2026-10-28T00:00:00Z"
#
# [tool.uv.sources]
# # PR 141's head (refkv.phkv); building refkv needs a Rust toolchain (cargo)
# refkv = { git = "https://github.com/d-v-b/refkv", subdirectory = "python", rev = "632883306ff30ab0c420db15888b0cbc77591ebd" }
# ///
"""Virtual shards of an HDF5-and-Zarr array, with the shard index stored four ways, in vzip and phkv
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
  spliced   native, but the empty slots (HDF5: address 2**64-1, size 0) are 0xff bytes, Zarr's own encoding
            of an empty chunk (ranges of one 0xff source, shared by the archive), between ranges of the file for
            the runs of present entries

Usage (the script's dependencies are its inline metadata, locked in compare.py.lock; phkv needs
refkv with PR 141, refkv.phkv, which the metadata pins; building refkv needs a Rust toolchain):

    uv run --script design/experiments/native_shard_index/compare.py OUT_DIR [--verify]

It reads the first 16 KiB and the last 8196 bytes of every shard, over HTTP, and writes twelve archives
into OUT_DIR. `--verify` checks every archive against the source array: four decoded regions, and the index bytes of every shard.
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures as cf
import hashlib
import json
import os
import re
import struct
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import refkv.zarr
import zarr
from refkv.phkv import PhkvWriter
from refkv.pb import Range
from refkv.policy import crc32c
from refkv.vzip.archive import VZipWriter
from zarr.abc.store import SuffixByteRequest
from zarr.core.buffer import default_buffer_prototype
from zarr.core.sync import sync
from zarr.storage import FsspecStore

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
    """(file offset of the entries, the entries) of the one fixed array chunk index in `head`.

    The file's addresses must be offsets from the start of the file, which the entries then are: a
    superblock (version 2 or 3) with 8-byte offsets and lengths and base address 0 (a userblock, H5Pset_userblock,
    moves the base address, and every address with it).
    """
    if head[:8] != b"\x89HDF\r\n\x1a\n" or head[8] not in (2, 3):
        raise ValueError("not an HDF5 file with a version 2 or 3 superblock at its start")
    if (head[9], head[10]) != (8, 8):
        raise ValueError(f"offsets of {head[9]} bytes and lengths of {head[10]}: the entries need 8 and 8")
    if (base := int.from_bytes(head[12:20], "little")) != 0:
        raise ValueError(f"base address {base}: a userblock moves every address in the file")
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
                if esz != 16:  # 8 (address) + the chunk size field + 4 (mask): Zarr's entries are 16 bytes
                    raise ValueError(f"entries of {esz} bytes, so a chunk size field of {esz - 12}: the field is "
                                     "4 bytes for chunks of 64 KiB to under 16 MiB, uncompressed")
                if cid != 1 or nent > 1 << pb:
                    raise ValueError(f"unexpected FA: client {cid} (1 is filtered chunks), {nent} entries, "
                                     f"page bits {pb} (the entries are paged above 2**page bits)")
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
    keys: list[str] = []
    token = ""
    while True:
        url = f"{BUCKET}/?list-type=2&prefix={PREFIX}/c/&max-keys=1000"
        body, _ = http(url + (f"&continuation-token={urllib.parse.quote(token)}" if token else ""))
        text = body.decode()
        keys += re.findall(r"<Key>([^<]+)</Key>", text)
        if "<IsTruncated>true</IsTruncated>" not in text:
            return sorted(k[len(PREFIX) + 1:] for k in keys)
        token = re.search(r"<NextContinuationToken>([^<]+)</NextContinuationToken>", text).group(1)


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
    if variant == "spliced":
        # the entries as ranges of the file, but for the empty slots, which are Zarr's own 16 bytes of 0xff
        runs: list[Range] = []
        k = 0
        while k < 512:
            empty = sh.entries[16 * k:16 * k + 8] == b"\xff" * 8
            n = 1
            while k + n < 512 and (sh.entries[16 * (k + n):16 * (k + n) + 8] == b"\xff" * 8) == empty:
                n += 1
            runs.append(Range(source=w.blob(b"\xff" * INDEX), offset=0, length=16 * n) if empty else
                        Range(source=src, offset=sh.entries_off + 16 * k, length=16 * n))
            k += n
        return [file_range, *runs, Range(data=sh.zindex[INDEX:])]  # Zarr's CRC: of the bytes the reader sees
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


TAIL = 16 + INDEX + 4  # the last 16 bytes of the chunks, and the index with its CRC


def _digest(x) -> str:  # noqa: ANN001 (a numpy array)
    return hashlib.md5(x.tobytes(), usedforsecurity=False).hexdigest()[:10]


def expected_tail(sh: Shard, variant: str) -> bytes:
    """The last TAIL bytes of a virtual shard, from the shard's own file and the index `variant` presents.

    The virtual shard is as long as the file (the chunks end where zarrindex starts, and the index is as long), so
    the bytes before the index are the file's, whatever `end` the writer used.
    """
    chunks, _ = http(sh.url, f"bytes={sh.end - 16}-{sh.end - 1}")
    index = sh.entries + struct.pack("<I", crc32c(sh.entries)) if variant == "native" else sh.zindex
    return chunks + index


async def _tails(store: zarr.abc.store.Store, shards: list[Shard]) -> dict[str, bytes]:
    proto = default_buffer_prototype()
    got = {}
    for sh in shards:
        buf = await store.get(sh.key, proto, byte_range=SuffixByteRequest(TAIL))
        got[sh.key] = b"" if buf is None else buf.to_bytes()
    return got


def verify(out: Path, shards: list[Shard]) -> bool:
    """Check every archive of `out` against the source array, not only against each other.

    - REGIONS decoded through zarr-python from the archive equal the same regions read from the source array
      at BUCKET/PREFIX directly (the shards themselves, as a Zarr array over HTTP).
    - The last TAIL bytes of every virtual shard, as the archive's store serves them, equal the bytes of the
      shard's own file followed by the index the variant presents (the Zarr index, or for "native" the verbatim
      entries and their CRC).
    """
    truth_arr = zarr.open_array(f"{BUCKET}/{PREFIX}", mode="r")
    truth = {name: _digest(truth_arr[sl]) for name, sl in REGIONS.items()}
    print("source array, read directly:", truth)
    expected = {(sh.key, v): expected_tail(sh, v) for sh in shards for v in ("inline", "native")}
    ok = True
    for p in sorted(out.iterdir()):
        variant = p.stem.rsplit("-", 1)[1]
        store = refkv.zarr.open(str(p), **({"binding": "phkv"} if p.suffix == ".phkv" else {}))
        arr = zarr.open_array(store, mode="r")
        bad = [name for name, sl in REGIONS.items() if _digest(arr[sl]) != truth[name]]
        tails = sync(_tails(store, shards))
        bad_tails = [sh.key for sh in shards
                     if tails[sh.key] != expected[(sh.key, "native" if variant == "native" else "inline")]]
        ok &= not bad and not bad_tails
        print(f"{p.name:<28} regions equal the source: {not bad}  index bytes as expected: {not bad_tails}"
              + (f"  differ: {bad} {bad_tails}" if bad or bad_tails else ""))
    return ok


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out", type=Path, help="directory for the archives")
    ap.add_argument("--verify", action="store_true", help="check every archive against the source array")
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
            for variant in ("inline", "internal", "native", "spliced"):
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
        sys.exit(0 if verify(out, shards) else 1)


if __name__ == "__main__":
    main()
