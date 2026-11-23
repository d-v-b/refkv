# MP4: stored arrays

What the MP4 parser describes as stored arrays ([stored-array.md](../../stored-array.md)), part of [the MP4 profile](../mp4.md).

Every track whose handler is `vide` or `soun` has
descriptions, whether or not the profile zarrifies it, keyed by its `trak` box, a kind of element
that may declare several arrays: each of a track's has an index. The parser's description gives the
key: each such track's `elements` has the id of its `trak` box as `trak`
([mp4.md §8.5](../mp4.md#85-the-description)). A track's samples are its tables', then its
track fragments', in decode order ([mp4.md §3](../mp4.md#3-tracks-and-samples)), each
the element `samples/<T>/<j>`.

- **Declarations.** The `fields` value of the track's first sample entry (its `data` value, of a
  sample entry the convention does not parse), then the `data` value of each of its child boxes,
  in order: a video codec's configuration is among them. A track with no sample entry has none.
- **A video track of H.264, HEVC, AV1 or VP9** (the sample entries `avc1`; `hvc1` and `hev1`;
  `av01`; `vp09`), of one sample description, whose configuration box (`avcC`, `hvcC`, `av1C`,
  `vpcC`) the parser reads as pictures of three planes of one bit depth, from 8 to 16 (4:2:0,
  4:2:2 or 4:4:4), or of a luma plane alone of such a depth (4:0:0), has three arrays, the planes
  of its pictures: index 0 the luma plane, 1 and 2 the chroma planes; of 4:0:0, the luma plane's
  alone.
  - `shape` is the samples, then the plane's height and width: the sample entry's `height` and
    `width` for the luma plane, and for a chroma plane those halved, rounded up, as the format
    subsamples (both for 4:2:0, the width for 4:2:2, neither for 4:4:4). The first axis is the
    samples in decode order, not the pictures in presentation order: when a sample is presented
    is the profile's, from the track's tables. A stream whose pictures are of another size than
    the sample entry's decodes to the stream's.
  - `item_bits` is 8 for a bit depth of 8, else 16.
  - The grid is a sample a unit: `Each(1)`, then `Each` of the plane's height and of its width,
    under `short`.
  - The chain is the codec (`h264`, `hevc`, `av1` or `vp9`): its `setup` the configuration box's
    `data` value, its `plane` the array's index, and `sync` a unit parameter, 1 for a sync sample
    ([mp4.md §3](../mp4.md#3-tracks-and-samples)) and 0 for any other.
  - Each sample is a row of each of the track's arrays: its element.
- **A Motion JPEG track** (`jpeg`, `mjpa`, `dmb1`, or `mp4v` of object type indication 0x6C), of
  one sample description, whose first sample's frame header the parser read, of the sample entry's
  width and height, 8 bits and 1 or 3 components, has one array, index 0: `shape` the samples,
  the height, the width and, for 3 components, the components; `item_bits` 8; a sample a unit, in
  decode order, in a `jpeg` chain with no `tables`.
- **Any other track of one sample description** has one array, index 0, of its samples as the
  sample entry encodes them, by the general fallback ([stored-array.md §1](../../stored-array.md#1-stored-arrays)): an audio track,
  whose decoded samples are not described yet; a video track of another codec, of a sample entry
  of width or height 0, or whose configuration the parser does not read as above (pictures of
  chroma planes of another bit depth than luma's); and a Motion JPEG track of another frame (a
  picture's fields, 12 bits, streams with no markers). `shape` is the track's sample elements,
  one axis; there is no `item_bits`, since the IR does not say what a sample decodes to; the grid
  is `Each(1)` under `short`; and the chain is `source`, with `format` `mp4` and `id` the sample
  entry's type, its four characters as a string. Each sample element is a row, in decode order:
  a sample, or, of a PCM track of one sample size, a chunk's samples together
  ([mp4.md §5.3](../mp4.md#53-elements)).
- **No grid**, and neither `shape` nor `item_bits`, in one description of index 0, for a track
  whose samples are not one array: a track with no samples, with more than one sample
  description, or with its sample sizes in an `stz2` box; and a PCM track with a sample that its
  tables size as no whole number of frames, which readers do not agree on (ffmpeg reads its
  frames by the sample entry's size).
- **The grid** is dense: MP4 has no fill value.
