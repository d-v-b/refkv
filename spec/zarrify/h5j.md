# H5J

The Zarr layout of a Janelia H5J file (`.h5j`): an HDF5 file whose channels are each an MP4 file of
monochrome HEVC, a picture a slice of a confocal stack, as Janelia's FlyLight project and the
Janelia Workstation write them and the Fiji H5J loader (JaneliaSciComp/H5J_Loader_Plugin) reads
them. What all of refkv's conventions share is in [spec/conventions.md](../conventions.md), cited
here as "conventions §n". How refkv produces this layout as a reference store, and which files it
accepts, is the H5J profile, [Part 2](#part-2-the-profile) below.

Convention version: 0 (until release, [conventions §1](../conventions.md#1-conventions)) · UUID: `5bd31fac-d92d-452b-bcb6-15ca89c0f931` ·
Schema: [schema.json](h5j/schema.json)

Changes: [changes/h5j/](changes/h5j/), one file per change.

Added 2026-11-19.

This document has two parts ([conventions §11.1](../conventions.md#111-parts)).

Each channel is an OME-NGFF image of one level. Its pictures stay in the file as coded HEVC
pictures; the array's codec, `refkv.stream` ([conventions §10](../conventions.md#10-coded-streams)),
decodes them when a chunk is read. The HEVC stream is lossy against the microscope's data: the
arrays are the decoded stream, as every H5J reader gives it. The survey of public files and the
design are in [design/plans/h5j.md](../../design/plans/h5j.md).

# Part 1. The convention

## Layout

The hierarchy's entries ([conventions §11.5](../conventions.md#115-layout-tables)):

| prefix | type | structure | description |
|---|---|---|---|
| `zarr.json` | group | [`GroupDocument`](../conventions.md#type-groupdocument), its attributes a [`Declaration`](../conventions.md#type-declaration) and nothing else | the root ([§4](#4-the-layout)): no `ome` member |
| `zarr.json#/attributes/zarr_conventions` | attribute | array of [`ConventionMetadata`](../conventions.md#type-conventionmetadata) | the CMO of [§1](#1-declaration) |
| `zarr.json#/attributes/refkv_zarrified` | attribute | [`RootProperty`](../conventions.md#type-rootproperty) | `profile` `"h5j"`, `version` `0`, `source.url` the file's URL |
| `zarr.json#/attributes/refkv_zarrified/h5j` | attribute | [`H5jRoot`](#type-h5jroot) | the root's source metadata ([§5.1](#51-the-root)) |
| `<name>/zarr.json` | group | [`ImageGroup`](../conventions.md#type-imagegroup) | a channel the profile zarrifies: its image ([§4.1](#41-a-channel), [§8.2](#82-channels)) |
| `<name>/zarr.json#/attributes/ome` | attribute | [`OmeImage`](../conventions.md#type-omeimage) | one level, the array `0`, with the axes, scale and `omero` channel of [§4.1](#41-a-channel) |
| `<name>/zarr.json#/attributes/zarr_conventions` | attribute | array of [`ConventionMetadata`](../conventions.md#type-conventionmetadata) | the CMO of [§1](#1-declaration) |
| `<name>/zarr.json#/attributes/refkv_zarrified` | attribute | [`NodeProperty`](../conventions.md#type-nodeproperty) | the channel's property |
| `<name>/zarr.json#/attributes/refkv_zarrified/h5j` | attribute | [`H5jChannel`](#type-h5jchannel) | the channel's source metadata ([§5.2](#52-a-channel)) |
| `<name>/0/zarr.json` | array | [`H5jArray`](#type-h5jarray) | the channel's slices ([§4.1](#41-a-channel)) |
| `<name>/0/c/<q>/0/0` | chunk | [`H5jChunk`](#type-h5jchunk) | every chunk of the slices ([§8.4](#84-chunks)) |
| `refkv_source/**` | subtree | [the mirror's entries](../conventions.md#table-mirror) | the IR mirror ([conventions §11.3](../conventions.md#113-the-ir-mirror)): its elements are [§5.3](#53-elements)'s |

| placeholder | value |
|---|---|
| `<name>` | the channel's dataset's name, `Channel_<n>` |
| `<q>` | the chunk's index along `z`, in decimal, from 0 |

## 1. Declaration

The root, each channel's group and `refkv_source` declare the convention
([conventions §11.2](../conventions.md#112-declaration)). Its CMO is:

```json
{
  "uuid": "5bd31fac-d92d-452b-bcb6-15ca89c0f931",
  "schema_url": "https://raw.githubusercontent.com/d-v-b/refkv/refs/heads/main/spec/zarrify/h5j/schema.json",
  "spec_url": "https://github.com/d-v-b/refkv/blob/main/spec/zarrify/h5j.md",
  "name": "refkv_zarrified",
  "description": "The Zarr layout of a H5J source zarrified by refkv, and the source's metadata"
}
```

## 2. The source

An H5J file is an HDF5 file ([hdf5.md](hdf5.md) reads it as one) with this layout, as the writers
of the public files make it:

| object | what it holds |
|---|---|
| the root group | the attributes `channel_spec` (a letter a channel: `s` signal, `r` reference), `unit` (`micron`), `voxel_size` (three numbers: x, y, z), `image_size` (three numbers: x, y, z), and others the writer adds (`objective`, `area`, `owner`, `sample_id`, `alignment_space`) |
| `/Channels` | a group, with the integer attributes `width`, `height`, `frames`, `pad_right` and `pad_bottom`, each an array of one element |
| `/Channels/Channel_<n>` | a channel: a contiguous dataset of one dimension of `uint8`, with the attribute `content_type` (`signal` or `reference`), whose bytes are a whole MP4 file ([mp4.md](mp4.md)) of one HEVC video track |

The MP4 file's offsets are from the start of the dataset's bytes, so a sample at offset `o` of the
MP4 file is at `a + o` of the H5J file, `a` the dataset's address (with the base address,
[hdf5.md §8.2](hdf5.md#82-reading)).

**The pictures.** The track's pictures are 4:0:0 (monochrome) HEVC of 8, 10 or 12 bits; the
public files' are 12 bits, HEVC's range extensions profile. Picture `k` in presentation order
([mp4.md §4.1](mp4.md#41-video-tracks)) is slice `z = k`, as the Fiji loader takes the decoder's
pictures in the order it outputs them.

**The padding.** An encoder codes a picture of a size that is a whole number of its blocks, so the
writer pads each slice on the right and at the bottom, and records the padding in `/Channels`'s
`pad_right` and `pad_bottom`. A slice is the coded picture, `W` by `H` (the sample entry's `width`
and `height`), less the padding: `W − pad_right` by `H − pad_bottom`, the columns and rows from the
picture's top left. This is the Fiji loader's rule. The root's `image_size` is not: in a public
file it holds the coded size where `/Channels`'s `width` and `height` hold the slice's
([design/plans/h5j.md §1.4](../../design/plans/h5j.md#14-padding-and-the-true-size)). Both are
kept as source metadata ([§5](#5-source-metadata)) and neither is read.

## 3. Data types

A slice's samples are the decoder's ([conventions §10](../conventions.md#10-coded-streams)):
`uint8` for 8 bits, `uint16` for 10 or 12, from 0 to 2^D − 1 (`D` the bit depth), not scaled.

## 4. The layout

The root is a group with no image of its own. Each channel the profile zarrifies
([§8.2](#82-channels)) is an image at its dataset's name.

### 4.1 A channel

A channel of `N` pictures of `W` by `H` coded samples of `D` bits, with the padding `pad_right`
and `pad_bottom` (each 0 when `/Channels` lacks it), is an image of one array, `0`, its axes `z`,
`y` and `x`.

**The image.** Its `ome` ([`OmeImage`](../conventions.md#type-omeimage), OME-NGFF 0.5) has one
multiscale: `name` the channel's name, the axes `z`, `y` and `x` of type `space`, and one dataset,
`0`, of one scale transformation:

- the scale is `[v_z, v_y, v_x]` when the root's `voxel_size` is three numbers `[v_x, v_y, v_z]`,
  each finite and greater than 0; else `[1, 1, 1]`;
- each axis has the unit of the root's `unit` when it is one of `micron`, `microns`,
  `micrometer`, `um`, `µm` (`"micrometer"`), `nanometer`, `nm` (`"nanometer"`), `millimeter` or
  `mm` (`"millimeter"`); else no unit ([conventions §5](../conventions.md#5-units)).

Its `omero` has one channel: `label` the dataset's `content_type` attribute when it is text, else
the channel's name; `color` `"FFFFFF"`; `active` `true`; and the window `0` to `2^D − 1` shown
whole.

<a id="type-h5jarray"></a>**`H5jArray`**, the `zarr.json` of `0`: an
[`ArrayDocument`](../conventions.md#type-arraydocument) with these members, `L_q` the length of
chunk `q` ([§8.4](#84-chunks)):

| member | type | presence | value |
|---|---|---|---|
| `shape` | array of integer | required | `[N, H − pad_bottom, W − pad_right]` |
| `data_type` | string | required | `"uint8"` when `D` is 8, `"uint16"` when it is 10 or 12 |
| `chunk_grid` | object | required | when every `L_q` but the last is `L_0` and the last at most `L_0`, the regular grid of chunk shape `[L_0, H, W]`; otherwise the zarr-extensions `rectilinear` grid, exactly `{"name": "rectilinear", "configuration": {"kind": "inline", "chunk_shapes": [T, H, W]}}`, `T` written as [mp4.md §4.1](mp4.md#41-video-tracks) writes it |
| `codecs` | array of object | required | `[{"name": "refkv.stream", "configuration": {"format": "hevc", "setup": S, "plane": "y"}}]`, a [`StreamConfiguration`](../conventions.md#type-streamconfiguration), `S` the base64 of the payload of the sample entry's `hvcC` box |
| `dimension_names` | array of string | required | `["z", "y", "x"]` |
| `fill_value` | integer | required | `0` |
| `attributes` | object | required | `{}` |

A chunk is `[L_q, H, W]`, the coded pictures whole: the padding's columns and rows are past the
array's end along `y` and `x`, so a reader drops them, and no codec crops a picture.

<a id="type-h5jchunk"></a>**`H5jChunk`**: a `refkv.stream` chunk
([`StreamChunk`](../conventions.md#type-streamchunk)) of the channel's samples, as
[§8.4](#84-chunks) gives them.

## 5. Source metadata

The HDF5 attributes are written as the HDF5 convention writes them
([hdf5.md §6](hdf5.md#6-attributes)).

### 5.1 The root

<a id="type-h5jroot"></a>**`H5jRoot`**, the root's source metadata:

| member | type | presence | value |
|---|---|---|---|
| `attributes` | object | required | the root group's attributes |
| `channels_attributes` | object | required | `/Channels`'s attributes |
| `channels` | array of [`H5jChannelListing`](#type-h5jchannellisting) | required | each channel, in order of `n` |

<a id="type-h5jchannellisting"></a>**`H5jChannelListing`**, a channel:

| member | type | presence | value |
|---|---|---|---|
| `name` | string | required | its dataset's name |
| `path` | string or null | required | its group's path when the profile zarrifies it; else null |
| `reason` | string or null | required | null when the profile zarrifies it; else why not ([§8.2](#82-channels)) |

### 5.2 A channel

<a id="type-h5jchannel"></a>**`H5jChannel`**, a channel group's source metadata:

| member | type | presence | value |
|---|---|---|---|
| `dataset` | string | required | `Channels/<name>` |
| `offset` | integer | required | where its dataset's bytes, the MP4 file, start in the H5J file |
| `length` | integer | required | the dataset's bytes |
| `attributes` | object | required | the dataset's attributes |
| `brand` | object or null | required | the MP4 file's brands, as [mp4.md §5.1](mp4.md#51-the-root)'s `brand`, or null without an `ftyp` box |
| `movie_timescale` | integer | required | the MP4 file's `mvhd` box's timescale |
| `movie_duration` | [`Integer`](../conventions.md#type-integer) | required | its `mvhd` box's duration |
| `track` | object | required | the track, as [mp4.md §5.2](mp4.md#52-the-tracks) gives a video track's `id`, `codec`, `format`, `timescale`, `duration`, `edits`, `width`, `height`, `chroma` (`"4:0:0"`), `bit_depth`, `profile`, `level` and `sync_samples`, and `frames`, its pictures |
| `chunk_frames` | integer | required | `F` of [§8.4](#84-chunks) |

### 5.3 Elements

The IR ([conventions §8](../conventions.md#8-the-ir-mirror)) is the HDF5 file's IR
([hdf5.md §7.3](hdf5.md#73-the-ir-mirror)), but that the contiguous data of each channel the
profile zarrifies is not one `data` element: it is a struct `data` of the same extent whose
children are the elements of its MP4 file's IR ([mp4.md §5.3](mp4.md#53-elements)) but that file's
root, each extent moved `a` bytes on (`a` where the dataset's bytes start). So the mirror holds the
MP4 file's boxes, and its samples are `data` elements, `data/samples/<T>/<j>` under the dataset's
object. A channel the profile doesn't zarrify keeps its `data` element.

### 5.4 Equivalence

Two outputs are equivalent as [spec/zarrify.md §1.1](../zarrify.md#11-output-and-equivalence)
says.

## 6. Example

The public file `SS21347-20151117_32_D4-m-20x-brain-Split_GAL4-JRC2018_Unisex_20x_HR-aligned_stack.h5j`
of FlyLight's Split-GAL4 Omnibus Broad release (7,666,989 bytes) has two channels of 174 pictures
of 1216 × 568 coded samples of 12 bits, padded by 6 on the right and 2 at the bottom. Its entries,
in part:

```
zarr.json                     the root: H5jRoot, channels Channel_0 and Channel_1
Channel_0/zarr.json           ome: axes z, y, x in micrometer, scale [1.0, 0.5189161, 0.5189161];
                              omero: "signal", window 0 to 4095
Channel_0/0/zarr.json         shape [174, 566, 1210], uint16, chunks [24, 568, 1216], refkv.stream hevc y
Channel_0/0/c/0/0/0 … c/7/0/0 eight chunks of 24 pictures, the last of 6; chunk q decodes from picture 0
Channel_1/…                   "reference", the same shape
refkv_source/…                the mirror
```

## 7. What the layout does not express

This section is informative.

- **One array of channels.** OME-NGFF wants an image's channels on one axis of one array; a
  `refkv.stream` chunk decodes one stream into rows along its first axis, so each channel is an
  image of its own. The root lists them.
- **The other attributes' meanings.** `channel_spec`, `image_size`, `objective`, `alignment_space`
  and the rest are recorded, not interpreted.
- **The time stamps.** The pictures' presentation times order the slices and mean nothing more;
  they are in the mirror, and no array holds them.
- **Pictures of chroma.** A channel of 4:2:0 or 4:2:2 pictures, which no public file has, is not
  zarrified ([§8.2](#82-channels)).

# Part 2. The profile

## 8. H5J profile

The profile reads the file as the HDF5 profile does ([hdf5.md §8](hdf5.md#8-hdf5-profile)): every
check of that profile rejects an H5J file too. Then it reads each channel's MP4 file as the MP4
profile does ([mp4.md §8](mp4.md#8-mp4-profile)), each read moved by the dataset's address.

### 8.1 Choosing the profile

The profile has no signature. The HDF5 profile chooses it ([hdf5.md §8.1](hdf5.md#81-choosing-the-profile);
its registry entry's `chosen_by` is `hdf5`): an HDF5 file that is not an Imaris file, whose root
group has a link `Channels` (hard, or soft through hard links) to a group with at least one link,
every one of them named `Channel_<n>` (`n` one to nine decimal digits), is an H5J file. A
structure on that path that can't be read makes the file no H5J file. A file named as the H5J
profile's input ([spec/zarrify.md §1.7](../zarrify.md#17-named-inputs-and-layouts)) that is not an
H5J file is rejected: `not an H5J file: the root has no group Channels of links Channel_<n>`.

### 8.2 Channels

A channel is each object `/Channels/Channel_<n>`, in order of `n`. It is zarrified unless one of
these holds, checked in this order; the first that holds is its **reason** ([§5.1](#51-the-root)),
and the channel keeps its dataset's `data` element ([§5.3](#53-elements)):

- it is no dataset: `not a dataset of bytes`;
- its data type is not `uint8`, or it has other than one dimension;
- its storage is not contiguous (compact, chunked, external or not allocated);
- the MP4 profile rejects its bytes, read as a file: `its MP4 file: ` and the rejection;
- the MP4 file has other than one track;
- the MP4 profile doesn't zarrify the track ([mp4.md §8.2](mp4.md#82-tracks)): its reason;
- the track is not H.264, HEVC, AV1 or VP9 video;
- its pictures are not 4:0:0;
- `/Channels`'s `pad_right` or `pad_bottom` is not one integer of at least 0;
- `pad_right` is not less than `W`, or `pad_bottom` not less than `H`.

### 8.3 Checks

In this order, after the HDF5 profile's:

1. **Channels:** at most 1024: `budget: more than 1024 channels`.
2. **A channel:** the profile zarrifies at least one ([§8.2](#82-channels)). Without one, the input
   is rejected with each channel's reason, joined by `; `: `no channel can be zarrified:
   Channel_0: its pictures are 4:2:0, not 4:0:0 (monochrome)`.

### 8.4 Chunks

A channel's chunks are those of the MP4 profile's `y` array of its track
([mp4.md §4.1](mp4.md#41-video-tracks), [§8.3](mp4.md#83-chunks)): at most
`F = min(256, max(1, floor(2^25 / (W × H × B))))` pictures (`B` the bytes of a sample), cut at the
sync samples' presentation indexes, each chunk the `refkv.stream` chunk of its pictures and their
lead-in, its frames ranges of the H5J file. The public files have one or two sync samples, so a
chunk past the first decodes from picture 0.

### 8.5 Summary

This section is informative. Both implementations print a one-line JSON summary, with these
members:

| member | value |
|---|---|
| `channels` | for each channel zarrified, its `name`, `path`, `frames`, `chunkFrames` and `chunks` |
| `skipped` | for each other, its `name` and `reason` |
