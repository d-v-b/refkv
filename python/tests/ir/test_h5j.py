"""The H5J profile (spec/zarrify/h5j.md <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/h5j.md>)
against the readers of what an H5J file holds: each channel's array against ffmpeg's decoding of
its MP4 file (PyAV over the dataset's bytes, which h5py reads), cropped by `/Channels`'s padding as
the Fiji H5J loader crops it, voxel for voxel; the image's scale, unit and window, and every
attribute, against h5py; the channels the profile records rather than zarrifies; and the inputs
it rejects. The archive rebuilds the file from the mirror (ir_helpers.zarrified)."""

import io
import json
import re

import av
import h5py
import numpy as np
import pytest
from ir_helpers import FIXTURES, ROOT, core_setting
from ir_helpers import zarrified as shared_zarrified

from refkv import _core
from refkv._narrow import at, integer, mapping, ndarray, sequence, string
from refkv.ir import planner

FILES = sorted(p for p in (FIXTURES / "h5j").iterdir() if "reject" not in p.name)


def zarrified(path, tmp_path, chunk_frames=None):
    """(the output, the hierarchy's root group), at most `chunk_frames` pictures a chunk (None: the
    profile's 256)."""
    with core_setting(_core._set_chunk_frames, chunk_frames):
        out, root, _ir = shared_zarrified(path, tmp_path, "h5j")
    return out, root


def attribute(v):
    """An HDF5 attribute's value as the HDF5 convention writes it: a fixed-length string its text,
    an array its list, a number itself."""
    if isinstance(v, bytes):
        return v.decode()
    if isinstance(v, np.ndarray):
        return v.tolist()
    return v.item() if isinstance(v, np.generic) else v


def attributes(obj) -> dict:
    return {k: attribute(v) for k, v in obj.attrs.items()}


def decoded(data: bytes) -> np.ndarray:
    """ffmpeg's decoding of an MP4 file's video track: the luma plane of each picture, in the
    order the decoder outputs them, which is presentation order: [pictures, height, width]."""
    c = av.open(io.BytesIO(data), mode="r")
    frames = list(c.decode(video=0))
    wide = any(x.bits > 8 for x in frames[0].format.components)
    dtype = np.dtype("<u2") if wide else np.dtype(np.uint8)
    planes = [np.frombuffer(f.planes[0], dtype).reshape(f.height, -1)[:, :f.width] for f in frames]
    return np.stack(planes).astype(dtype.newbyteorder("="))


@pytest.mark.parametrize("chunk_frames", [None, 7])
@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_channels_are_ffmpegs_decoding(path, chunk_frames, tmp_path):
    """Every zarrified channel is ffmpeg's decoding of its MP4 file, less the padding, value for
    value, in chunks of the profile's size and of 7 pictures (several chunks, each with its
    lead-in); its image and source metadata are the file's, as h5py reads it."""
    out, root = zarrified(path, tmp_path, chunk_frames)
    meta = mapping(at(root.attrs, "refkv_zarrified", "h5j"))
    with h5py.File(path, "r") as f:
        group = f["Channels"]
        assert meta["attributes"] == attributes(f)
        assert meta["channels_attributes"] == attributes(group)
        pad_right = int(group.attrs.get("pad_right", [0])[0])
        pad_bottom = int(group.attrs.get("pad_bottom", [0])[0])
        voxel = [float(x) for x in f.attrs["voxel_size"]]
        listed = [mapping(c) for c in sequence(meta["channels"])]
        assert [c["name"] for c in listed] == sorted(group, key=lambda n: int(n.split("_")[1]))
        presented = [string(c["name"]) for c in listed if c["path"] is not None]
        assert presented == [at(c, "name") for c in sequence(out.summary["channels"])]
        assert presented
        for name in presented:
            d = group[name]
            data = d[()].tobytes()
            image = root.get_group(name)
            # the image: z, y, x in micrometer, the voxel size reversed, the window the samples'
            ome = mapping(image.attrs["ome"])
            (ms,) = sequence(ome["multiscales"])
            assert [(at(a, "name"), at(a, "unit")) for a in sequence(at(ms, "axes"))] == [
                ("z", "micrometer"), ("y", "micrometer"), ("x", "micrometer")]
            (ds,) = sequence(at(ms, "datasets"))
            assert at(ds, "path") == "0"
            assert at(ds, "coordinateTransformations") == [{"type": "scale", "scale": voxel[::-1]}]
            want = decoded(data)
            want = want[:, :want.shape[1] - pad_bottom, :want.shape[2] - pad_right]
            got = ndarray(image.get_array("0")[...])
            assert got.dtype == want.dtype
            assert got.shape == want.shape
            assert np.array_equal(got, want)
            bits = integer(at(image.attrs, "refkv_zarrified", "h5j", "track", "bit_depth"))
            assert bits == (8 if want.dtype == np.uint8 else 12)
            (channel,) = sequence(at(ome, "omero", "channels"))
            assert at(channel, "label") == d.attrs["content_type"].decode()
            assert at(channel, "window") == {"min": 0, "max": 2**bits - 1, "start": 0, "end": 2**bits - 1}
            # the channel's source metadata: its dataset, where its MP4 file is, its attributes
            own = mapping(at(image.attrs, "refkv_zarrified", "h5j"))
            assert own["dataset"] == f"Channels/{name}"
            assert (own["offset"], own["length"]) == (d.id.get_offset(), len(data))
            assert own["attributes"] == attributes(d)
            track = mapping(own["track"])
            height, width = integer(track["height"]) - pad_bottom, integer(track["width"]) - pad_right
            assert (integer(track["frames"]), height, width) == want.shape
            assert track["chroma"] == "4:0:0"
            # a chunk is whole coded pictures: the padding is past the array's end
            doc = json.loads(out.bytes_entries[f"{name}/0/zarr.json"])
            grid = doc["chunk_grid"]
            shapes = grid["configuration"]["chunk_shape" if grid["name"] == "regular" else "chunk_shapes"]
            assert shapes[1:] == [track["height"], track["width"]]
            assert doc["codecs"][0]["configuration"]["plane"] == "y"
            assert doc["dimension_names"] == ["z", "y", "x"]


