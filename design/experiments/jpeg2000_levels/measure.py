"""Prototype of design/proposals/jpeg2000-levels.md: a JPEG 2000 tile's lower resolutions as codestreams
made of a few literal bytes and byte ranges of the file, checked against reference decoders.

    measure.py sentinel <workdir> [url]  a real Sentinel-2 band file, read by range requests, against OpenJPEG's reduced decode
    measure.py tiles <workdir>           tiles whose origin is not a multiple of 2^N, against the encoders' own decoders
    measure.py file <workdir> <a.jp2>    a file without PLT, its packets found by walking their headers

These need opj_compress, opj_decompress, ojph_compress and ojph_expand on PATH, such as with
`pixi exec --spec openjpeg --spec openjph`. It is a design note's experiment, not part of the package.
"""

from __future__ import annotations

import json
import math
import struct
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import imagecodecs
import numpy as np

SOC, SIZ, COD, QCD, COC, QCC, TLM, PLM, PPM = (bytes.fromhex(x) for x in
                                               ("ff4f", "ff51", "ff52", "ff5c", "ff53", "ff5d", "ff55", "ff57", "ff60"))
SOT, SOD, EOC, PLT = (bytes.fromhex(x) for x in ("ff90", "ff93", "ffd9", "ff58"))
SENTINEL = ("https://storage.googleapis.com/gcp-public-data-sentinel-2/tiles/10/S/EG/"
            "S2B_MSIL1C_20240102T190809_N0510_R013_T10SEG_20240102T205807.SAFE/")


def ceil_shift(a: int, n: int) -> int:
    return -((-a) >> n)


def nprec(z0: int, z1: int, d: int, e: int) -> int:
    """Precincts along an axis of a resolution d levels below the full one (spec/zarrify/safe.md §8.6)."""
    a, b = ceil_shift(z0, d), ceil_shift(z1, d)
    return ceil_shift(b, e) - (a >> e) if b > a else 0


@dataclass
class Main:
    X: int
    Y: int
    XT: int
    YT: int
    C: int
    N: int
    layers: int
    progression: int
    precincts: list[int]
    siz: bytes
    segments: list[tuple[bytes, bytes]]
    first_sot: int

    @property
    def nx(self) -> int:
        return math.ceil(self.X / self.XT)

    def area(self, t: int) -> tuple[int, int, int, int]:
        u, v = t % self.nx, t // self.nx
        return u * self.XT, min((u + 1) * self.XT, self.X), v * self.YT, min((v + 1) * self.YT, self.Y)


@dataclass
class TilePart:
    body: int  # the offset of the bytes after SOD
    end: int
    plt: list[int] = field(default_factory=list)


def parse_main(b: bytes, base: int = 0) -> Main:
    assert b[base:base + 2] == SOC
    pos, segments = base + 2, []
    while b[pos:pos + 2] != SOT:
        length = struct.unpack(">H", b[pos + 2:pos + 4])[0]
        segments.append((b[pos:pos + 2], b[pos:pos + 2 + length]))
        pos += 2 + length
    seg = dict(segments)
    for marker in (COC, QCC, PPM):
        assert marker not in seg, "the prototype leaves these out"
    siz, cod = seg[SIZ], seg[COD]
    X, Y, XO, YO, XT, YT, XTO, YTO = struct.unpack(">8I", siz[6:38])
    assert XO == YO == XTO == YTO == 0
    assert all(siz[41 + 3 * c:43 + 3 * c] == b"\x01\x01" for c in range(struct.unpack(">H", siz[38:40])[0]))
    N = cod[9]
    return Main(X, Y, XT, YT, struct.unpack(">H", siz[38:40])[0], N, struct.unpack(">H", cod[6:8])[0], cod[5],
                list(cod[14:15 + N]) if cod[4] & 1 else [0xff] * (N + 1), siz, segments, pos)


def tile_part_at(head: bytes, at: int) -> tuple[int, int, int, TilePart]:
    """(Isot, TPsot, Psot, the tile-part) of the SOT at file offset `at`, whose first bytes are `head`."""
    assert head[:2] == SOT
    isot, psot, tp = struct.unpack(">HIB", head[4:11])
    q, plt = 12, []
    while head[q:q + 2] != SOD:
        length = struct.unpack(">H", head[q + 2:q + 4])[0]
        if head[q:q + 2] == PLT:
            value = 0
            for x in head[q + 5:q + 2 + length]:
                value = (value << 7) | (x & 0x7f)
                if not x & 0x80:
                    plt.append(value)
                    value = 0
        q += 2 + length
    return isot, tp, psot, TilePart(at + q + 2, at + psot, plt)


