# Implementation plan: the H5J profile

This plan adds an H5J convention and profile (`spec/zarrify/h5j.md`). H5J is the format Janelia
Research Campus's FlyLight and Workstation tools write for confocal stacks, and the Fiji
H5J loader reads (JaneliaSciComp/H5J_Loader_Plugin). It is a design note, not normative: the
document and the codec's section of [conventions §10](../../spec/conventions.md#10-coded-streams)
will state the decisions.

**Status.** Planned: the survey ([§1](#1-the-survey)) is done, and the decisions below are what the
implementation follows.

## 1. The survey

Four public files, all from the `janelia-flylight-imagery` bucket on S3, read whole with h5py and
PyAV (ffmpeg 8.0's decoders). Scripts in a scratch directory; every claim below is **[inspected]**
on all four unless it names one.

| file | channels | coded size | true size | frames | keyframes | HEVC level | `hvcC` bytes |
|---|---|---|---|---|---|---|---|
| `Split-GAL4 Omnibus Broad/SS21347/…_D4-m-20x-brain-…-JRC2018_Unisex_20x_HR-aligned_stack.h5j` (the example) | 2 (`sr`) | 1216 × 568 | 1210 × 566 | 174 | 1 | 3.1 | 1,991 and 1,992 |
| `…/SS21347/…_D1-m-20x-brain-Split_GAL4-unaligned_stack.h5j` | 2 (`sr`) | 1024 × 1024 | 1024 × 1024 | 130 | 1 | 4 | 1,991 and 1,992 |
| `…/SS21347/…_D1-m-20x-ventral_nerve_cord-…-JRC2018_VNC_Unisex_40x_DS-aligned_stack.h5j` | 2 (`sr`) | 576 × 1120 | 573 × 1119 | 219 | 1 | 3.1 | 1,991 and 1,992 |
| `Gen1 MCFO/R10A07/R10A07-20181121_61_G1-f-40x-central-GAL4-unaligned_stack.h5j` | 4 (`sssr`) | 688 × 688 | 686 × 685 | 406 | 2 | 3 | 1,990 or 1,991 |

### 1.1 The HDF5 layout

- Superblock version 0. The root group has one link, `Channels`, a group.
- The root's attributes: `channel_spec` (one letter a channel: `s` signal, `r` reference), `unit`
  (`micron`), `voxel_size` (three float64, x, y, z), `image_size` (three float64, x, y, z),
  `objective`, `area`, `owner`, `sample_id` (int64), and in aligned stacks `alignment_space`. The
  strings are fixed-length ASCII.
- `/Channels` has the attributes `width`, `height`, `frames`, `pad_right` and `pad_bottom`, each a
  one-element int64 array, and one link a channel, `Channel_0` to `Channel_<n−1>`.
- Each `Channel_<k>` is a dataset of one dimension, `uint8`, contiguous, no filters, with one
  attribute, `content_type` (`signal` or `reference`).
- **The dataset's bytes are a whole MP4 file**: `ftyp` (`isom`, minor 512, `isom iso2 mp41`), `free`,
  `mdat`, then `moov`, as ffmpeg's muxer writes them. Its chunk offsets are from the start of the
  dataset's bytes, so a sample's place in the H5J file is the dataset's address plus its offset.

### 1.2 The coded stream

- One track, `vide`, sample entry `hev1`, with an `hvcC` box of about 1,990 bytes (the VPS, SPS,
  PPS and x265's SEI; the table gives each file's).
- HEVC **Format Range Extensions** profile (general_profile_idc 4), at the level the picture size
  needs (the table), **4:0:0 (monochrome) of 12 bits**: ffmpeg decodes it to `gray12le`. So a channel's samples are integers from 0 to 4095.
- The stream has B-frames (two pictures of reordering) and few keyframes: one in the three
  two-channel files, two in the four-channel one. A chunk that starts past the first keyframe
  decodes from the keyframe before it ([conventions §10](../../spec/conventions.md#10-coded-streams)),
  so the last chunk of a 174-picture channel decodes every picture.
- The HEVC conformance window is empty: the decoded picture is the coded size.

### 1.3 Frames and z

The track has as many samples as `/Channels`'s `frames`, and as the third value of `image_size` in
the files whose `image_size` is right. Picture `k` in presentation order is slice `z = k`: the Fiji
loader takes the decoder's pictures in the order it outputs them, which is presentation order. The
time stamps (25 pictures a second, timescale 12800) mean nothing beyond that order.

### 1.4 Padding and the true size

The encoder needs a size that is a multiple of its block size, so the writer pads each slice on the
right and at the bottom, and records the padding in `/Channels`'s `pad_right` and `pad_bottom`. The
true size is the coded size less the padding: 1216 − 6 = 1210 and 568 − 2 = 566 in the example.
`/Channels`'s `width` and `height` hold it too.

**`image_size` is not reliable**: in the four-channel file it is 688 × 688, the coded size, while
`width` and `height` are 686 and 685. The Fiji loader reads neither: it crops the decoded picture by
`pad_right` and `pad_bottom` (`FijiAdapter.java`), and reads its calibration from `voxel_size` and
`unit`. The profile follows it: the true size is the coded size less the padding, and the other
three attributes are kept as source metadata.

### 1.5 The reference decoder

The Fiji loader decodes with ffmpeg (through JavaCPP), converts `gray12le` to `gray16be` with
swscale, then divides each sample by 16: the 12-bit value again. So the samples it shows are the
decoder's. ffmpeg's HEVC decoder through PyAV is the oracle, and it is exact (HEVC decoding is
exact by its standard), so the comparison is voxel for voxel. The stream itself is lossy against
the microscope's data; the comparison is with the decoded stream, not the original.

## 2. Findings that shaped the design

1. **The HDF5 profile already reads the file, but not the image.** It presents each channel as a
   one-dimensional `uint8` array of the MP4 file's bytes. Nothing in its IR describes the MP4: to
   the HDF5 parser those bytes are a dataset's data.
2. **A layout plugin can't add it.** A plugin builds from the base and the IR, with structure only
   ([layout.md §2](../../spec/layout.md#2-the-base)): it can't read the source's bytes, and the
   HDF5 IR holds no sample table, so it can't find the pictures.
3. **The MP4 profile already reads the channel's bytes,** but rejects the track: it presents HEVC of
   4:2:0 and 4:2:2 only.
4. **`refkv.stream` already decodes HEVC** through PyAV, and its decoder outputs `gray12le` for this
   stream; only its table of pixel formats lacks the monochrome ones.

## 3. Decisions

1. **A profile of its own, `h5j`, chosen by HDF5** as IMS is
   ([hdf5.md §8.1](../../spec/zarrify/hdf5.md#81-choosing-the-profile)): its output is an image,
   unlike the HDF5 profile's hdmf-zarr hierarchy, and finding the image means reading inside a
   dataset's bytes, which neither the HDF5 profile nor a layout plugin does (findings 1 and 2). An
   HDF5 file whose root has a link `Channels` to a group whose links, at least one, are each
   `Channel_<n>` is taken for an H5J file. The names are generic, so the choice is confirmed by
   the channels: a file the HDF5 profile chose this way with no channel to present is the HDF5
   profile's after all (decision 8). The H5J profile reads one file: a store run of HDF5's,
   following external links, reads its files as HDF5.
2. **The parser is the HDF5 parser, then an MP4 parse of each channel.** The HDF5 parser reads the
   file as it would any other: every object, the strict checks, the facts. Then the MP4 parser
   reads each channel dataset's bytes as a file of their own, its reads moved by the dataset's
   address. No HDF5 or MP4 structure is read twice, and neither parser changes but for being
   handed a window.
3. **The IR is the HDF5 IR with each channel's MP4 IR in place of its data.** Where the HDF5 IR
   has the channel's contiguous data as one `data` element, the H5J IR has a struct of the same
   extent, holding the MP4 IR's elements, their extents moved by the dataset's address. So the
   mirror holds the `moov` box as structure, and the samples are data. This takes a file-into-file
   graft, `Ir::graft` generalized from its store form. A channel the MP4 parser rejects keeps its
   `data` element, and is listed with the reason.
4. **The stored arrays are both:** every dataset's, as the HDF5 parser describes them, and each
   channel track's planes, as the MP4 parser does, their elements renumbered into the H5J IR.
5. **The image layout.** Each channel is an OME-NGFF image of one level, at its dataset's name
   (`Channel_0`), with the axes `z`, `y`, `x`, the scale `voxel_size` gives (z, y, x), in the unit
   `unit` gives (`micron` is `micrometer`), and an `omero` channel labeled by its
   `content_type`, of the window 0 to 2^bits − 1. Its one array, `0`, has the shape
   (frames, coded height − `pad_bottom`, coded width − `pad_right`), `uint16` for 10 or 12 bits
   and `uint8` for 8, and chunks of (pictures, coded height, coded width): the padding is past the
   array's end, in each chunk, so no codec crops anything and the grid stays regular in `y` and
   `x`. The pictures of a chunk follow the MP4 profile's rule
   ([mp4.md §4.1](../../spec/zarrify/mp4.md#41-video-tracks)): whole groups of pictures up to
   32 MiB of luma, a longer group split. Its codec is `refkv.stream` of format `hevc` and plane
   `y`.
6. **Monochrome HEVC, in `refkv.stream` and the MP4 profile.** A 4:0:0 picture has a Y plane
   alone: the codec's `plane` is `y`, and the MP4 profile presents a 4:0:0 track's `y` array alone.
   The Python codec adds ffmpeg's `gray`, `gray10le` and `gray12le`. This is the MP4 profile's gain
   too: a monochrome HEVC file is now presented, not refused.
7. **Source metadata.** The root's attributes and `/Channels`'s, as the HDF5 profile writes
   attributes, on the root; each channel's attributes and its track's facts on its image group.
8. **Record, don't reject.** A channel that is not a contiguous, unfiltered, one-dimensional
   `uint8` dataset, whose bytes the MP4 parser rejects, whose track the MP4 profile does not present,
   or whose padding is not less than its coded size, is listed on the root with the reason. A file
   with no channel to present is the HDF5 profile's when the HDF5 profile chose H5J for it, and
   rejected when it was named as the H5J profile's input, as an MP4 file with no track to present
   is.
9. **Fixtures.** A generator writes tiny H5J files with h5py (untimed, as the HDF5 fixtures are)
   holding MP4 files that ffmpeg and libx265 encode (`gray12le`, `info=0`): two channels with
   padding, one of 8 bits, one of 10 bits with several groups of pictures, one with channels that
   are not MP4 files, one after a user block (a nonzero base address), one with an external link,
   and the files of no channel to present. The MP4 fixtures gain 12-bit and 10-bit monochrome
   streams. It needs ffmpeg, so it is on demand. The 7.7 MB example is in the public corpus, not
   committed.

## 4. Order of work

1. Monochrome HEVC in `refkv.stream` and the MP4 profile, with an MP4 fixture of it. Gives: a
   channel's bytes, extracted, zarrify.
2. The file-into-file graft and the H5J parser, its projection, the registry entry, the document,
   the stored-array paragraph and the schema module.
3. Fixtures, `test_h5j.py` (the arrays against ffmpeg's decode, voxel for voxel, and every metadata
   value against h5py) and `test_h5j_stored.py`.
4. The docs page, the example script, the corpus line.
5. Later, in a session of its own: the Neuroglancer fork. Its `refkv.stream` decoder takes 4:2:0
   and 4:2:2 pictures, not 4:0:0, and decodes HEVC with WebCodecs only where the platform has a
   decoder for it; range extensions decoders are rare.

## 5. Open questions

1. **One array of channels, or an image a channel?** OME-NGFF wants one array with a `c` axis. A
   `refkv.stream` chunk decodes one stream into rows along the first axis, so a `c` axis would need
   the codec to place its rows under a leading axis of one. Recommended: an image a channel now; a
   codec member later if viewers need the one array.
2. **Should `width`, `height` and `frames` be checked against the stream?** They agree on all four
   files, and the Fiji loader ignores them. Recommended: keep them as source metadata, and check
   nothing.
