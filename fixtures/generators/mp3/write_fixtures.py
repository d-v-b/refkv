"""Writes synthetic MP3 files to fixtures/mp3/, covering the rules of the MP3 profile
(spec/virtualize/mp3.md §16
<https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp3.md#16-mp3-profile>): MPEG-1, MPEG-2
and MPEG-2.5 streams, mono and stereo, constant and variable bitrates, CRC protection, the bit
reservoir on and off, Xing, Info and LAME tags, ID3v2, ID3v1 and APE tags, junk between and
around frames, a truncated last frame, and the inputs the profile rejects (`mp3_reject_*`).

The audio is a deterministic signal (tones, a sweep and seeded noise, so that frames use the
bit reservoir) encoded by LAME (`lame`, with ffmpeg's libmp3lame for one file, and ffmpeg's
Layer II encoder for a rejected one): both must be on PATH. Some files are then edited byte by
byte (tags added, frames cut, junk inserted). For each accepted file without junk, the
expected samples are ffmpeg's decoding of its audio frames (an independent decoder) in
`fixtures/mp3/expected/<name>.npy`, int16 of shape [samples, channels]; the tests compare
the virtualized array with them within a small tolerance, and with miniaudio's sequential
decoding (the codec's own decoder) exactly. The files with junk have no expected samples:
decoders differ on what they do after junk.

`just fixtures` does not run this generator (it needs lame and ffmpeg, as NDPI's needs the
network): `just fixtures-of mp3` does.

Usage: uv run python fixtures/generators/mp3/write_fixtures.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

OUT = Path(__file__).parents[2] / "mp3"
EXPECTED = OUT / "expected"

# MPEG audio frame headers (spec/virtualize/mp3.md §2
# <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/mp3.md#2-frames>): version bits ->
# name; sample rates by version then index; Layer III bitrates (kbps) by version then index.
VERSIONS = {0: "MPEG-2.5", 2: "MPEG-2", 3: "MPEG-1"}
RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}
BITRATES = {
    3: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    2: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
BITRATES[0] = BITRATES[2]


def header(b: bytes) -> dict | None:
    """The fields of a Layer III frame header, or None when `b` does not start with one (a sync,
    a version that is not reserved, Layer III, a bitrate index from 1 to 14, a sample rate
    index from 0 to 2)."""
    if len(b) < 4 or b[0] != 0xFF or b[1] & 0xE0 != 0xE0:
        return None
    version, layer = (b[1] >> 3) & 3, (b[1] >> 1) & 3
    bitrate, rate, padding = b[2] >> 4, (b[2] >> 2) & 3, (b[2] >> 1) & 1
    if version == 1 or layer != 1 or bitrate in (0, 15) or rate == 3:
        return None
    crc = not b[1] & 1
    mode = b[3] >> 6
    channels = 1 if mode == 3 else 2
    kbps, sr = BITRATES[version][bitrate], RATES[version][rate]
    per = 144 if version == 3 else 72
    length = per * kbps * 1000 // sr + padding
    side = (17 if channels == 1 else 32) if version == 3 else (9 if channels == 1 else 17)
    return {"version": version, "rate": sr, "channels": channels, "crc": crc, "length": length,
            "side": side, "kbps": kbps, "mode": mode}


def id3v2_size(data: bytes, at: int = 0) -> int:
    """The whole size of the ID3v2 tag at `at`, footer included, or 0 if there is none."""
    if data[at:at + 3] != b"ID3" or len(data) < at + 10:
        return 0
    size = 0
    for c in data[at + 6:at + 10]:
        size = size << 7 | (c & 0x7F)
    return 10 + size + (10 if data[at + 5] & 0x10 else 0)


def frames_of(data: bytes) -> list[tuple[int, int, dict]]:
    """The chain of frames of an encoder's output: (offset, length, header) of each, from the
    first header after the ID3v2 tags, until the chain breaks or the file ends."""
    p = 0
    while (n := id3v2_size(data, p)):
        p += n
    out = []
    while (h := header(data[p:p + 4])) and p + h["length"] <= len(data):
        out.append((p, h["length"], h))
        p += h["length"]
    return out


def is_info(data: bytes, offset: int, h: dict) -> bool:
    """Whether the frame at `offset` is a Xing, Info or VBRI frame (§2)."""
    tag = offset + 4 + (2 if h["crc"] else 0) + h["side"]
    return data[tag:tag + 4] in (b"Xing", b"Info") or data[offset + 36:offset + 40] == b"VBRI"


def audio_region(data: bytes) -> tuple[int, int, int]:
    """(start, end, channels) of the audio frames: the chain's frames after an info frame."""
    frames = frames_of(data)
    if not frames:
        raise ValueError("no frames")
    if is_info(data, frames[0][0], frames[0][2]):
        frames = frames[1:]
    return frames[0][0], frames[-1][0] + frames[-1][1], frames[0][2]["channels"]


