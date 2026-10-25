"""Writes synthetic MP4 and MOV files to fixtures/mp4/, covering the rules of the MP4 profile
(spec/zarrify/mp4.md §8
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp4.md#8-mp4-profile>): H.264 video
with and without B-frames, intra-only, odd sizes, a variable frame rate, long enough for
several chunks, groups of pictures of unequal lengths, open groups, two video tracks; AAC and MP3 audio,
alone and beside video; the moov box before and after the media data, a 64-bit mdat size and an
mdat that runs to the end of the file; tracks the profile does not zarrify (HEVC, 10-bit
H.264) beside ones it does; and the inputs it rejects (`mp4_reject_*`).

The media are ffmpeg's test sources encoded by ffmpeg (libx264, libx265, its AAC encoder and
libmp3lame), with the bit-exact flags so that a build writes no version strings; some files
are then edited box by box. ffmpeg must be on PATH. The tests compare each zarrified track
with ffmpeg's own decoding of the file (through PyAV), so no expected values are stored.

Usage: uv run python fixtures/generators/mp4/write_fixtures.py
"""

# fixtures: on demand (it needs ffmpeg; `just fixtures` skips it)

from __future__ import annotations

import shutil
import struct
import subprocess
import sys
from pathlib import Path

OUT = Path(__file__).parents[2] / "mp4"
CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"dinf", b"mvex"}


def video(size: str = "64x48", rate: int = 10, seconds: float = 3) -> list[str]:
    return ["-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}:duration={seconds}"]


def audio(rate: int = 16000, seconds: float = 3, channels: int = 1) -> list[str]:
    layout = "mono" if channels == 1 else "stereo"
    return ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate={rate}:duration={seconds},"
            f"aformat=channel_layouts={layout}"]


X264 = ["-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p"]
AAC = ["-c:a", "aac", "-b:a", "32k", "-aac_pns", "0"]
EXACT = ["-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact", "-map_metadata", "-1"]


def ffmpeg(*args: str, fmt: str = "mp4") -> bytes:
    """ffmpeg's output for `args`, written to a seekable file (so that the muxer finishes the
    moov box) and read back."""
    out = OUT / "_tmp"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-nostdin", *args, *EXACT, "-f", fmt, str(out)],
                   check=True, timeout=120)
    data = out.read_bytes()
    out.unlink()
    return data


def boxes(data: bytes, start: int = 0, end: int | None = None, path: str = ""):
    """(path, offset, header length, size) of every box, depth first, descending into the
    containers and stsd."""
    end = len(data) if end is None else end
    p = start
    while p + 8 <= end:
        size, typ = struct.unpack_from(">I4s", data, p)
        head = 8
        if size == 1:
            (size,) = struct.unpack_from(">Q", data, p + 8)
            head = 16
        elif size == 0:
            size = end - p
        name = f"{path}/{typ.decode('latin-1')}"
        yield name, p, head, size
        if typ in CONTAINERS:
            yield from boxes(data, p + head, p + size, name)
        p += size


def find(data: bytes, suffix: str, k: int = 0) -> tuple[int, int, int]:
    """(offset, header length, size) of the k-th box whose path ends with `suffix`."""
    hits = [(o, h, s) for name, o, h, s in boxes(data) if name.endswith(suffix)]
    return hits[k]


def write(name: str, data: bytes) -> None:
    (OUT / name).write_bytes(data)
    print(f"{name}: {len(data)} bytes")


