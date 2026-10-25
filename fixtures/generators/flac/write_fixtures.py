"""Writes synthetic FLAC files to fixtures/flac/, covering the rules of the FLAC profile
(spec/zarrify/flac.md §8
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/flac.md#8-flac-profile>): mono and stereo,
8 to 32 bits per sample, every metadata block type, ID3v2 and ID3v1 tags, uncommon block sizes and
sample rates, header codes that defer to STREAMINFO, streams of one frame and of several shards,
and the inputs the profile rejects (`edge_reject_flac_*`).

The audio is a deterministic signal (tones and seeded noise). Most files' frames are ffmpeg's FLAC
encoder's (LPC and fixed subframes, Rice-coded residuals, stereo decorrelation); their metadata is
written here, around ffmpeg's STREAMINFO block (whose MD5 and total ffmpeg fills in). The rest are
written here bit for bit (`frame`: verbatim subframes), for what ffmpeg's encoder cannot make: 8,
12 and 32 bits, uncommon codes, a variable block size, frames that break the stream's rules.

`just fixtures` does not run this generator (it needs ffmpeg, whose encoder's output may differ
from version to version, as the MP3 and MP4 generators do): `just fixtures-of flac` does.

Usage: uv run python fixtures/generators/flac/write_fixtures.py
"""

# fixtures: on demand (it needs ffmpeg; `just fixtures` skips it)

from __future__ import annotations

import hashlib
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

import numpy as np

OUT = Path(__file__).parents[2] / "flac"

STREAMINFO, PADDING, APPLICATION, SEEKTABLE, VORBIS_COMMENT, CUESHEET, PICTURE = range(7)
# frame header codes (RFC 9639 §9.1.2 <https://www.rfc-editor.org/rfc/rfc9639#section-9.1.2>)
RATE_CODES = {88200: 1, 176400: 2, 192000: 3, 8000: 4, 16000: 5, 22050: 6, 24000: 7, 32000: 8, 44100: 9,
              48000: 10, 96000: 11}
BITS_CODES = {8: 1, 12: 2, 16: 4, 20: 5, 24: 6, 32: 7}


def signal(rate: int, seconds: float, channels: int, bits: int, seed: int) -> np.ndarray:
    """Tones and seeded noise, [samples, channels] int64, within `bits` signed bits."""
    t = np.arange(round(rate * seconds)) / rate
    rng = np.random.default_rng(seed)
    full = 2 ** (bits - 1) - 1
    out = []
    for c in range(channels):
        x = 0.5 * np.sin(2 * np.pi * (220 * (c + 1)) * t) + 0.2 * np.sin(2 * np.pi * 1375 * t + c)
        x += 0.003 * rng.standard_normal(len(t))
        out.append(np.clip(np.rint(x * full), -full - 1, full).astype(np.int64))
    return np.stack(out, axis=1)


def md5(samples: np.ndarray, bits: int) -> bytes:
    """STREAMINFO's MD5 (RFC 9639 §8.2): the samples interleaved, each little-endian in whole
    bytes, sign-extended."""
    width = (bits + 7) // 8
    dtype = {1: "<i1", 2: "<i2", 3: "<i4", 4: "<i4"}[width]
    raw = samples.astype(dtype).tobytes()
    if width == 3:
        raw = b"".join(raw[k:k + 3] for k in range(0, len(raw), 4))
    return hashlib.md5(raw, usedforsecurity=False).digest()


def ffmpeg(samples: np.ndarray, rate: int, bits: int, *args: str) -> bytes:
    """ffmpeg's FLAC encoding of `samples` (a file, so that its STREAMINFO block holds the total
    and the MD5)."""
    exe = shutil.which("ffmpeg")
    if exe is None:
        sys.exit("write_fixtures.py: ffmpeg is not on PATH")
    wide = bits > 16
    raw = (samples << (32 - bits) if wide else samples).astype("<i4" if wide else "<i2").tobytes()
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out.flac"
        fmt = ["-sample_fmt", "s32", "-bits_per_raw_sample", str(bits)] if wide else []
        subprocess.run([exe, "-hide_banner", "-loglevel", "error", "-y", "-f", "s32le" if wide else "s16le",
                        "-ar", str(rate), "-ac", str(samples.shape[1]), "-i", "-", "-c:a", "flac", *fmt, *args,
                        "-fflags", "+bitexact", "-flags:a", "+bitexact", str(out)], input=raw, check=True)
        return out.read_bytes()


