"""Writes synthetic MP4 and MOV files to fixtures/mp4/, covering the rules of the MP4 profile
(spec/zarrify/mp4.md §8
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp4.md#8-mp4-profile>): H.264 video
with and without B-frames, intra-only, odd sizes, a variable frame rate, long enough for
several chunks, groups of pictures of unequal lengths, open groups, two video tracks; HEVC
video, 8-bit and Main 10 (HLG, as an iPhone records it), in hvc1 and hev1 sample entries, with
open groups (CRA pictures and their RASL pictures), RADL pictures, and in a QuickTime file;
AAC and MP3 audio, alone and beside video; the moov box before and after the media data, a
64-bit mdat size and an mdat that runs to the end of the file; fragmented files, as ffmpeg's muxer
writes them and with each way a track fragment can say where its samples are; Motion JPEG of each
sample entry; HE-AAC; PCM audio of each kind of sample entry; tracks the profile does not zarrify (4:2:2
H.264, 4:4:4 HEVC and VP9) beside ones it does; and the inputs it rejects (`*_reject_*`). The byte-crafted edge
cases are write_edge_fixtures.py's.

The media are ffmpeg's test sources encoded by ffmpeg (libx264, libx265, its AAC encoder and
libmp3lame), with the bit-exact flags so that a build writes no version strings; some files
are then edited box by box. ffmpeg must be on PATH. The tests compare each zarrified track
with ffmpeg's own decoding of the file (through PyAV), so no expected values are stored.

Usage: uv run python fixtures/generators/mp4/write_fixtures.py
"""

# fixtures: on demand (it needs ffmpeg; `just fixtures` skips it)

from __future__ import annotations

import io
import shutil
import struct
import subprocess
import sys
from pathlib import Path

from PIL import Image

OUT = Path(__file__).parents[2] / "mp4"
CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"dinf", b"mvex", b"moof", b"traf"}


def video(size: str = "64x48", rate: int = 10, seconds: float = 3) -> list[str]:
    return ["-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}:duration={seconds}"]


def audio(rate: int = 16000, seconds: float = 3, channels: int = 1) -> list[str]:
    layout = "mono" if channels == 1 else "stereo"
    return ["-f", "lavfi", "-i", (f"sine=frequency=440:sample_rate={rate}:duration={seconds},"
            f"aformat=channel_layouts={layout}")]


X264 = ["-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p"]


def x265(params: str = "", pix_fmt: str = "yuv420p", tag: str = "hvc1") -> list[str]:
    """libx265, single-threaded, quiet and without its version SEI (so that a build of another
    version writes the same bytes), with x265's own `params`, in the sample entry `tag`
    (Apple's hvc1, ffmpeg's default for MP4 being hev1)."""
    return ["-c:v", "libx265", "-pix_fmt", pix_fmt, "-tag:v", tag,
            "-x265-params", ":".join(p for p in ("log-level=error:pools=1:frame-threads=1:info=0", params) if p)]