# ---- signals and encoders

def signal(rate: int, seconds: float, channels: int, seed: int) -> np.ndarray:
    """Deterministic audio: a tone, a sweep and noise per channel (int16, [samples, channels])."""
    n = int(rate * seconds)
    t = np.arange(n) / rate
    rng = np.random.default_rng(seed)
    out = np.zeros((n, channels), dtype=np.float64)
    for c in range(channels):
        f0 = 330.0 * (c + 1)
        sweep = np.sin(2 * np.pi * (f0 * t + (rate / 8) * t * t / (2 * seconds)))
        tone = np.sin(2 * np.pi * (f0 * 3.5) * t + c)
        noise = rng.standard_normal(n)
        out[:, c] = 9000 * sweep + 6000 * tone + 2500 * noise
    return np.clip(out, -32768, 32767).astype("<i2")


def lame(pcm: np.ndarray, rate: int, *args: str, name: str = "") -> bytes:
    """LAME's encoding of raw little-endian int16 `pcm` at `rate` Hz with `args`."""
    channels = pcm.shape[1]
    raw = Path(f"_{name or 'in'}.raw")
    out = Path(f"_{name or 'out'}.mp3")
    raw.write_bytes(pcm.tobytes())
    cmd = ["lame", "-r", "-s", f"{rate / 1000:g}", "--bitwidth", "16", "--signed", "--little-endian",
           "-m", "m" if channels == 1 else "j", "--quiet", "--noreplaygain", *args, str(raw), str(out)]
    subprocess.run(cmd, check=True)
    data = out.read_bytes()
    raw.unlink()
    out.unlink()
    return data


def ffmpeg_encode(pcm: np.ndarray, rate: int, codec: str, kbps: int) -> bytes:
    """ffmpeg's encoding of `pcm` with `codec` (libmp3lame writes an ID3v2 tag and an Info
    frame with a Lavc LAME tag, the frame only to a file it can seek back in; mp2 is Layer II)."""
    out = Path("_ffmpeg.mp3")
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "s16le", "-ar", str(rate), "-ac", str(pcm.shape[1]), "-i", "pipe:",
           "-c:a", codec, "-b:a", f"{kbps}k", "-f", "mp3" if codec == "libmp3lame" else "mp2", str(out)]
    subprocess.run(cmd, input=pcm.tobytes(), capture_output=True, check=True)
    data = out.read_bytes()
    out.unlink()
    return data


def ffmpeg_decode(audio: bytes, channels: int) -> np.ndarray:
    """ffmpeg's decoding of a run of MPEG audio frames (no tags: nothing is trimmed) to int16."""
    cmd = ["ffmpeg", "-v", "error", "-f", "mp3", "-i", "pipe:", "-f", "s16le", "-c:a", "pcm_s16le", "pipe:"]
    pcm = subprocess.run(cmd, input=audio, capture_output=True, check=True).stdout
    return np.frombuffer(pcm, dtype="<i2").reshape(-1, channels)


# ---- tags

def syncsafe(n: int) -> bytes:
    return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])


def id3v2(body: bytes, footer: bool = False, version: int = 4) -> bytes:
    """An ID3v2 tag holding `body` (padding is fine), with a footer when asked."""
    flags = 0x10 if footer else 0
    tag = b"ID3" + bytes([version, 0, flags]) + syncsafe(len(body)) + body
    if footer:
        tag += b"3DI" + bytes([version, 0, flags]) + syncsafe(len(body))
    return tag


def id3v1(title: str, artist: str, album: str, year: str, comment: str, track: int, genre: int) -> bytes:
    text = lambda s, n: s.encode("latin-1").ljust(n, b"\0")[:n]  # noqa: E731
    return (b"TAG" + text(title, 30) + text(artist, 30) + text(album, 30) + text(year, 4)
            + text(comment, 28) + bytes([0, track, genre]))