def split(data: bytes) -> tuple[bytes, bytes]:
    """(the STREAMINFO body, the frames) of a FLAC file without tags."""
    assert data[:4] == b"fLaC"
    p, info = 4, b""
    while True:
        last, kind, n = data[p] >> 7, data[p] & 0x7F, int.from_bytes(data[p + 1:p + 4], "big")
        if kind == STREAMINFO:
            info = data[p + 4:p + 4 + n]
        p += 4 + n
        if last:
            return info, data[p:]


def block(kind: int, body: bytes, *, last: bool = False) -> bytes:
    return bytes([kind | (0x80 if last else 0)]) + len(body).to_bytes(3, "big") + body


def flac(info: bytes, frames: bytes, *blocks: tuple[int, bytes]) -> bytes:
    """`fLaC`, STREAMINFO, `blocks`, the last flagged, then the frames."""
    out = [block(STREAMINFO, info, last=not blocks)]
    out += [block(k, b, last=i == len(blocks) - 1) for i, (k, b) in enumerate(blocks)]
    return b"fLaC" + b"".join(out) + frames


def streaminfo(min_block: int, max_block: int, rate: int, channels: int, bits: int, total: int,
               digest: bytes = bytes(16), frame_sizes: tuple[int, int] = (0, 0)) -> bytes:
    packed = rate << 44 | (channels - 1) << 41 | (bits - 1) << 36 | total
    return (struct.pack(">HH", min_block, max_block) + frame_sizes[0].to_bytes(3, "big")
            + frame_sizes[1].to_bytes(3, "big") + packed.to_bytes(8, "big") + digest)


def vorbis(vendor: str, *comments: str) -> bytes:
    out = struct.pack("<I", len(vendor.encode())) + vendor.encode() + struct.pack("<I", len(comments))
    for c in comments:
        out += struct.pack("<I", len(c.encode())) + c.encode()
    return out


def frame_offsets(frames: bytes, count: int) -> list[int]:
    """The offsets of a stream's frames in `frames`, by their CRC-16 (RFC 9639 §9.3) and the next
    header's sync code, as an independent check of the profile's own search."""
    out, p = [0], 0
    while len(out) < count:
        q = frames.index(b"\xff\xf8", p + 2)
        while crc16(frames[p:q]) != 0:
            q = frames.index(b"\xff\xf8", q + 1)
        out.append(q)
        p = q
    return out


def seektable(points: list[tuple[int, int, int]], placeholders: int = 0) -> bytes:
    body = b"".join(struct.pack(">QQH", s, o, n) for s, o, n in points)
    return body + struct.pack(">QQH", 2**64 - 1, 0, 0) * placeholders


def cuesheet() -> bytes:
    """A CD-DA cue sheet of two tracks and the lead-out (RFC 9639 §8.7)."""
    out = b"1234567890123".ljust(128, b"\0") + struct.pack(">Q", 88200) + b"\x80" + bytes(258) + bytes([3])
    for offset, number, isrc, indexes in ((0, 1, b"USABC2600001", [(0, 1)]), (2940, 2, b"", [(0, 0), (588, 1)]),
                                         (8820, 170, b"", [])):
        out += struct.pack(">QB", offset, number) + isrc.ljust(12, b"\0") + b"\x00" + bytes(13) + bytes([len(indexes)])
        for o, n in indexes:
            out += struct.pack(">QB", o, n) + bytes(3)
    return out


def png() -> bytes:
    """A 4 x 2 RGB PNG."""
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    rows = b"".join(b"\0" + bytes(v for x in range(4) for v in (x * 60, y * 120, 200)) for y in range(2))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 2, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


def picture(kind: int, mime: str, description: str, data: bytes, size: tuple[int, int, int, int]) -> bytes:
    return (struct.pack(">II", kind, len(mime)) + mime.encode() + struct.pack(">I", len(description.encode()))
            + description.encode() + struct.pack(">IIIII", *size, len(data)) + data)


def syncsafe(n: int) -> bytes:
    return bytes((n >> s) & 0x7F for s in (21, 14, 7, 0))