# x265's open groups, a CRA picture every 12 followed in decode order by RASL pictures presented
# before it, which reference the group before
OPEN_GOP = "keyint=12:min-keyint=12:scenecut=0:open-gop=1:bframes=3"
# HLG and BT.2020, as an iPhone's HDR video signals them
HLG = "colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc"
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
    # the byte-crafted edge_*.mp4 files are write_edge_fixtures.py's
    for old in list(OUT.glob("*.mp4")) + list(OUT.glob("*.mov")):
        if not old.name.startswith("edge_"):
            old.unlink()

    # H.264: B-frames in groups of 10 with the moov box first; the baseline profile (no
    # B-frames) with the moov box last; every frame a keyframe; a size that is not a multiple
    # of 16 (cropped); a variable frame rate; long enough for two chunks of 256 frames
    write("mp4_h264_bframes.mp4", ffmpeg(*video(), *X264, "-g", "10", "-bf", "2", "-movflags", "+faststart"))
    write("mp4_h264_baseline.mp4", ffmpeg(*video(), *X264, "-profile:v", "baseline", "-g", "5"))
    write("mp4_h264_intra.mp4", ffmpeg(*video(seconds=1.5), *X264, "-g", "1"))
    write("mp4_h264_odd_size.mp4", ffmpeg(*video("62x46"), *X264, "-g", "8", "-bf", "3"))
    write("mp4_h264_vfr.mp4",
          ffmpeg(*video(), "-vf", "settb=1/1000,setpts='N*0.1/TB+0.03*mod(N,3)/TB'", "-fps_mode", "passthrough",
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
    write("mp4_aac_pns.mp4",
          ffmpeg("-f", "lavfi", "-i", "anoisesrc=d=2:c=pink:r=16000:a=0.3", "-c:a", "aac", "-b:a", "24k"))
    write("mp4_h264_mp3.mp4", ffmpeg(*video(), *audio(), *X264, "-g", "10", "-c:a", "libmp3lame", "-b:a", "32k"))
    write("mov_h264_aac.mov", ffmpeg(*video(), *audio(), *X264, "-g", "10", "-bf", "2", *AAC, fmt="mov"))
    # Apple's encoder's composition offsets: a version 0 ctts box with negative ones (QuickTime's
    # are signed), the first picture's 0
    write("mov_h264_negative_ctts_aac.mov",
          negative_ctts(ffmpeg(*video(), *audio(), *X264, "-g", "10", "-bf", "2", *AAC, fmt="mov")))
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
    # HEVC: x265's defaults (open groups, B-frames) in an hev1 sample entry beside AAC; open
    # groups of 12 in hvc1; closed groups whose IDR pictures are followed by RADL pictures; a
    # size that is not a multiple of 8 (a conformance window); long enough for two chunks; Main
    # 10 with HLG; and an iPhone's .MOV: QuickTime's brand, hvc1, Main 10 HLG and AAC
    write("mp4_hevc_aac.mp4", ffmpeg(*video(), *audio(), *x265(tag="hev1"), "-g", "10", *AAC))
    write("mp4_hevc_open_gop.mp4", ffmpeg(*video(seconds=4), *x265(OPEN_GOP)))
    radl = "keyint=10:min-keyint=10:scenecut=0:open-gop=0:radl=2:bframes=3"
    write("mp4_hevc_radl.mp4", ffmpeg(*video(seconds=4), *x265(radl)))
    write("mp4_hevc_odd_size.mp4", ffmpeg(*video("62x46"), *x265("keyint=8:bframes=3")))
    write("mp4_hevc_long.mp4", ffmpeg(*video("32x32", 25, 16), *x265("keyint=50:min-keyint=50:scenecut=0:bframes=3")))
    write("mp4_hevc_main10.mp4", ffmpeg(*video(), *x265(f"{OPEN_GOP}:{HLG}", "yuv420p10le")))
    write("mov_hevc_main10_aac.mov",
          ffmpeg(*video(), *audio(), *x265(f"{OPEN_GOP}:{HLG}", "yuv420p10le"), *AAC, fmt="mov"))
    write("mov_hevc_aac.mov", ffmpeg(*video(), *audio(), *x265(OPEN_GOP), *AAC, fmt="mov"))
    # HE-AAC and HE-AAC v2, as AudioToolbox writes them (macOS only), and the same streams with the
    # hierarchical signaling; SBR that does not double the rate, and another AAC object type
    he = ("-c:a", "aac_at", "-profile:a", "4", "-b:a", "32k")
    he2 = ("-c:a", "aac_at", "-profile:a", "28", "-b:a", "24k")
    tone = audio(44100, 1.5, 2)
    he_aac = ffmpeg(*tone, *he)
    he_aac_v2 = ffmpeg(*tone, *he2)
    write("mp4_he_aac.mp4", he_aac)
    write("mp4_he_aac_v2.mp4", he_aac_v2)
    write("mp4_he_aac_v2_long.mp4", ffmpeg(*audio(44100, 10, 2), *he2))
    write("mp4_he_aac_hierarchical.mp4", audio_config(he_aac, bytes.fromhex("2b92088000")))
    write("mp4_he_aac_v2_hierarchical.mp4", audio_config(he_aac_v2, bytes.fromhex("eb8a0880000000")))
    beside_he = ffmpeg(*video(), *audio(44100, 1.5, 2), *X264, "-g", "10", *he)
    write("mp4_he_aac_h264.mp4", beside_he)
    write("mp4_he_aac_low_sbr_h264.mp4", audio_config(beside_he, bytes.fromhex("139056e598")))
    write("mp4_aac_ssr_h264.mp4", audio_config(beside_he, bytes.fromhex("1b9056e5a0")))
    # Motion JPEG: each sample entry; gray, progressive and CMYK streams, which ffmpeg's encoder does
    # not write; a first picture with its frame header past the first read; in fragments; and what
    # the profile does not read, beside AAC
    mjpeg = ("-c:v", "mjpeg", "-q:v", "5")
    short = video(seconds=1)
    beside = (*short, *audio(16000, 1), *mjpeg, *AAC)
    write("mov_mjpeg.mov", ffmpeg(*short, *mjpeg, fmt="mov"))
    write("mp4_mjpeg_aac.mp4", ffmpeg(*beside))
    write("mov_mjpa_444.mov", ffmpeg(*short, *mjpeg, "-pix_fmt", "yuvj444p", "-tag:v", "mjpa", fmt="mov"))
    write("mov_dmb1_422.mov", ffmpeg(*short, *mjpeg, "-pix_fmt", "yuvj422p", "-tag:v", "dmb1", fmt="mov"))
    write("mov_mjpeg_gray.mov", from_jpegs(jpegs("L")))
    write("mov_mjpeg_progressive.mov", from_jpegs(jpegs("RGB", progressive=True)))
    big = jpegs("RGB")
    big[0] = big[0][:2] + b"\xff\xe1" + struct.pack(">H", 20002) + bytes(20000) + big[0][2:]
    write("mov_mjpeg_late_header.mov", from_jpegs(big))
    write("mp4_frag_mjpeg.mp4", ffmpeg(*short, *mjpeg, "-movflags", "frag_keyframe+empty_moov"))
    mov = ffmpeg(*beside, fmt="mov")
    write("mov_mjpb_aac.mov", entry_type(mov, b"mjpb"))
    write("mov_mjpeg_fields_aac.mov", doubled_height(mov))
    write("mov_mjpeg_12bit_aac.mov", first_picture(mov, 12))
    write("mov_mjpeg_no_header_aac.mov", first_picture(mov, None))
    write("mov_reject_mjpeg_cmyk.mov", from_jpegs(jpegs("CMYK")))
    # PCM, alone and beside video: QuickTime's sample entries, a version 2 sound description past
    # 16 bits of sample rate, ISO/IEC 23003-5's, and QuickTime's own sample size of 1
    pcm16 = ffmpeg(*video(), *audio(8000, 1), *X264, "-g", "10", "-c:a", "pcm_s16le", fmt="mov")
    write("mov_pcm_s16le_h264.mov", pcm16)
    write("mov_pcm_s16be_stereo.mov", ffmpeg(*audio(8000, 1, 2), "-c:a", "pcm_s16be", fmt="mov"))
    write("mov_pcm_u8.mov", ffmpeg(*audio(8000, 1), "-c:a", "pcm_u8", fmt="mov"))
    write("mov_pcm_s8.mov", ffmpeg(*audio(8000, 1), "-c:a", "pcm_s8", fmt="mov"))
    write("mov_pcm_s32le.mov", ffmpeg(*audio(8000, 1, 2), "-c:a", "pcm_s32le", fmt="mov"))
    write("mov_pcm_s32be.mov", ffmpeg(*audio(8000, 1), "-c:a", "pcm_s32be", fmt="mov"))
    write("mov_pcm_f32le.mov", ffmpeg(*audio(8000, 1), "-c:a", "pcm_f32le", fmt="mov"))
    write("mov_pcm_f64be.mov", ffmpeg(*audio(8000, 1), "-c:a", "pcm_f64be", fmt="mov"))
    write("mov_pcm_96k_lpcm.mov", ffmpeg(*audio(96000, 0.1, 2), "-c:a", "pcm_s16le", fmt="mov"))
    write("mp4_pcm_s16le.mp4", ffmpeg(*video(), *audio(8000, 1), *X264, "-g", "10", "-c:a", "pcm_s16le"))
    write("mp4_pcm_f32be.mp4", ffmpeg(*audio(8000, 1, 2), "-c:a", "pcm_f32be"))
    write("mov_pcm_sample_size_1.mov", stsz_size(pcm16, "soun", 1))
    # fragmented: a moof box per keyframe beside AAC; each track in a moof box of its own, the data
    # offsets from the moof box; the first fragment's samples in the moov box's tables; DASH's sidx
    # boxes, with version 1 trun boxes of negative offsets; audio alone; and every way a track
    # fragment can say where its samples are
    empty = ("-movflags", "frag_keyframe+empty_moov")
    frag = ffmpeg(*video(), *audio(), *X264, "-g", "10", "-bf", "2", *AAC, *empty)
    write("mp4_frag_h264_aac.mp4", frag)
    write("mp4_frag_hevc_moof_base.mp4",
          ffmpeg(*video(), *audio(), *x265("keyint=10:min-keyint=10:scenecut=0"), *AAC,
                 "-movflags", "frag_keyframe+empty_moov+separate_moof+default_base_moof"))
    write("mp4_frag_hybrid.mp4", ffmpeg(*video(), *X264, "-g", "10", "-bf", "2", "-movflags", "frag_keyframe"))
    write("mp4_frag_dash.mp4",
          ffmpeg(*video(), *X264, "-g", "10", "-bf", "2", "-movflags",
                 "frag_keyframe+empty_moov+dash+global_sidx+negative_cts_offsets+default_base_moof"))
    write("mp4_frag_aac.mp4", ffmpeg(*audio(44100, 2, 2), *AAC, "-movflags", "empty_moov+frag_custom+frag_every_frame"))
    write("mp4_frag_pcm_h264.mp4",
          ffmpeg(*video(), *audio(8000, 1), *X264, "-g", "10", "-c:a", "pcm_s16le", *empty))
    # 8 packets of 1024 frames: a trun box without a table, its one size in the tfhd box
    write("mov_frag_pcm_s32be.mov", ffmpeg(*audio(8000, 1.024), "-c:a", "pcm_s32be", *empty, fmt="mov"))
    write("mp4_frag_offsets.mp4",
          refragmented(ffmpeg(*video(), *audio(), *X264, "-g", "7", "-bf", "2", *AAC, *empty)))
    # 10-bit H.264, HEVC of each subsampling and bit depth, AV1 (SVT-AV1 writes Main profile alone,
    # of at least 64 pixels a side) and VP9's four profiles
    write("mp4_h264_10bit_aac.mp4", ffmpeg(*video(), *audio(), "-c:v", "libx264", "-threads", "1",
                                           "-pix_fmt", "yuv420p10le", "-g", "10", *AAC))
    write("mp4_hevc_422_aac.mp4", ffmpeg(*video(), *audio(), *x265("keyint=10", "yuv422p10le"), *AAC))
    short = video(seconds=1)
    write("mp4_hevc_422_8bit.mp4", ffmpeg(*short, *x265("keyint=4:min-keyint=4", "yuv422p")))
    write("mp4_hevc_12bit.mp4", ffmpeg(*short, *x265("keyint=4:min-keyint=4", "yuv420p12le")))
    write("mp4_hevc_422_12bit.mp4", ffmpeg(*short, *x265("keyint=4:min-keyint=4", "yuv422p12le")))
    square = video("64x64", seconds=1)
    svt = ("-c:v", "libsvtav1", "-preset", "12", "-g", "4")
    write("mp4_av1_aac.mp4", ffmpeg(*square, *audio(16000, 1), *svt, "-pix_fmt", "yuv420p", *AAC))
    write("mp4_av1_10bit.mp4", ffmpeg(*square, *svt, "-pix_fmt", "yuv420p10le"))
    vpx = ("-c:v", "libvpx-vp9", "-threads", "1", "-cpu-used", "8", "-g", "4")
    write("mp4_vp9_aac.mp4", ffmpeg(*short, *audio(16000, 1), *vpx, "-pix_fmt", "yuv420p", *AAC))
    write("mp4_vp9_422.mp4", ffmpeg(*short, *vpx, "-pix_fmt", "yuv422p"))
    write("mp4_vp9_10bit.mp4", ffmpeg(*short, *vpx, "-pix_fmt", "yuv420p10le"))
    write("mp4_vp9_422_12bit.mp4", ffmpeg(*short, *vpx, "-pix_fmt", "yuv422p12le"))
    # tracks the profile does not zarrify, beside one it does
    write("mp4_hevc_444_aac.mp4", ffmpeg(*video(), *audio(), *x265("keyint=10", "yuv444p"), *AAC))
    write("mp4_h264_422_aac.mp4", ffmpeg(*video(), *audio(), "-c:v", "libx264", "-threads", "1",
                                         "-pix_fmt", "yuv422p", "-g", "10", *AAC))
    write("mp4_vp9_444_aac.mp4", ffmpeg(*short, *audio(16000, 1), *vpx, "-pix_fmt", "yuv444p", *AAC))
    write("mov_h264_pcm_s24.mov", ffmpeg(*video(), *audio(8000, 1), *X264, "-g", "10", "-c:a", "pcm_s24le", fmt="mov"))
    write("mov_h264_pcm_odd_size.mov", stsz_size(pcm16, "soun", 3))
    write("mov_h264_lpcm_not_interleaved.mov",
          lpcm_flags(ffmpeg(*video(), *audio(96000, 0.1), *X264, "-g", "10", "-c:a", "pcm_s16le", fmt="mov"), 0x20))

    # rejected: no moov box (cut before it), only tracks it does not zarrify (4:4:4 HEVC),
    # a box past the end of the file, a box smaller than its header, a second moov box, a
    # sample past the end of the file, tables that disagree on the sample count, and a fragmented
    # file with no trex box, a traf box with no tfhd box, a trun box whose table does not fit, a
    # trun box whose data starts before the file, and a fragment's sample past the end of the file
    o, _, _ = find(plain, "/moov")
    write("mp4_reject_no_moov.mp4", plain[:o])
    write("mp4_reject_hevc_444_only.mp4", ffmpeg(*video(), *x265("keyint=10", "yuv444p")))
    write("mov_reject_pcm_s24_only.mov", ffmpeg(*audio(8000, 0.5), "-c:a", "pcm_s24le", fmt="mov"))
    o, _, n = find(fast, "/moov")
    write("mp4_reject_box_outside.mp4", fast[:o] + struct.pack(">I", n + len(fast)) + fast[o + 4:])
    _f, _, fn = find(fast, "/ftyp")
    write("mp4_reject_box_too_small.mp4", fast[:fn] + struct.pack(">I4s", 4, b"free") + fast[fn:])
    o, _, n = find(plain, "/moov")
    write("mp4_reject_moov_twice.mp4", plain + plain[o:o + n])
    s, h, _ = find(plain, "/stco")
    write("mp4_reject_sample_outside.mp4", plain[:s + h + 8] + struct.pack(">I", len(plain) + 10) + plain[s + h + 12:])
    s, h, _ = find(plain, "/stts")
    (count,) = struct.unpack_from(">I", plain, s + h + 8)
    write("mp4_reject_stts_count.mp4", plain[:s + h + 8] + struct.pack(">I", count + 1) + plain[s + h + 12:])
    o, h, _ = find(frag, "/trex")
    write("mp4_reject_frag_no_trex.mp4", frag[:o + 4] + b"free" + frag[o + 8:])
    o, h, _ = find(frag, "/tfhd")
    write("mp4_reject_frag_no_tfhd.mp4", frag[:o + 4] + b"free" + frag[o + 8:])
    o, h, _ = find(frag, "/trun")
    write("mp4_reject_frag_trun_table.mp4", frag[:o + h + 4] + struct.pack(">I", 1 << 20) + frag[o + h + 8:])
    write("mp4_reject_frag_trun_before.mp4", frag[:o + h + 8] + struct.pack(">i", -(1 << 30)) + frag[o + h + 12:])
    write("mp4_reject_frag_sample_outside.mp4", frag_sample_outside(frag))


def audio_config(data: bytes, config: bytes) -> bytes:
    """The file with its AudioSpecificConfig replaced by `config`, of the same length."""
    o = data.index(b"esds")
    at = data.index(b"\x05\x80", o)  # the descriptor's tag, then its length's bytes
    while data[at + 1] & 0x80:
        at += 1
    start = at + 2
    assert data[start - 1] == len(config), (data[start - 1], len(config))
    return data[:start] + config + data[start + len(config):]


def jpegs(mode: str, **options: object) -> list[bytes]:
    """10 JPEG streams of a moving gradient, by Pillow, in the color mode `mode`."""
    out = []
    for k in range(10):
        image = Image.linear_gradient("L").resize((64, 48)).rotate(36 * k)
        stream = io.BytesIO()
        image.convert(mode).save(stream, "JPEG", quality=80, **options)
        out.append(stream.getvalue())
    return out


def from_jpegs(streams: list[bytes]) -> bytes:
    """A QuickTime file of the JPEG streams, each a sample as it is, at 10 pictures a second."""
    for k, stream in enumerate(streams):
        (OUT / f"_tmp{k}.jpg").write_bytes(stream)
    data = ffmpeg("-framerate", "10", "-i", str(OUT / "_tmp%d.jpg"), "-c:v", "copy", fmt="mov")
    for k in range(len(streams)):
        (OUT / f"_tmp{k}.jpg").unlink()
    return data


def entry_type(data: bytes, typ: bytes) -> bytes:
    """The file with its first sample entry of the type `typ`."""
    o, h, _ = find(data, "/stsd")
    return data[:o + h + 12] + typ + data[o + h + 16:]


def doubled_height(data: bytes) -> bytes:
    """The file with its first sample entry's height doubled, so that its JPEG pictures are half
    of it, as an interlaced picture's fields are."""
    o, h, _ = find(data, "/stsd")
    out = bytearray(data)
    at = o + h + 8 + 8 + 26  # the box's fields, the entry's header, the height's offset in it
    struct.pack_into(">H", out, at, 2 * struct.unpack_from(">H", data, at)[0])
    return bytes(out)


def first_picture(data: bytes, precision: int | None) -> bytes:
    """The file with its first sample's frame header of `precision` bits, or with no JPEG stream at
    its start (None)."""
    o, h, _ = find(data, "/stco")
    (first,) = struct.unpack_from(">I", data, o + h + 8)
    assert data[first:first + 2] == b"\xff\xd8", "the video's chunk is first"
    out = bytearray(data)
    if precision is None:
        out[first:first + 2] = bytes(2)
    else:
        sof = data.index(b"\xff\xc0", first)
        assert data[sof + 4] == 8
        out[sof + 4] = precision
    return bytes(out)


def box(typ: bytes, *payload: bytes) -> bytes:
    body = b"".join(payload)
    return struct.pack(">I4s", 8 + len(body), typ) + body


def fragment_samples(data: bytes, moof: int, size: int) -> list[dict]:
    """Each traf box of the moof box at `moof`: its track, base time, and each sample's duration,
    size, flags and composition offset, as ffmpeg's muxer writes them (a base data offset, a data
    offset and a table of durations, sizes and offsets at most, the first sample's flags and
    others' by default) with the defaults its tfhd box gives; and its data's offset."""
    trafs = []
    for name, t, th, tn in boxes(data, moof + 8, moof + size):
        if not name.endswith("/traf"):
            continue
        boxed = {b.rsplit("/", 1)[1]: (o, h) for b, o, h, _ in boxes(data, t + th, t + tn, "")}
        o, h = boxed["tfhd"]
        (flags, track) = struct.unpack_from(">II", data, o + h)
        flags &= 0xFFFFFF
        at = o + h + 8
        base = moof
        if flags & 1:
            (base,) = struct.unpack_from(">Q", data, at)
            at += 8
        defaults = {}
        for bit, field in ((2, "entry"), (8, "duration"), (0x10, "size"), (0x20, "flags")):
            if flags & bit:
                (defaults[field],) = struct.unpack_from(">I", data, at)
                at += 4
        o, h = boxed["tfdt"]
        time = struct.unpack_from(">Q" if data[o + h] == 1 else ">I", data, o + h + 4)[0]
        o, h = boxed["trun"]
        (rflags, count) = struct.unpack_from(">II", data, o + h)
        rflags &= 0xFFFFFF
        at = o + h + 8
        (offset,) = struct.unpack_from(">i", data, at)
        at += 4
        first = None
        if rflags & 4:
            (first,) = struct.unpack_from(">I", data, at)
            at += 4
        samples = []
        for j in range(count):
            sample = dict(defaults)
            for bit, field in ((0x100, "duration"), (0x200, "size"), (0x400, "flags"), (0x800, "cto")):
                if rflags & bit:
                    (sample[field],) = struct.unpack_from(">i" if field == "cto" else ">I", data, at)
                    at += 4
            if j == 0 and first is not None:
                sample["flags"] = first
            samples.append(sample)
        trafs.append({"track": track, "time": time, "data": base + offset, "samples": samples})
    return trafs


def tfhd(flags: int, track: int, *fields: bytes) -> bytes:
    return box(b"tfhd", struct.pack(">II", flags, track), *fields)


def tfdt(time: int, version: int = 0) -> bytes:
    return box(b"tfdt", struct.pack(">IQ", 1 << 24, time) if version else struct.pack(">II", 0, time))


ALL_MEMBERS = ("duration", "size", "flags", "cto")


def trun(samples: list[dict], *, offset: int | None = None, first_flags: int | None = None,
         members: tuple[str, ...] = ALL_MEMBERS) -> bytes:
    """A trun box of `samples`, its table of `members`."""
    bits = {"duration": 0x100, "size": 0x200, "flags": 0x400, "cto": 0x800}
    flags = sum(bits[m] for m in members) | (0 if offset is None else 1) | (0 if first_flags is None else 4)
    body = struct.pack(">II", flags, len(samples))
    if offset is not None:
        body += struct.pack(">i", offset)
    if first_flags is not None:
        body += struct.pack(">I", first_flags)
    for s in samples:
        for m in members:
            body += struct.pack(">i", s.get(m, 0)) if m == "cto" else struct.pack(">I", s.get(m, 0))
    return box(b"trun", body)


def traf_pair(way: int, v: dict, a: dict, durations: dict[int, int], at: int, data_at: int) -> tuple[tuple, tuple]:
    """The boxes of the traf boxes of `v` and `a`, in a moof box at `at` whose data is at `data_at`
    (`v`'s, then `a`'s), saying where their samples are in the way `way` (refragmented's)."""
    v_size = sum(s["size"] for s in v["samples"])
    if way == 0:
        half = max(1, len(v["samples"]) // 2)
        skip = sum(s["size"] for s in v["samples"][:half])
        return ((tfhd(1, v["track"], struct.pack(">Q", data_at)), tfdt(v["time"]),
                 trun(v["samples"][:half]), trun(v["samples"][half:], offset=skip)),
                (tfhd(1, a["track"], struct.pack(">Q", data_at + v_size)), tfdt(a["time"]), trun(a["samples"])))
    if way == 1:
        by_trex = all(s["duration"] == durations[a["track"]] for s in a["samples"])
        return ((tfhd(0, v["track"]), tfdt(v["time"]), trun(v["samples"], offset=data_at - at)),
                (tfhd(0, a["track"]), tfdt(a["time"]),
                 trun(a["samples"], offset=0, members=ALL_MEMBERS[1:] if by_trex else ALL_MEMBERS)))
    if way == 2:
        rest = v["samples"][1].get("flags", 0) if len(v["samples"]) > 1 else 0
        same = all(s.get("flags") == rest for s in v["samples"][1:])
        video = (trun(v["samples"], offset=data_at - at, first_flags=v["samples"][0]["flags"],
                      members=("duration", "size", "cto")) if same else trun(v["samples"], offset=data_at - at))
        sizes = {s["size"] for s in a["samples"]}
        one = sizes.pop() if len(sizes) == 1 else None
        size_flag, size_field = (0x10, (struct.pack(">I", one),)) if one is not None else (0, ())
        members = ("duration", "flags", "cto") if one is not None else ALL_MEMBERS
        return ((tfhd(0x20020, v["track"], struct.pack(">I", rest)), tfdt(v["time"], 1), video),
                (tfhd(0x20000 | size_flag, a["track"], *size_field), tfdt(a["time"], 1),
                 trun(a["samples"], offset=data_at + v_size - at, members=members)))
    return ((tfhd(0x20000, v["track"]), trun(v["samples"], offset=data_at - at)),
            (tfhd(0x20000, a["track"]), trun(a["samples"], offset=data_at + v_size - at)))


def refragmented(data: bytes) -> bytes:
    """`data`, a fragmented file of two tracks, each moof box with a traf box of each, its moof
    and mdat boxes written anew, each moof box's track fragments saying where their samples are
    in another of the ways ISO/IEC 14496-12 §8.8.7 and §8.8.8 give. Way 0: the tfhd box's base data
    offset and no data offset, its trun box split in two, the second's data offset its own (a run
    without one starts where the one before ended, which ffmpeg's demuxer does not read:
    tests/mp4.rs has that). Way 1: the moof box's start by default, the second traf box's where the
    first's data ended, and the second's durations the trex box's when they all are. Way 2:
    default-base-is-moof, a version 1 tfdt box, the first sample's flags its own and the others' the
    tfhd box's, the sizes by default when they are all one. Way 3: no tfdt box, the times following
    the fragment before's."""
    moofs = [(o, n) for name, o, _, n in boxes(data) if name == "/moof"]
    first = moofs[0][0]
    parsed = [fragment_samples(data, o, n) for o, n in moofs]
    assert all(len(p) == 2 for p in parsed), "a traf box of each track"
    # the trex boxes' durations hold each track's first sample's, so that a fragment can leave it out
    durations = {t["track"]: t["samples"][0]["duration"] for t in parsed[0]}
    head = bytearray(data[:first])
    for name, o, h, _ in boxes(data):
        if name.endswith("/trex"):
            (track,) = struct.unpack_from(">I", data, o + h + 4)
            struct.pack_into(">I", head, o + h + 12, durations[track])
    out = bytes(head)
    for k, (v, a) in enumerate(parsed):
        media = b"".join(data[t["data"]:t["data"] + sum(s["size"] for s in t["samples"])] for t in (v, a))
        at = len(out)
        data_at = at + 8
        for _ in range(2):  # the second time, knowing the moof box's length
            video, sound = traf_pair(k % 4, v, a, durations, at, data_at)
            moof = box(b"moof", box(b"mfhd", struct.pack(">II", 0, k + 1)), box(b"traf", *video), box(b"traf", *sound))
            data_at = at + len(moof) + 8
        out += moof + box(b"mdat", media)
    return out


def frag_sample_outside(data: bytes) -> bytes:
    """`data` with its first fragment's first sample's size made larger than the file."""
    o, h, _ = find(data, "/trun")
    (flags, _count) = struct.unpack_from(">II", data, o + h)
    assert flags & 0x200, "sizes in the table"
    at = o + h + 8 + 4 + (4 if flags & 4 else 0) + (4 if flags & 0x100 else 0)
    return data[:at] + struct.pack(">I", len(data) + 1) + data[at + 4:]


def track_of(data: bytes, handler: str) -> tuple[int, int]:
    """(offset, size) of the trak box whose hdlr box's handler is `handler`."""
    for name, t, _, n in boxes(data):
        if name.endswith("/trak") and handler.encode() in data[t:t + n]:
            o, h, _ = next((o, h, s) for b, o, h, s in boxes(data) if b.endswith("/hdlr") and t < o < t + n)
            if data[o + h + 8:o + h + 12] == handler.encode():
                return t, n
    raise ValueError(handler)


def stsz_size(data: bytes, handler: str, size: int) -> bytes:
    """`data` with the stsz box of the track of `handler` giving its samples' size as `size`."""
    t, n = track_of(data, handler)
    o, h, _ = next((o, h, s) for b, o, h, s in boxes(data) if b.endswith("/stsz") and t < o < t + n)
    return data[:o + h + 4] + struct.pack(">I", size) + data[o + h + 8:]


def lpcm_flags(data: bytes, flags: int) -> bytes:
    """`data`, whose first lpcm sample entry has `flags` added to its version 2 format flags (52
    bytes into its payload, 8 of box header before it)."""
    o = data.index(b"lpcm") - 4
    at = o + 8 + 52
    (old,) = struct.unpack_from(">I", data, at)
    return data[:at] + struct.pack(">I", old | flags) + data[at + 4:]


def irregular_groups() -> bytes:
    """60 pictures with B-frames whose groups start at pictures 0, 7, 9, 23 and 40: keyframes
    forced (as IDR pictures, closing each group) and none other, so that the profile's video
    chunks of whole groups have unequal lengths (a rectilinear grid)."""
    return ffmpeg(*video(seconds=6), *X264, "-bf", "2", "-x264-params",
                  "keyint=200:min-keyint=1:scenecut=0:forced-idr=1", "-force_key_frames", "0,0.7,0.9,2.3,4.0")


def negative_ctts(data: bytes) -> bytes:
    """`data` with its ctts box's offsets less the first, and the media_time of the edit of the
    track that holds it less the first too, so that each picture is presented when it was: the
    version 0 box then has negative offsets, as Apple's encoder writes it."""
    out = bytearray(data)
    o, h, _ = find(data, "/ctts")
    assert data[o + h] == 0, "a version 0 ctts box"
    (count,) = struct.unpack_from(">I", data, o + h + 4)
    (first,) = struct.unpack_from(">I", data, o + h + 12)
    for i in range(count):
        at = o + h + 12 + 8 * i
        (offset,) = struct.unpack_from(">I", data, at)
        struct.pack_into(">i", out, at, offset - first)
    t, _, n = next((t, th, tn) for name, t, th, tn in boxes(data) if name.endswith("/trak") and t < o < t + tn)
    e, eh, _ = next((e, eh, en) for name, e, eh, en in boxes(data) if name.endswith("/elst") and t < e < t + n)
    assert data[e + eh] == 0, "a version 0 elst box"
    at = e + eh + 12
    (media_time,) = struct.unpack_from(">i", data, at)
    struct.pack_into(">i", out, at, media_time - first)
    return bytes(out)


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