def ape(items: dict[str, str], with_header: bool = True) -> bytes:
    """An APEv2 tag with a footer and, when asked, a header."""
    body = b"".join(len(v.encode()).to_bytes(4, "little") + bytes(4) + k.encode() + b"\0" + v.encode()
                    for k, v in items.items())
    size = len(body) + 32

    def block(is_header: bool) -> bytes:
        flags = (0x80000000 if with_header else 0) | (0x20000000 if is_header else 0)
        return (b"APETAGEX" + (2000).to_bytes(4, "little") + size.to_bytes(4, "little")
                + len(items).to_bytes(4, "little") + flags.to_bytes(4, "little") + bytes(8))

    return (block(True) if with_header else b"") + body + block(False)


def raw_header(version: int, layer: int, bitrate: int, rate: int, channels: int, crc: bool = False,
               padding: int = 0) -> bytes:
    """A frame header from its fields (layer bits: 1 III, 2 II, 3 I)."""
    b1 = 0xE0 | version << 3 | layer << 1 | (0 if crc else 1)
    b2 = bitrate << 4 | rate << 2 | padding << 1
    b3 = (0xC0 if channels == 1 else 0x40)
    return bytes([0xFF, b1, b2, b3])


# ---- the files

def write(name: str, data: bytes, expected: bool = True) -> None:
    (OUT / f"{name}.mp3").write_bytes(data)
    if expected and not name.startswith("mp3_reject"):
        start, end, channels = audio_region(data)
        np.save(EXPECTED / f"{name}.npy", ffmpeg_decode(data[start:end], channels))
    print(f"{name}.mp3: {len(data)} bytes")