def test_records_the_channels_it_does_not_zarrify(tmp_path):
    """A channel that is not an MP4 file of 4:0:0 pictures in a contiguous dataset of bytes is listed
    with the reason, and has no group (spec/zarrify/h5j.md §8.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/h5j.md#82-channels>)."""
    out, root = zarrified(FIXTURES / "h5j" / "h5j_recorded.h5j", tmp_path)
    listed = [mapping(c) for c in sequence(at(root.attrs, "refkv_zarrified", "h5j", "channels"))]
    assert listed == [
        {"name": "Channel_0", "path": "Channel_0", "reason": None},
        {"name": "Channel_1", "path": None,
         "reason": "its MP4 file: the 1852797984-byte an M box at 0 is outside its parent, which ends at 1024"},
        {"name": "Channel_2", "path": None, "reason": "a dataset of chunked storage, not contiguous"},
        {"name": "Channel_3", "path": None, "reason": "its pictures are 4:2:0, not 4:0:0 (monochrome)"},
        {"name": "Channel_4", "path": None, "reason": "a dataset of uint16 of shape [16], not bytes of one dimension"},
    ]
    assert sorted(root.group_keys()) == ["Channel_0", "refkv_source"]
    skipped = [at(s, "name") for s in sequence(out.summary["skipped"])]
    assert skipped == ["Channel_1", "Channel_2", "Channel_3", "Channel_4"]


def test_a_rectilinear_grid_cuts_chunks_at_sync_samples(tmp_path):
    """Groups of 8 pictures (sync samples presented at 0, 8, 16 and 24) in chunks of at most 7: each
    group split, its last picture a chunk of its own, and the last 7 pictures one chunk, since they
    fit one, so the grid along z is rectilinear (spec/zarrify/h5j.md §8.4
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/h5j.md#84-chunks>)."""
    out, _ = zarrified(FIXTURES / "h5j" / "h5j_groups.h5j", tmp_path, 7)
    doc = json.loads(out.bytes_entries["Channel_0/0/zarr.json"])
    assert doc["chunk_grid"] == {"name": "rectilinear",
                                 "configuration": {"kind": "inline", "chunk_shapes": [[7, 1, 7, 1, [7, 2]], 16, 24]}}
    assert doc["shape"] == [30, 16, 23]


def test_a_file_named_h5j_that_is_not_one_is_rejected():
    """An HDF5 file with no group Channels of links Channel_<n>, given as the H5J profile's input,
    is rejected (spec/zarrify/h5j.md §8.1
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/h5j.md#81-choosing-the-profile>)."""
    path = ROOT / "fixtures" / "hdf5" / "hdf5_latest.h5"
    t = planner.open_transport(str(path))
    run = planner.new_run("h5j", t)
    message = "not an H5J file: the root has no group Channels of links Channel_<n>"
    with pytest.raises(planner.Rejected, match=f"^{re.escape(message)}$"):
        planner.drive(run, t)