def main() -> None:
    if shutil.which("ffmpeg") is None:
        sys.exit("ffmpeg is not on PATH")
    OUT.mkdir(exist_ok=True)
    for old in list(OUT.glob("*.mp4")) + list(OUT.glob("*.mov")):
        old.unlink()

    # H.264: B-frames in groups of 10 with the moov box first; the baseline profile (no
    # B-frames) with the moov box last; every frame a keyframe; a size that is not a multiple
    # of 16 (cropped); a variable frame rate; long enough for two chunks of 256 frames
    write("mp4_h264_bframes.mp4", ffmpeg(*video(), *X264, "-g", "10", "-bf", "2", "-movflags", "+faststart"))
    write("mp4_h264_baseline.mp4", ffmpeg(*video(), *X264, "-profile:v", "baseline", "-g", "5"))
    write("mp4_h264_intra.mp4", ffmpeg(*video(seconds=1.5), *X264, "-g", "1"))
    write("mp4_h264_odd_size.mp4", ffmpeg(*video("62x46"), *X264, "-g", "8", "-bf", "3"))
    write("mp4_h264_vfr.mp4", ffmpeg(*video(), "-vf", "settb=1/1000,setpts='N*0.1/TB+0.03*mod(N,3)/TB'", "-fps_mode", "passthrough",
                                     "-enc_time_base", "1/1000", "-video_track_timescale", "1000", *X264, "-g", "9", "-bf", "2"))
    write("mp4_h264_long.mp4", ffmpeg(*video("32x32", 25, 16), *X264, "-g", "50", "-bf", "3"))
    write("mp4_h264_irregular_groups.mp4", irregular_groups())
    # open groups: each keyframe after the first a non-IDR I picture, followed in decode order
    # by two B-frames presented before it that reference the group before
    write("mp4_h264_open_gop.mp4", ffmpeg(*video(seconds=4), *X264, "-bf", "2",
                                          "-x264-params", "keyint=12:min-keyint=12:scenecut=0:open-gop=1"))
    write("mp4_h264_two_tracks.mp4", ffmpeg(*video(), "-filter_complex", "[0:v]split[a][b];[b]scale=32:24[c]",
                                            "-map", "[a]", "-map", "[c]", *X264, "-g", "6", "-bf", "1"))
    # audio, alone and beside video: AAC without noise substitution (whose noise a chunk
    # decodes from a generator of its own), AAC with it, and MP3
    write("mp4_h264_aac.mp4", ffmpeg(*video(), *audio(), *X264, "-g", "10", "-bf", "2", *AAC))
    write("mp4_aac_stereo.mp4", ffmpeg(*audio(44100, 2, 2), *AAC))
    write("mp4_aac_pns.mp4", ffmpeg("-f", "lavfi", "-i", "anoisesrc=d=2:c=pink:r=16000:a=0.3", "-c:a", "aac", "-b:a", "24k"))
    write("mp4_h264_mp3.mp4", ffmpeg(*video(), *audio(), *X264, "-g", "10", "-c:a", "libmp3lame", "-b:a", "32k"))
    write("mov_h264_aac.mov", ffmpeg(*video(), *audio(), *X264, "-g", "10", "-bf", "2", *AAC, fmt="mov"))
    # boxes: a 64-bit mdat size (the 8-byte free box ffmpeg writes before mdat, so that the
    # header can grow, and the mdat header, rewritten as one 16-byte header), an mdat that runs
    # to the end of the file (size 0), and a title in udta
    plain = ffmpeg(*video(), *X264, "-g", "10", "-bf", "2")
    o, _, n = find(plain, "/mdat")
    w = o - 8
    assert plain[w:w + 8] in (struct.pack(">I4s", 8, b"free"), struct.pack(">I4s", 8, b"wide")), "8 bytes to grow into"
    write("mp4_mdat_largesize.mp4", plain[:w] + struct.pack(">I4sQ", 1, b"mdat", n + 8) + plain[o + 8:])
    fast = ffmpeg(*video(), *X264, "-g", "10", "-bf", "2", "-movflags", "+faststart")
    o, _, n = find(fast, "/mdat")
    assert o + n == len(fast), "faststart puts mdat last"
    write("mp4_mdat_to_end.mp4", fast[:o] + struct.pack(">I", 0) + fast[o + 4:])
    write("mp4_udta_title.mp4", subprocess_title(*video(), *X264, "-g", "10"))
    # tracks the profile does not zarrify, beside one it does
    write("mp4_hevc_aac.mp4", ffmpeg(*video(), *audio(), "-c:v", "libx265", "-x265-params", "log-level=error:pools=1",
                                     "-g", "10", *AAC))
    write("mp4_h264_10bit_aac.mp4", ffmpeg(*video(), *audio(), "-c:v", "libx264", "-threads", "1",
                                           "-pix_fmt", "yuv420p10le", "-g", "10", *AAC))

    # rejected: fragmented, no moov box (cut before it), only tracks it does not zarrify,
    # a box past the end of the file, a box smaller than its header, a second moov box, a
    # sample past the end of the file, and tables that disagree on the sample count
    write("mp4_reject_fragmented.mp4", ffmpeg(*video(), *X264, "-g", "10", "-movflags", "frag_keyframe+empty_moov"))
    o, _, _ = find(plain, "/moov")
    write("mp4_reject_no_moov.mp4", plain[:o])
    write("mp4_reject_hevc_only.mp4", ffmpeg(*video(), "-c:v", "libx265", "-x265-params", "log-level=error:pools=1", "-g", "10"))
    o, _, n = find(fast, "/moov")
    write("mp4_reject_box_outside.mp4", fast[:o] + struct.pack(">I", n + len(fast)) + fast[o + 4:])
    f, _, fn = find(fast, "/ftyp")
    write("mp4_reject_box_too_small.mp4", fast[:fn] + struct.pack(">I4s", 4, b"free") + fast[fn:])
    o, _, n = find(plain, "/moov")
    write("mp4_reject_moov_twice.mp4", plain + plain[o:o + n])
    s, h, _ = find(plain, "/stco")
    write("mp4_reject_sample_outside.mp4", plain[:s + h + 8] + struct.pack(">I", len(plain) + 10) + plain[s + h + 12:])
    s, h, _ = find(plain, "/stts")
    (count,) = struct.unpack_from(">I", plain, s + h + 8)
    write("mp4_reject_stts_count.mp4", plain[:s + h + 8] + struct.pack(">I", count + 1) + plain[s + h + 12:])


def irregular_groups() -> bytes:
    """60 pictures with B-frames whose groups start at pictures 0, 7, 9, 23 and 40: keyframes
    forced (as IDR pictures, closing each group) and none other, so that the profile's video
    chunks of whole groups have unequal lengths (a rectilinear grid)."""
    return ffmpeg(*video(seconds=6), *X264, "-bf", "2", "-x264-params", "keyint=200:min-keyint=1:scenecut=0:forced-idr=1",
                  "-force_key_frames", "0,0.7,0.9,2.3,4.0")


def subprocess_title(*args: str) -> bytes:
    """ffmpeg's output with a title in udta (the bit-exact flags would drop it)."""
    out = OUT / "_tmp.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-nostdin", *args, "-fflags", "+bitexact", "-flags:v", "+bitexact",
                    "-metadata", "title=A test title", str(out)], check=True, timeout=120)
    data = out.read_bytes()
    out.unlink()
    return data


if __name__ == "__main__":
    main()