class Bits:
    """A packet header's bits (ISO/IEC 15444-1 B.10.1): after a byte 0xFF, a byte holds 7."""

    def __init__(self, data: bytes, pos: int) -> None:
        self.data, self.pos, self.byte, self.left = data, pos, 0, 0

    def bit(self) -> int:
        if not self.left:
            self.left = 7 if self.byte == 0xff else 8
            self.byte = self.data[self.pos]
            self.pos += 1
        self.left -= 1
        return (self.byte >> self.left) & 1

    def bits(self, n: int) -> int:
        value = 0
        for _ in range(n):
            value = (value << 1) | self.bit()
        return value

    def end(self) -> int:
        return self.pos + 1 if self.byte == 0xff else self.pos


class TagTree:
    """A tag tree's decoder (ISO/IEC 15444-1 B.10.2)."""

    def __init__(self, w: int, h: int) -> None:
        self.sizes = [(w, h)]
        while self.sizes[-1] != (1, 1):
            self.sizes.append(((self.sizes[-1][0] + 1) // 2, (self.sizes[-1][1] + 1) // 2))
        self.low = [[0] * (a * b) for a, b in self.sizes]
        self.value = [[1 << 30] * (a * b) for a, b in self.sizes]

    def below(self, bits: Bits, x: int, y: int, threshold: int) -> bool:
        """Whether the value at leaf (x, y) is below `threshold`."""
        low = 0
        for k in range(len(self.sizes) - 1, -1, -1):
            i = (y >> k) * self.sizes[k][0] + (x >> k)
            low = max(low, self.low[k][i])
            while low < threshold and low < self.value[k][i]:
                if bits.bit():
                    self.value[k][i] = low
                else:
                    low += 1
            self.low[k][i] = low
        return self.value[0][y * self.sizes[0][0] + x] < threshold


def passes(bits: Bits) -> int:
    """A code-block's number of coding passes (table B.4)."""
    if not bits.bit():
        return 1
    if not bits.bit():
        return 2
    value = bits.bits(2)
    if value != 3:
        return 3 + value
    value = bits.bits(5)
    return 6 + value if value != 31 else 37 + bits.bits(7)


def walk_packets(m: Main, t: int, data: bytes, start: int) -> list[tuple[int, int, int]]:
    """(offset, header length, body length) of each packet of tile `t`, whose first tile-part's packets start at `start`,
    by reading the packet headers. One layer of the default code-block style, in resolution-major order."""
    cod = dict(m.segments)[COD]
    assert m.layers == 1 and m.progression in (0, 1, 2) and m.C == 1 and cod[12] == 0 and not cod[4] & 6
    xcb, ycb = cod[10] + 2, cod[11] + 2
    x0, x1, y0, y1 = m.area(t)
    out, pos = [], start
    for r in range(m.N + 1):
        d, ppx, ppy = m.N - r, m.precincts[r] & 15, m.precincts[r] >> 4
        rx0, rx1, ry0, ry1 = (ceil_shift(v, d) for v in (x0, x1, y0, y1))
        nb = m.N if r == 0 else d + 1
        offsets = [(0, 0)] if r == 0 else [(1, 0), (0, 1), (1, 1)]
        for p in range(nprec(x0, x1, d, ppx) * nprec(y0, y1, d, ppy)):
            npw = nprec(x0, x1, d, ppx)
            qx0 = ((rx0 >> ppx) + p % npw) << ppx
            qy0 = ((ry0 >> ppy) + p // npw) << ppy
            qx1, qy1 = qx0 + (1 << ppx), qy0 + (1 << ppy)
            shift = 1 if r else 0
            bits, body = Bits(data, pos), 0
            present = bits.bit()
            for xo, yo in offsets if present else []:
                half = (1 << (nb - 1)) if r else 0
                bx0, bx1 = (ceil_shift(v - half * xo, nb) for v in (x0, x1))
                by0, by1 = (ceil_shift(v - half * yo, nb) for v in (y0, y1))
                ax0, ax1 = max(bx0, qx0 >> shift), min(bx1, qx1 >> shift)
                ay0, ay1 = max(by0, qy0 >> shift), min(by1, qy1 >> shift)
                if ax1 <= ax0 or ay1 <= ay0:
                    continue
                cw, ch = min(xcb, ppx - shift), min(ycb, ppy - shift)
                w, h = ceil_shift(ax1, cw) - (ax0 >> cw), ceil_shift(ay1, ch) - (ay0 >> ch)
                included, zero = TagTree(w, h), TagTree(w, h)
                for j in range(h):
                    for i in range(w):
                        if not included.below(bits, i, j, 1):
                            continue
                        z = 1
                        while not zero.below(bits, i, j, z):
                            z += 1
                        count = passes(bits)
                        lblock = 3
                        while bits.bit():
                            lblock += 1
                        body += bits.bits(lblock + count.bit_length() - 1)
            end = bits.end()
            out.append((pos, end - pos, body))
            pos = end + body
    return out


def rest_of_main(m: Main, n: int) -> bytes:
    """The main header after SIZ at n levels down: COD's levels and precincts and QCD's bands cut, TLM and PLM left out."""
    out = b""
    r = m.N - n
    for marker, b in m.segments:
        if marker in (SIZ, TLM, PLM):
            continue
        if marker == COD:
            b = bytearray(b)
            b[9] = r
            if b[4] & 1:
                b = b[:14 + r + 1]
                b[2:4] = struct.pack(">H", len(b) - 2)
            b = bytes(b)
        elif marker == QCD:
            style = b[4] & 0x1f
            if style != 1:  # the scalar derived form's exponents do not depend on the levels cut
                size = 1 if style == 0 else 2
                b = bytearray(b[:5 + (1 + 3 * r) * size])
                b[2:4] = struct.pack(">H", len(b) - 2)
                b = bytes(b)
        out += b
    return out


def body_ranges(m: Main, t: int, parts: list[TilePart], n: int) -> list[tuple[int, int]]:
    """The file ranges of tile `t`'s packets of the resolutions kept."""
    r = m.N - n
    if len(parts) == m.N + 1:  # one tile-part a resolution
        return [(p.body, p.end - p.body) for p in parts[:r + 1]]
    assert len(parts) == 1 and parts[0].plt, "needs PLT, or one tile-part a resolution"
    assert m.progression in (1, 2) or m.layers == 1, "not resolution-major"
    x0, x1, y0, y1 = m.area(t)
    count = m.layers * m.C * sum(nprec(x0, x1, m.N - k, m.precincts[k] & 15) * nprec(y0, y1, m.N - k, m.precincts[k] >> 4)
                                 for k in range(r + 1))
    return [(parts[0].body, sum(parts[0].plt[:count]))]


def codestream(m: Main, t: int, parts: list[TilePart], n: int, read, *, keep_grid: bool = True,
               pad: bool = False) -> tuple[bytes, int]:
    """(the codestream of tile `t` at n levels down, the file bytes it refers to). `keep_grid` places the tile on the
    reference grid where the file does, scaled; otherwise it is moved to the origin. `pad` adds empty tiles to fill the
    tile's cell of the chunk grid, as spec/zarrify/safe.md §8.6 does."""
    x0, x1, y0, y1 = m.area(t)
    ax0, ax1, ay0, ay1 = (ceil_shift(v, n) for v in (x0, x1, y0, y1))
    w, h = ax1 - ax0, ay1 - ay0
    cw, ch = ceil_shift(x0 + m.XT, n) - ax0, ceil_shift(y0 + m.YT, n) - ay0
    ox, oy = (ax0, ay0) if keep_grid else (0, 0)
    width, height = (cw, ch) if pad else (w, h)
    # Rsiz: a profile's limits may not hold for the new tiles, but bit 14 says the stream is HTJ2K, and its CAP marker stays
    rsiz = struct.pack(">H", struct.unpack(">H", m.siz[4:6])[0] & 0x4000)
    siz = m.siz[:4] + rsiz + struct.pack(">8I", ox + width, oy + height, ox, oy, w, h, ox, oy) + m.siz[38:]
    ranges = body_ranges(m, t, parts, n)
    size = sum(length for _, length in ranges)
    out = SOC + siz + rest_of_main(m, n) + SOT + struct.pack(">HHIBB", 10, 0, 14 + size, 0, 1) + SOD
    for offset, length in ranges:
        out += read(offset, length)
    r = m.N - n
    if pad:
        a, b = math.ceil(cw / w), math.ceil(ch / h)
        for k in range(1, a * b):
            i, j = k % a, k // a
            ex0, ex1 = ox + i * w, min(ox + (i + 1) * w, ox + cw)
            ey0, ey1 = oy + j * h, min(oy + (j + 1) * h, oy + ch)
            empty = m.layers * m.C * sum(nprec(ex0, ex1, r - q, m.precincts[q] & 15) * nprec(ey0, ey1, r - q, m.precincts[q] >> 4)
                                         for q in range(r + 1))
            out += SOT + struct.pack(">HHIBB", 10, k, 14 + empty, 0, 1) + SOD + b"\0" * empty
    return out + EOC, size


def analysis_53(x: np.ndarray) -> np.ndarray:
    """The low-pass half of one level of the reversible 5/3 transform along axis 0 (ISO/IEC 15444-1 F.3.8), from an even start."""
    n = x.shape[0]
    if n == 1:
        return x

    def mirror(i: np.ndarray) -> np.ndarray:
        i = np.mod(i, 2 * (n - 1))
        return np.where(i < n, i, 2 * (n - 1) - i)

    odd, even = np.arange(1, n, 2), np.arange(0, n, 2)
    d = np.zeros_like(x)
    d[odd] = x[odd] - np.floor_divide(x[mirror(odd - 1)] + x[mirror(odd + 1)], 2)
    return x[even] + np.floor_divide(d[mirror(even - 1)] + d[mirror(even + 1)] + 2, 4)


def low_pass(a: np.ndarray, n: int) -> np.ndarray:
    a = a.astype(np.int64)
    for _ in range(n):
        a = analysis_53(a)  # vertical first, as F.4.2 does
        a = analysis_53(a.T).T
    return a


def get(url: str, offset: int, length: int) -> bytes:
    request = urllib.request.Request(url, headers={"Range": f"bytes={offset}-{offset + length - 1}"})
    with urllib.request.urlopen(request, timeout=120) as r:
        return r.read()


def sentinel(url: str | None, work: Path) -> None:
    work.mkdir(parents=True, exist_ok=True)
    if url is None:
        listing = ("https://storage.googleapis.com/storage/v1/b/gcp-public-data-sentinel-2/o?maxResults=500&prefix="
                   + SENTINEL.split("gcp-public-data-sentinel-2/")[1])
        with urllib.request.urlopen(listing, timeout=60) as r:
            names = [i["name"] for i in json.load(r)["items"]]
        url = "https://storage.googleapis.com/gcp-public-data-sentinel-2/" + next(x for x in names if x.endswith("_B02.jp2"))
    print(url)
    head = get(url, 0, 1 << 16)
    base = head.find(SOC + SIZ)
    m = parse_main(head, base)
    print(f"{m.X}x{m.Y}, tiles {m.XT}, N={m.N}, layers={m.layers}, progression={m.progression}, precincts={m.precincts}")
    at, header_bytes, tiles = m.first_sot, 0, {}
    nt = m.nx * math.ceil(m.Y / m.YT)
    for k in range(nt):
        isot, _, psot, part = tile_part_at(get(url, at, 1 << 14), at)
        assert isot == k
        tiles[k] = part
        header_bytes += part.body - at
        at += psot
    print(f"{nt} tiles, {header_bytes} bytes of tile-part headers to read, {at - base} bytes of codestream")
    size_of = lambda k: tiles[k].end - tiles[k].body  # noqa: E731
    last_column = max(range(m.nx - 1, nt, m.nx), key=size_of)
    last_row = max(range(nt - m.nx, nt), key=size_of)
    for t in (0, 1, last_column, last_row):
        whole = get(url, tiles[t].body, tiles[t].end - tiles[t].body)
        offset = tiles[t].body

        def read(a: int, n: int, whole: bytes = whole, offset: int = offset) -> bytes:
            return whole[a - offset:a - offset + n]

        x0, x1, y0, y1 = m.area(t)
        whole_tile = codestream(m, t, [tiles[t]], 0, read)[0]
        full = imagecodecs.jpeg2k_decode(whole_tile)
        (work / "tile.j2k").write_bytes(whole_tile)
        clipped = int((full == 0).sum() + (full == (1 << (m.siz[40] + 1)) - 1).sum())
        cells = []
        for n in range(1, m.N + 1):
            data, size = codestream(m, t, [tiles[t]], n, read, pad=True)
            got = imagecodecs.jpeg2k_decode(data)
            w, h = ceil_shift(x1, n) - ceil_shift(x0, n), ceil_shift(y1, n) - ceil_shift(y0, n)
            subprocess.run(["opj_decompress", "-i", "tile.j2k", "-o", "ref.pgm", "-r", str(n)], cwd=work, check=True, capture_output=True)
            same = np.array_equal(got[:h, :w], read_pgm(work / "ref.pgm"))
            other = int((got[:h, :w] != low_pass(full, n)).sum())
            cells.append(f"n={n} {got.shape} {size} bytes ({100 * size / len(whole):.1f}%) literal {len(data) - size} "
                         f"equal to opj_decompress -r: {same}; samples unlike a 5/3 of the decoded tile: {other}")
        print(f"tile {t} at ({x0}, {y0}), {x1 - x0}x{y1 - y0}, {len(whole)} bytes, {clipped} samples at the range's ends:\n  " + "\n  ".join(cells))


def one_file(path: Path, work: Path) -> None:
    """A .jp2 file without PLT, as a Human Organ Atlas slice is: its packets found by walking their headers."""
    work.mkdir(parents=True, exist_ok=True)
    jp2 = path.read_bytes()
    data = jp2[jp2.find(SOC + SIZ):]
    m = parse_main(data)
    _, _, _, part = tile_part_at(data[m.first_sot:m.first_sot + (1 << 16)], m.first_sot)
    packets = walk_packets(m, 0, data, part.body)
    assert packets[-1][0] + packets[-1][1] + packets[-1][2] == part.end, "the walk must end at the tile-part's end"
    print(f"{m.X}x{m.Y}, N={m.N}, {len(packets)} packets; (offset, header, body):", packets)
    print(f"{sum(p[1] for p in packets)} bytes of packet headers in {len(data)} bytes")
    part.plt = [p[1] + p[2] for p in packets]
    (work / "slice.j2k").write_bytes(data)
    for n in range(1, m.N + 1):
        stream, size = codestream(m, 0, [part], n, lambda o, k: data[o:o + k])
        got = imagecodecs.jpeg2k_decode(stream)
        subprocess.run(["opj_decompress", "-i", "slice.j2k", "-o", "ref.pgm", "-r", str(n)], cwd=work, check=True,
                       capture_output=True)
        truth = read_pgm(work / "ref.pgm")
        print(f"n={n} {got.shape} {size} bytes ({100 * size / len(data):.2f}%), literal {len(stream) - size}, "
              f"equal to opj_decompress -r: {got.shape == truth.shape and np.array_equal(got, truth)}")


def synthetic() -> np.ndarray:
    rng = np.random.default_rng(0)
    y, x = np.mgrid[0:512, 0:512]
    image = 20000 + 9000 * np.sin(x / 17) * np.cos(y / 23) + rng.normal(0, 400, (512, 512))
    return np.clip(image, 0, 65535).astype(np.uint16)


def write_pgm(path: Path, a: np.ndarray) -> None:
    path.write_bytes(b"P5\n%d %d\n65535\n" % (a.shape[1], a.shape[0]) + a.astype(">u2").tobytes())


def read_pgm(path: Path) -> np.ndarray:
    d = path.read_bytes()
    fields = [x for x in d[:256].split(b"\n") if not x.startswith(b"#")]
    w, h = (int(x) for x in fields[1].split())
    return np.frombuffer(d[-2 * w * h:], ">u2").reshape(h, w).astype(np.uint16)


def stitch(m: Main, tiles: dict[int, list[TilePart]], n: int, data: bytes, decode, keep_grid: bool) -> np.ndarray:
    out = np.zeros((ceil_shift(m.Y, n), ceil_shift(m.X, n)), np.uint16)
    for t, parts in tiles.items():
        x0, x1, y0, y1 = m.area(t)
        a, b = ceil_shift(x0, n), ceil_shift(y0, n)
        stream = codestream(m, t, parts, n, lambda o, k: data[o:o + k], keep_grid=keep_grid)[0]
        got = decode(stream)
        h, w = ceil_shift(y1, n) - b, ceil_shift(x1, n) - a
        out[b:b + h, a:a + w] = got[:h, :w]
    return out


def tiles_experiment(work: Path) -> None:
    work.mkdir(parents=True, exist_ok=True)
    image = synthetic()
    write_pgm(work / "in.pgm", image)
    encoders = {
        "OpenJPEG, one tile-part a resolution": (
            "o_tp.j2k", ["opj_compress", "-i", "in.pgm", "-o", "o_tp.j2k", "-t", "TS,TS", "-n", "4", "-p", "RPCL", "-TP", "R"],
            imagecodecs.jpeg2k_decode, lambda n: (["opj_decompress", "-i", "o_tp.j2k", "-o", "ref.pgm", "-r", str(n)])),
        "OpenJPEG, PLT": (
            "o_plt.j2k", ["opj_compress", "-i", "in.pgm", "-o", "o_plt.j2k", "-t", "TS,TS", "-n", "4", "-p", "RPCL", "-PLT"],
            imagecodecs.jpeg2k_decode, lambda n: (["opj_decompress", "-i", "o_plt.j2k", "-o", "ref.pgm", "-r", str(n)])),
        "OpenJPEG, 9/7 at a ratio of 10, PLT": (
            "o_97.j2k", ["opj_compress", "-i", "in.pgm", "-o", "o_97.j2k", "-t", "TS,TS", "-n", "4", "-p", "RPCL", "-PLT", "-I", "-r", "10"],
            imagecodecs.jpeg2k_decode, lambda n: (["opj_decompress", "-i", "o_97.j2k", "-o", "ref.pgm", "-r", str(n)])),
        "OpenJPH (HTJ2K), one tile-part a resolution": (
            "h_tp.j2c", ["ojph_compress", "-i", "in.pgm", "-o", "h_tp.j2c", "-tile_size", "{TS,TS}", "-num_decomps", "3", "-reversible", "true",
                         "-prog_order", "RPCL", "-tileparts", "R"],
            imagecodecs.htj2k_decode, lambda n: (["ojph_expand", "-i", "h_tp.j2c", "-o", "ref.pgm", "-skip_res", str(n)])),
    }
    for ts in (512, 256, 100, 99):
        for name, (out, encode, decode, reference) in encoders.items():
            subprocess.run([a.replace("TS", str(ts)) for a in encode], cwd=work, check=True, capture_output=True)
            data = (work / out).read_bytes()
            m = parse_main(data)
            tiles: dict[int, list[TilePart]] = {}
            at = m.first_sot
            while data[at:at + 2] == SOT:
                isot, _, psot, part = tile_part_at(data[at:at + (1 << 16)], at)
                tiles.setdefault(isot, []).append(part)
                at += psot
            line = []
            for n in range(1, m.N + 1):
                subprocess.run(reference(n), cwd=work, check=True, capture_output=True)
                truth = read_pgm(work / "ref.pgm")
                cells = {}
                for label, keep in (("grid kept", True), ("moved to origin", False)):
                    try:
                        got = stitch(m, tiles, n, data, decode, keep)
                        cells[label] = "equal" if got.shape == truth.shape and np.array_equal(got, truth) else (
                            f"max diff {np.abs(got.astype(int) - truth.astype(int)).max()}" if got.shape == truth.shape else f"shape {got.shape}")
                    except Exception as e:  # noqa: BLE001 - a decoder's refusal is a result
                        cells[label] = f"{type(e).__name__}: {str(e)[:60]}"
                line.append(f"n={n}: kept {cells['grid kept']}; moved {cells['moved to origin']}")
            print(f"tile {ts}, {name}, N={m.N}, {len(tiles)} tiles, {len(next(iter(tiles.values())))} tile-parts each:\n  " + "\n  ".join(line))


if __name__ == "__main__":
    if sys.argv[1] == "sentinel":
        sentinel(sys.argv[3] if len(sys.argv) > 3 else None, Path(sys.argv[2]))
    elif sys.argv[1] == "file":
        one_file(Path(sys.argv[3]), Path(sys.argv[2]))
    else:
        tiles_experiment(Path(sys.argv[2]))
