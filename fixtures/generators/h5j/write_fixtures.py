"""Writes small synthetic Janelia H5J files to fixtures/h5j/, covering the H5J convention and
profile (spec/zarrify/h5j.md <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/h5j.md>):
the layout the public FlyLight files have (h5py's earliest file format, the root's and
/Channels's attributes, a contiguous uint8 dataset a channel holding an MP4 file), with channels
of 12-bit and 8-bit monochrome HEVC that libx265 encodes, padded and not, of one group of pictures
and of several; the channels the profile records rather than zarrifies (bytes that are no MP4
file, an MP4 file of 4:2:0 pictures, a chunked dataset, a dataset of another type); and the
inputs it rejects (`h5j_reject_*`).

python/tests/ir/test_h5j.py checks the zarrified files against ffmpeg's decoding of each
channel's MP4 file, cropped as the Fiji H5J loader crops it, and against h5py.

Usage: uv run python fixtures/generators/h5j/write_fixtures.py
"""

# fixtures: on demand (it needs ffmpeg with libx265; `just fixtures` skips it)

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import h5py
import numpy as np

OUT = Path(__file__).parents[2] / "h5j"

# Reproducible files: HDF5 stamps an object's header with its creation time unless its creation
# property list says not to (as fixtures/generators/hdf5/write_fixtures.py does).
_create = h5py.h5p.create


def _untimed(cls):
    plist = _create(cls)
    if isinstance(plist, (h5py.h5p.PropDCID, h5py.h5p.PropGCID)):
        plist.set_obj_track_times(False)
    return plist


h5py.h5p.create = _untimed


def mp4(size: str, frames: int, pix_fmt: str = "gray12le", x265: str = "bframes=2", source: str = "testsrc2") -> bytes:
    """An MP4 file of `frames` pictures of `size` that ffmpeg's libx265 encodes from its test
    source, single-threaded and without its version SEI (so that another build writes the same
    bytes), in an hev1 sample entry as the H5J writers' ffmpeg makes it."""
    params = ":".join(p for p in ("log-level=error:pools=1:frame-threads=1:info=0", x265) if p)
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "a.mp4"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-nostdin", "-f", "lavfi", "-i", f"{source}=size={size}:rate=25",
             "-frames:v", str(frames), "-c:v", "libx265", "-pix_fmt", pix_fmt, "-tag:v", "hev1", "-x265-params", params,
             "-fflags", "+bitexact", "-flags:v", "+bitexact", "-map_metadata", "-1", "-f", "mp4", str(out)],
            check=True, timeout=120)
        return out.read_bytes()


def write(name: str, channels: list[tuple[str, bytes | np.ndarray]], pad: tuple[int, int] | None,
          size: tuple[int, int, int], spec: str | None = None, userblock: int = 0, link: bool = False) -> None:
    """An H5J file of `channels` (name, the MP4 file's bytes or a dataset's array), `/Channels`'s
    padding `pad` (right, bottom; none: no such attributes) and `size` (width, height, frames), with
    the root's attributes the FlyLight files have; after a user block of `userblock` bytes, and with
    a root link `notes` to a file beside it when `link`."""
    w, h, frames = size
    with h5py.File(OUT / name, "w", libver="earliest", track_order=False, userblock_size=userblock) as f:
        if link:
            f["notes"] = h5py.ExternalLink("notes.h5", "/notes")
        f.attrs["channel_spec"] = np.bytes_(spec or "s" * (len(channels) - 1) + "r")
        f.attrs["unit"] = np.bytes_("micron")
        f.attrs["voxel_size"] = np.array([0.5189161, 0.5189161, 1.0])
        f.attrs["image_size"] = np.array([float(w), float(h), float(frames)])
        f.attrs["objective"] = np.bytes_("20x")
        f.attrs["sample_id"] = np.int64(2216633675703910498)
        g = f.create_group("Channels")
        for k, v in (("width", w), ("height", h), ("frames", frames)):
            g.attrs[k] = np.array([v], dtype=np.int64)
        if pad is not None:
            g.attrs["pad_right"] = np.array([pad[0]], dtype=np.int64)
            g.attrs["pad_bottom"] = np.array([pad[1]], dtype=np.int64)
        for k, (channel, data) in enumerate(channels):
            if isinstance(data, bytes):
                d = g.create_dataset(channel, data=np.frombuffer(data, dtype=np.uint8))
            else:
                d = g.create_dataset(channel, data=data, chunks=(len(data) // 2,) if k == 2 else None)
            d.attrs["content_type"] = np.bytes_("reference" if k == len(channels) - 1 else "signal")
    print(f"{name}: {(OUT / name).stat().st_size} bytes")


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for p in OUT.iterdir():
        p.unlink()
    # the public files' form: two channels of 12 bits, one group of pictures with B-frames, each
    # slice padded on the right and at the bottom (37 x 21 slices in 40 x 24 pictures)
    write("h5j_two_channels.h5j",
          [("Channel_0", mp4("40x24", 20)), ("Channel_1", mp4("40x24", 20, source="mandelbrot"))], (3, 3), (37, 21, 20))
    # 8 bits, no padding and no padding attributes
    write("h5j_8bit.h5j", [("Channel_0", mp4("32x16", 12, "gray"))], None, (32, 16, 12), spec="r")
    # 10 bits in groups of 8 pictures: chunks cut at sync samples, and a rectilinear grid when the
    # limit is less
    write("h5j_gray10_groups.h5j",
          [("Channel_0", mp4("24x16", 30, "gray10le", x265="keyint=8:min-keyint=8:scenecut=0:bframes=3"))],
          (1, 0), (23, 16, 30))
    # after a user block of 512 bytes: every address is 512 on from where the file's bytes are
    write("h5j_userblock.h5j", [("Channel_0", mp4("40x24", 10)), ("Channel_1", mp4("40x24", 10, source="mandelbrot"))],
          (3, 3), (37, 21, 10), userblock=512)
    # a root link to a file beside it, which the H5J profile does not read
    write("h5j_external_link.h5j", [("Channel_0", mp4("16x16", 6))], (0, 0), (16, 16, 6), link=True)
    # the channels recorded and not zarrified: bytes that are no MP4 file, an MP4 file of 4:2:0
    # pictures, a chunked dataset of bytes, a dataset of another type; beside one it zarrifies
    junk = b"not an MP4 file " * 64
    write("h5j_recorded.h5j",
          [("Channel_0", mp4("16x16", 6)), ("Channel_1", junk), ("Channel_2", np.frombuffer(junk, dtype=np.uint8)),
           ("Channel_3", mp4("16x16", 6, "yuv420p")), ("Channel_4", np.arange(16, dtype=np.uint16))],
          (0, 0), (16, 16, 6))
    # rejected: no channel the profile zarrifies; a padding as wide as the picture
    write("h5j_reject_no_channel.h5j", [("Channel_0", junk)], (0, 0), (16, 16, 6))
    write("h5j_reject_padding.h5j", [("Channel_0", mp4("16x16", 4))], (16, 0), (0, 16, 4))


if __name__ == "__main__":
    main()
