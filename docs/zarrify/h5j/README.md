# H5J

refkv turns a file into a Zarr hierarchy by writing a small archive of
references to the file's bytes, without copying the data
([overview](../README.md)). This page is about Janelia's H5J files.

## Who this is for

H5J is the format of Janelia Research Campus's published confocal stacks: the
FlyLight Split-GAL4 and Gen1 MCFO releases (the `janelia-flylight-imagery`
bucket on S3, tens of thousands of stacks), and what the Janelia Workstation
and VVD Viewer write. An H5J file is HDF5 whose channels are each an MP4 file
of monochrome HEVC, a picture a slice. Reading one takes Fiji's H5J loader, or
h5py and ffmpeg together; nothing reads it in place over HTTP.

refkv reads the HDF5 file and each channel's MP4 file, and writes an archive in
which every channel is an OME-NGFF image whose chunks are references to the
file's coded pictures, decoded when read. A 7.7 MB stack becomes a 36 KB
archive.

## What you get

- **An image a channel**, at the channel's dataset's name (`Channel_0`,
  `Channel_1`, …): OME-NGFF 0.5, one level, axes `z`, `y` and `x` scaled by the
  file's `voxel_size` in its `unit` (`micron` is `micrometer`), and an `omero`
  channel labeled by the dataset's `content_type` (`signal` or `reference`),
  its window the samples' whole range.
- **The slices**, array `0`: `uint16` samples from 0 to 4095 for the public
  files' 12-bit streams (`uint8` for 8 bits), the decoder's values, which are
  what Fiji's loader shows. The writer pads each slice to the encoder's block
  size; the array is the slice without the padding (`pad_right` and
  `pad_bottom`), as Fiji crops it.
- **Chunks** are runs of slices, each decoded by `refkv.stream` from the
  keyframe before it, at most 32 MiB of samples (24 slices of 1216 × 568).
  The public files have one or two keyframes, so a chunk deep in the stack
  decodes the slices before it too.
- **Source metadata:** the root's `refkv_zarrified.h5j` holds the file's
  attributes (`channel_spec`, `voxel_size`, `image_size`, `objective`,
  `alignment_space`, …) and `/Channels`'s, and lists every channel with its
  path or the reason it isn't presented; each image's holds its dataset's
  attributes and its stream's codec, profile, level and size. The whole file,
  each MP4 file's boxes included, is described on `refkv_source` by the IR
  mirror, from which it is rebuilt byte for byte.

**Not presented, and recorded instead:** a channel that is not an MP4 file of
one monochrome video track in a contiguous dataset of bytes. The HEVC streams
are lossy against the microscope's data: the arrays are the decoded stream,
which is all any H5J reader gives.

## Try it

[example.py](example.py) zarrifies a FlyLight brain stack from Janelia's S3
bucket and plots each channel's maximum intensity projection. Its dependencies
are in its header (PEP 723), so it runs on its own:

```bash
uv run docs/zarrify/h5j/example.py
```

The same stack with the CLI:

```bash
uv run refkv zarrify "https://s3.amazonaws.com/janelia-flylight-imagery/Split-GAL4+Omnibus+Broad/SS21347/SS21347-20151117_32_D4-m-20x-brain-Split_GAL4-JRC2018_Unisex_20x_HR-aligned_stack.h5j" flylight.vzip
```

```
{"format": "h5j", "channels": [{"name": "Channel_0", "path": "Channel_0/0", "frames": 174, "chunkFrames": 24, "chunks": 8}, {"name": "Channel_1", ...}], "skipped": [], ...}
```

Then, with `refkv[zarr,media]` installed:

```python
import zarr
import refkv.zarr

root = zarr.open_group(refkv.zarr.open("flylight.vzip"), mode="r", zarr_format=3)
stack = root["Channel_0/0"][...]  # (174, 566, 1210) uint16
```

## Viewing

The [demo](https://d-v-b.github.io/refkv-demo/image-to-zarr/?url=https%3A%2F%2Fs3.amazonaws.com%2Fjanelia-flylight-imagery%2FSplit-GAL4%2BOmnibus%2BBroad%2FSS21347%2FSS21347-20151117_32_D4-m-20x-brain-Split_GAL4-JRC2018_Unisex_20x_HR-aligned_stack.h5j) opens an H5J
file's channels in Neuroglancer: all of them together, each a layer in its
color (a signal channel green, the reference magenta), added together, and
then each alone. Its example is the FlyLight stack above, read from Janelia's
bucket, which serves CORS and range requests. The Neuroglancer fork decodes
`refkv.stream` HEVC chunks with libde265 built to wasm, in every browser and
exactly (the same samples as PyAV's decoding), and the page samples each
channel's contrast from the data with the same decoder, since the images'
omero window is the samples' whole range. Elsewhere, open the images with
zarr-python, napari or any OME-NGFF reader that runs the codec in Python.

## Supported and not supported

Supported:

- H5J files as FlyLight and the Workstation write them: HDF5 of any structure
  the HDF5 profile reads, `/Channels/Channel_<n>` datasets each holding an MP4
  file of one HEVC track of 4:0:0 pictures of 8, 10 or 12 bits, with B-frames
  and any number of keyframes, padded or not.

Rejected:

- Whatever the HDF5 profile rejects. A file with no channel to present is
  zarrified as HDF5, or rejected when named as an H5J file.
  [spec/zarrify/h5j.md §8.3](../../../spec/zarrify/h5j.md#83-checks) has the
  messages.

## Performance

- Zarrifying the example reads 210 KB of its 7.7 MB in 12 requests and 9
  rounds: the HDF5 structure, then each channel's MP4 file's first 64 KiB and
  its `moov` box. The pictures are never read.
- Reading a chunk is one range request of its pictures and their lead-in,
  joined; decoding 174 slices of 1216 × 568 takes about a second.
- The archive has one entry per chunk: 16 for the example's two channels.

## How it's verified

`python/tests/ir/test_h5j.py` zarrifies the synthetic files of
[fixtures/h5j/](../../../fixtures/h5j/) (h5py around streams libx265 encodes,
by [the generator](../../../fixtures/generators/h5j/write_fixtures.py)) and
compares every channel, voxel for voxel, with ffmpeg's decoding of its MP4
file cropped by the padding, as the Fiji loader crops it, in chunks of the
profile's size and of 7 slices; and every attribute with h5py. Four public
files of the bucket (two and four channels, padded and not, one and two
keyframes) match the same way. `python/tests/ir/test_h5j_stored.py` checks the
stored-array descriptions against h5py and ffmpeg's demuxer and decoder. The
Rust tests rebuild every fixture byte for byte from its mirror.

- Convention and profile: [spec/zarrify/h5j.md](../../../spec/zarrify/h5j.md)
- Rust core: [rust/refkv-ir/src/h5j.rs](../../../rust/refkv-ir/src/h5j.rs), the HDF5 parser's last phase, and the projection [rust/refkv-ir/src/project/h5j.rs](../../../rust/refkv-ir/src/project/h5j.rs)
- Fixtures: [fixtures/h5j/](../../../fixtures/h5j/)
- Design: [design/plans/h5j.md](../../../design/plans/h5j.md)
