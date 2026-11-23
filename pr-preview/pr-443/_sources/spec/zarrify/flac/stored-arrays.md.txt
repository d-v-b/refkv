# FLAC: stored arrays

What the FLAC parser describes as stored arrays ([stored-array.md](../../stored-array.md)), part of [the FLAC profile](../flac.md).

A FLAC file's samples are one stored array,
keyed by its STREAMINFO block, the value `metadata/0/streaminfo`, with no index. The parser's
description gives the key: its `elements` has that element's id as `streaminfo`
([flac.md §8.4](../flac.md#84-the-description)).

- **Declarations.** One: the STREAMINFO block, whose fields give the sample rate, the channels,
  the bit depth, the samples in all and the MD5 of the decoded samples.
- **`shape`** is the samples, then the channels: the frames' samples in all, which the parser
  checks are STREAMINFO's total where it records one, 0 meaning unknown
  ([flac.md §8.1](../flac.md#81-the-stream)), and STREAMINFO's channels. A sample's channels are together, last, as the codec makes them.
- **`item_bits`** is the bit depth rounded up to whole bytes: 8, 16, 24 or 32. These are the bytes
  STREAMINFO's MD5 is of (RFC 9639 §8.2): each sample little-endian, sign-extended.
- **The grid** is the frames: `Each` of the first frame's block size, then `Each` of the
  channels, under `short`. The profile accepts a stream of one block size, whose
  last frame alone may hold fewer samples. Each frame is a row, the element `frames/<j>`, whole.
- **The chain** is one, `flac`, its `setup` the STREAMINFO block. A frame decodes alone: FLAC has
  no state from frame to frame, so the codec is not a stream ([stored-array.md §3.1](../../stored-array.md#31-chains)).
- **The grid** is dense: FLAC has no fill value.