def main() -> None:
    for tool in ("lame", "ffmpeg"):
        if shutil.which(tool) is None:
            sys.exit(f"{tool} is not on PATH")
    OUT.mkdir(exist_ok=True)
    EXPECTED.mkdir(exist_ok=True)
    for old in list(OUT.glob("*.mp3")) + list(EXPECTED.glob("*.npy")):
        old.unlink()

    # MPEG-1 at 44.1 kHz: a stereo CBR stream without any tag, and a mono VBR one with LAME's
    # Xing frame (its frames differ in length)
    write("mp3_mpeg1_stereo_cbr", lame(signal(44100, 0.4, 2, 1), 44100, "-b", "128", "-t"))
    write("mp3_mpeg1_mono_vbr", lame(signal(44100, 0.5, 1, 2), 44100, "-V", "5"))
    # MPEG-2 at 22.05 and 16 kHz: CRC-protected stereo frames, and a mono stream whose encoder
    # did not use the bit reservoir
    crc = lame(signal(22050, 0.6, 2, 3), 22050, "-b", "48", "-p", "-t")
    write("mp3_mpeg2_stereo_crc", crc)
    write("mp3_mpeg2_mono_nores", lame(signal(16000, 0.7, 1, 4), 16000, "-b", "32", "--nores", "-t"))
    # MPEG-2.5 at 8 kHz: a short mono stream, and a long one of more than 256 frames (the
    # chunk size), whose expected samples would be large and which the tests decode themselves
    mono8 = lame(signal(8000, 1.0, 1, 5), 8000, "-b", "16", "-t")
    write("mp3_mpeg25_mono_8k", mono8)
    write("mp3_mpeg25_long", lame(signal(8000, 24.0, 1, 6), 8000, "-b", "8", "-t"), expected=False)
    # tags: LAME's ID3v2 and ID3v1, ID3v1 alone, an APE tag, ffmpeg's ID3v2 and Info frame, and
    # LAME's ABR Xing frame
    plain = lame(signal(16000, 0.7, 1, 7), 16000, "-b", "32", "-t")
    write("mp3_mpeg2_mono_16k", plain)
    text = ["--tt", "A title", "--ta", "An artist", "--tl", "An album", "--ty", "2021", "--tc", "Some comment",
            "--tn", "7", "--tg", "Blues"]
    # (the tag variants hold the frames of mp3_mpeg2_mono_16k, whose expected samples serve them)
    write("mp3_tags_id3v2_id3v1", lame(signal(16000, 0.7, 1, 7), 16000, "-b", "32", "-t", "--add-id3v2", *text),
          expected=False)
    write("mp3_tags_id3v1_only", lame(signal(16000, 0.7, 1, 7), 16000, "-b", "32", "-t", "--id3v1-only", *text),
          expected=False)
    write("mp3_tags_ape", plain + ape({"Title": "Ape title", "Artist": "Ape artist"}), expected=False)
    write("mp3_tags_ape_id3v1", plain + ape({"Title": "Ape title"}, with_header=False)
          + id3v1("V1 title", "V1 artist", "V1 album", "1999", "comment", 3, 17), expected=False)
    write("mp3_info_lavc", ffmpeg_encode(signal(16000, 0.7, 1, 8), 16000, "libmp3lame", 32))
    write("mp3_abr_xing", lame(signal(16000, 0.7, 1, 9), 16000, "--abr", "40"))
    # hand-made ID3v2 tags: one with a footer, two in a row, and padding after the tag
    write("mp3_id3v2_footer", id3v2(b"\0" * 40, footer=True) + plain, expected=False)
    write("mp3_id3v2_twice", id3v2(b"\0" * 16, version=3) + id3v2(b"\0" * 24) + plain, expected=False)
    write("mp3_id3v2_padding", id3v2(b"\0" * 16) + b"\0" * 64 + plain, expected=False)
    # the frames alone: one frame (which ffmpeg does not decode from a pipe), and CRC-protected
    # frames followed by plain ones, from two encodes of one signal without the bit reservoir
    # (so that no frame reaches into the other encode's)
    frames = frames_of(plain)
    write("mp3_single_frame", plain[frames[0][0]:frames[0][0] + frames[0][1]], expected=False)
    with_crc = lame(signal(22050, 0.6, 2, 3), 22050, "-b", "48", "-p", "--nores", "-t")
    without = lame(signal(22050, 0.6, 2, 3), 22050, "-b", "48", "--nores", "-t")
    crc_frames, plain_frames = frames_of(with_crc), frames_of(without)
    mixed = b"".join(with_crc[o:o + n] for o, n, _ in crc_frames[:5]) + b"".join(without[o:o + n] for o, n, _ in plain_frames[5:])
    write("mp3_crc_mixed", mixed)
    # junk: between frames, after the last frame, and a false sync (a header-like byte pair
    # followed by garbage) between frames; a truncated last frame
    o5, n5, _ = frames[5]
    junk = bytes([0x00, 0x01, 0x02, 0x7F, 0x10]) * 7
    write("mp3_junk_between_frames", plain[:o5 + n5] + junk + plain[o5 + n5:] + b"\x55" * 100, expected=False)
    false = b"\x00\x00" + raw_header(2, 1, 5, 2, 1) + b"\xaa" * 20
    write("mp3_junk_false_sync", plain[:o5 + n5] + false + plain[o5 + n5:], expected=False)
    last_o, last_n, _ = frames[-1]
    write("mp3_truncated_last_frame", plain[:last_o + last_n // 2], expected=False)
    write("mp3_id3v1_after_truncated", plain[:last_o + last_n // 2] + id3v1("t", "a", "b", "2000", "c", 1, 2),
          expected=False)

    # rejected: other layers, free format, a parameter change confirmed by the chain, no
    # frames, and bad ID3v2 tags
    write("mp3_reject_layer2", ffmpeg_encode(signal(16000, 0.5, 1, 10), 16000, "mp2", 32))
    layer1 = raw_header(3, 3, 4, 0, 2)  # MPEG-1 Layer I, 128 kbps, 44.1 kHz: 12 * 128000 / 44100 * 4 = 136 bytes
    write("mp3_reject_layer1", (layer1 + bytes(132)) * 3)
    write("mp3_reject_free_format", (raw_header(3, 1, 0, 0, 2) + bytes(412)) * 3)
    lo = lame(signal(16000, 0.5, 1, 11), 16000, "-b", "32", "-t")
    hi = lame(signal(22050, 0.5, 1, 11), 22050, "-b", "32", "-t")
    st = lame(signal(16000, 0.5, 2, 11), 16000, "-b", "48", "-t")
    m1 = lame(signal(32000, 0.5, 1, 11), 32000, "-b", "48", "-t")
    write("mp3_reject_sample_rate_change", lo + hi)
    write("mp3_reject_channels_change", lo + st)
    write("mp3_reject_version_change", lo + m1)
    l2 = ffmpeg_encode(signal(16000, 0.5, 1, 10), 16000, "mp2", 32)
    write("mp3_reject_layer_change", lo + l2)
    write("mp3_reject_no_frames", id3v2(b"\0" * 32) + bytes(200))
    write("mp3_reject_only_id3v1", id3v1("t", "a", "b", "2000", "c", 1, 2))
    write("mp3_reject_id3v2_outside", b"ID3\x04\x00\x00" + syncsafe(100000) + bytes(50))
    write("mp3_reject_id3v2_syncsafe", b"ID3\x04\x00\x00" + bytes([0, 0, 0x80, 0]) + bytes(50))
    write("mp3_reject_short", b"ID3")


if __name__ == "__main__":
    main()