def id3v2(body: bytes, version: int = 4) -> bytes:
    return b"ID3" + bytes([version, 0, 0]) + syncsafe(len(body)) + body


def id3v1(title: str, artist: str, album: str, year: str, comment: str, track: int, genre: int) -> bytes:
    f = lambda s, n: s.encode().ljust(n, b"\0")  # noqa: E731
    return b"TAG" + f(title, 30) + f(artist, 30) + f(album, 30) + f(year, 4) + f(comment, 28) + bytes([0, track, genre])


# ---- frames written bit for bit

CRC8 = [0] * 256
CRC16 = [0] * 256
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = ((_c << 1) ^ 0x07) & 0xFF if _c & 0x80 else (_c << 1) & 0xFF
    CRC8[_i] = _c
    _c = _i << 8
    for _ in range(8):
        _c = ((_c << 1) ^ 0x8005) & 0xFFFF if _c & 0x8000 else (_c << 1) & 0xFFFF
    CRC16[_i] = _c


def crc8(b: bytes) -> int:
    c = 0
    for x in b:
        c = CRC8[c ^ x]
    return c


def crc16(b: bytes) -> int:
    c = 0
    for x in b:
        c = ((c << 8) & 0xFFFF) ^ CRC16[(c >> 8) ^ x]
    return c


def coded(n: int) -> bytes:
    """A frame or sample number as RFC 9639 §9.1.5 codes it (extended UTF-8)."""
    if n < 0x80:
        return bytes([n])
    for k in range(2, 8):
        if n < 1 << (5 * k + 1 if k < 7 else 36):
            out = []
            for _ in range(k - 1):
                out.append(0x80 | (n & 0x3F))
                n >>= 6
            lead = (0xFF << (8 - k)) & 0xFF
            return bytes([lead | n, *reversed(out)])
    raise ValueError(n)


class Bits:
    def __init__(self) -> None:
        self.v, self.n = 0, 0

    def put(self, value: int, width: int) -> None:
        self.v = self.v << width | (value & ((1 << width) - 1))
        self.n += width

    def bytes(self) -> bytes:
        pad = -self.n % 8
        return (self.v << pad).to_bytes((self.n + pad) // 8, "big")


def frame(samples: np.ndarray, bits: int, number: int, *, rate: int, assignment: int | None = None,
          rate_code: int | None = None, bits_code: int | None = None, block_code: int | None = None,
          variable: bool = False) -> bytes:
    """A frame of verbatim subframes (RFC 9639 §9): `samples` [block, channels], independent
    channels unless `assignment` (8 left-side, 9 side-right, 10 mid-side) says otherwise; the
    codes are the common ones, or as given (0: the STREAMINFO block's)."""
    block, channels = samples.shape
    if block_code is None:
        common = {192: 1, 576: 2, 1152: 3, 2304: 4, 4608: 5, **{1 << v: v for v in range(8, 16)}}
        block_code = common.get(block, 6 if block <= 256 else 7)
    if rate_code is None:
        rate_code = RATE_CODES.get(rate, 13)
    if bits_code is None:
        bits_code = BITS_CODES[bits]
    if assignment is None:
        assignment = channels - 1
    head = bytes([0xFF, 0xF8 | variable, block_code << 4 | rate_code, assignment << 4 | bits_code << 1])
    head += coded(number)
    if block_code == 6:
        head += bytes([block - 1])
    elif block_code == 7:
        head += (block - 1).to_bytes(2, "big")
    if rate_code == 12:
        head += bytes([rate // 1000])
    elif rate_code == 13:
        head += rate.to_bytes(2, "big")
    elif rate_code == 14:
        head += (rate // 10).to_bytes(2, "big")
    head += bytes([crc8(head)])
    left, right = samples[:, 0], samples[:, -1]
    channels_coded = {8: [(left, bits), (left - right, bits + 1)], 9: [(left - right, bits + 1), (right, bits)],
                      10: [((left + right) >> 1, bits), (left - right, bits + 1)]}
    subframes = channels_coded.get(assignment, [(samples[:, c], bits) for c in range(channels)])
    b = Bits()
    for values, width in subframes:
        b.put(0b00000010, 8)  # verbatim, no wasted bits
        for v in values.tolist():
            b.put(v, width)
    body = head + b.bytes()
    return body + crc16(body).to_bytes(2, "big")


def frames_of(samples: np.ndarray, block: int, bits: int, rate: int, **kw: object) -> bytes:
    return b"".join(frame(samples[k:k + block], bits, j, rate=rate, **kw)  # type: ignore[arg-type]
                    for j, k in enumerate(range(0, len(samples), block)))


def bitwise(samples: np.ndarray, block: int, bits: int, rate: int, *blocks: tuple[int, bytes], **kw: object) -> bytes:
    """A whole file of frames written here, with its STREAMINFO block (MD5 included)."""
    frames = frames_of(samples, block, bits, rate, **kw)
    info = streaminfo(block, block, rate, samples.shape[1], bits, len(samples), md5(samples, bits))
    return flac(info, frames, *blocks)


def write(name: str, data: bytes) -> None:
    (OUT / name).write_bytes(data)
    print(f"{name}: {len(data)} bytes")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.flac"):
        old.unlink()
    comments = vorbis("refkv fixtures", "TITLE=A tone", "ARTIST=refkv", "DATE=2026", "COMMENT=Zarrified, not copied")

    # ffmpeg's frames: mono and stereo, 16 and 24 bits
    mono16 = ffmpeg(signal(16000, 0.5, 1, 16, 1), 16000, 16, "-frame_size", "1024")
    info, frames = split(mono16)
    write("flac_mono_16.flac", flac(info, frames))
    stereo16 = ffmpeg(signal(44100, 0.25, 2, 16, 2), 44100, 16, "-frame_size", "2048")
    info, frames = split(stereo16)
    total = int.from_bytes(info[10:18], "big") & ((1 << 36) - 1)
    count = -(-total // 2048)
    offsets = frame_offsets(frames, count)
    points = [(j * 2048, offsets[j], 2048) for j in range(0, count, 3)]
    write("flac_stereo_16_seektable.flac", flac(info, frames, (SEEKTABLE, seektable(points, placeholders=2)),
                                                (VORBIS_COMMENT, comments), (PADDING, bytes(100))))
    info, frames = split(ffmpeg(signal(48000, 0.12, 2, 24, 3), 48000, 24, "-frame_size", "1152"))
    write("flac_stereo_24.flac", flac(info, frames, (VORBIS_COMMENT, comments)))
    info, frames = split(ffmpeg(signal(96000, 0.05, 1, 24, 4), 96000, 24, "-frame_size", "4096"))
    write("flac_mono_24.flac", flac(info, frames))
    # every block type: pictures (one of no data), an application block, two cue sheets, a reserved type
    info, frames = split(stereo16)
    art = picture(3, "image/png", "Front cover", png(), (4, 2, 24, 0))
    link = picture(4, "-->", "", b"https://example.org/back.png", (0, 0, 0, 0))
    empty = picture(0, "image/png", "no data", b"", (0, 0, 0, 0))
    write("flac_blocks.flac", flac(info, frames, (VORBIS_COMMENT, comments), (PICTURE, art), (PICTURE, link),
                                   (PICTURE, empty), (APPLICATION, b"riff" + b"WAVE fmt chunk"), (CUESHEET, cuesheet()),
                                   (CUESHEET, cuesheet()), (9, b"reserved"), (PADDING, b"")))
    # tags around the stream
    info, frames = split(mono16)
    write("flac_id3v2.flac", id3v2(b"TIT2\0\0\0\x05\0\0\x03Tone" + bytes(40)) + flac(info, frames))
    write("flac_id3v1.flac", flac(info, frames) + id3v1("A tone", "refkv", "Fixtures", "2026", "FLAC", 3, 12))
    # many frames (block size 16): two shards of 1024 frames
    info, frames = split(ffmpeg(signal(8000, 2.1, 1, 16, 5), 8000, 16, "-frame_size", "16"))
    write("flac_many_frames.flac", flac(info, frames))
    # one frame
    info, frames = split(ffmpeg(signal(22050, 0.01, 2, 16, 6), 22050, 16, "-frame_size", "4608"))
    write("flac_single_frame.flac", flac(info, frames))

    # frames written here: 8, 12 and 32 bits; stereo decorrelation; uncommon codes; codes that
    # defer to STREAMINFO
    write("flac_bitwise_8.flac", bitwise(signal(8000, 0.05, 1, 8, 7), 64, 8, 8000))
    write("flac_bitwise_12_side.flac", bitwise(signal(16000, 0.05, 2, 12, 8), 128, 12, 16000, assignment=8))
    write("flac_bitwise_20_mid.flac", bitwise(signal(32000, 0.02, 2, 20, 9), 160, 20, 32000, assignment=10))
    write("flac_bitwise_32.flac", bitwise(signal(44100, 0.01, 2, 32, 10), 192, 32, 44100, assignment=9))
    write("flac_bitwise_uncommon.flac", bitwise(signal(11025, 0.05, 1, 16, 11), 100, 16, 11025))
    write("flac_bitwise_streaminfo_codes.flac", bitwise(signal(22050, 0.05, 2, 16, 12), 256, 16, 22050,
                                                         rate_code=0, bits_code=0))

    # rejected
    info, frames = split(mono16)
    good = flac(info, frames)
    f0 = len(good) - len(frames)
    offsets = frame_offsets(frames, 8)
    write("edge_reject_flac_bad_sync.flac", good[:f0] + b"\xff\x00" + good[f0 + 2:])
    bad = bytearray(good)
    bad[f0 + 5] ^= 0x01  # frame 0's CRC-8 (its header is 6 bytes: a 1-byte number)
    write("edge_reject_flac_bad_crc8.flac", bytes(bad))
    bad = bytearray(good)
    bad[f0 + offsets[3] + 40] ^= 0x10  # a byte of frame 3's subframes
    write("edge_reject_flac_bad_crc16.flac", bytes(bad))
    write("edge_reject_flac_truncated.flac", good[:f0 + offsets[5] + 30])
    write("edge_reject_flac_truncated_frames.flac", good[:f0 + offsets[5]])
    write("edge_reject_flac_no_marker.flac", b"fLaX" + good[4:])
    write("edge_reject_flac_no_frames.flac", good[:f0])
    write("edge_reject_flac_streaminfo_not_first.flac", b"fLaC" + block(VORBIS_COMMENT, comments)
          + block(STREAMINFO, info, last=True) + frames)
    write("edge_reject_flac_streaminfo_size.flac", b"fLaC" + block(STREAMINFO, info[:30], last=True) + frames)
    outside = b"fLaC" + block(STREAMINFO, info) + bytes([0x81, 0x10, 0, 0]) + bytes(64)  # a 1 MiB block in 64 bytes
    write("edge_reject_flac_block_outside.flac", outside)
    write("edge_reject_flac_second_streaminfo.flac", flac(info, frames, (STREAMINFO, info)))
    write("edge_reject_flac_vorbis_comment.flac", flac(info, frames, (VORBIS_COMMENT, comments[:-3])))
    write("edge_reject_flac_picture.flac", flac(info, frames, (PICTURE, art[:-5])))
    write("edge_reject_flac_id3v2_outside.flac", b"ID3\x04\x00\x00" + syncsafe(100000) + good[:50])
    s = signal(8000, 0.05, 1, 16, 13)
    write("edge_reject_flac_variable_block_size.flac", bitwise(s, 64, 16, 8000, variable=True))
    two = frame(s[:64], 16, 0, rate=8000) + frame(s[64:128], 16, 1, rate=16000) + frame(s[128:192], 16, 2, rate=8000)
    write("edge_reject_flac_sample_rate_change.flac", flac(streaminfo(64, 64, 8000, 1, 16, 192), two))
    short = frame(s[:64], 16, 0, rate=8000) + frame(s[64:96], 16, 1, rate=8000) + frame(s[96:160], 16, 2, rate=8000)
    write("edge_reject_flac_short_frame.flac", flac(streaminfo(32, 64, 8000, 1, 16, 160), short))
    numbered = frame(s[:64], 16, 5, rate=8000) + frame(s[64:128], 16, 6, rate=8000)
    write("edge_reject_flac_frame_number.flac", flac(streaminfo(64, 64, 8000, 1, 16, 128), numbered))
    # a frame numbered wrongly after the first, the total unknown: it ends the frame before it
    skipped = b"".join(frame(s[64 * j:64 * j + 64], 16, 7 if j == 2 else j, rate=8000) for j in range(3))
    write("edge_reject_flac_frame_number_later.flac", flac(streaminfo(64, 64, 8000, 1, 16, 0), skipped))


if __name__ == "__main__":
    main()
