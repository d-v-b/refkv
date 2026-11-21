# NDPI: stored arrays

What the NDPI parser describes as stored arrays ([stored-array.md](../../stored-array.md)), part of [the NDPI profile](../ndpi.md).

An NDPI is read by the TIFF parser in NDPI's
layout, and every IFD that declares a size is described as a TIFF's, keyed likewise, the macro
image and the slide map too, with the differences below. The facts give the keys: each level's
`declared_by` lists, in the order of its `planes`, the id of each plane's IFD.

- **A JPEG strip that McuStarts cuts.** An image of Compression 7 in one strip (interleaved
  samples of 8 bits, with neither JPEGTables nor a tile size), with McuStarts (65426), whose
  strip's header the parser read ([zarrify/ndpi.md
  §7](../ndpi.md#7-ndpi-profile) reads those of the IFDs of positive Magnification), is cut
  into the strip's restart intervals, as tifffile reads it. The grid is `Each` of the MCU's
  height, `Each` of an interval's width (its MCUs times the MCU's width) and `Each` of the
  samples, under `full`. Each interval is a row: the strip's element, with the `range` from the
  interval's McuStarts value (with McuStartsHighBytes) to 2 bytes before the next one's, or
  before the strip's end, which leaves out the restart marker or EOI after it. The chain is
  `jpeg_restart_interval`, its `header` the range of the strip's element before the first
  interval.
- **A JPEG strip read whole.** The image is one strip in a `jpeg` chain, as a TIFF's, without
  McuStarts, and also where the parser did not read the strip's header (an IFD of no positive
  Magnification), where the header is not one the profile reads, where its frame's components
  are not the image's samples, where McuStarts and McuStartsHighBytes do not fit one another,
  and where McuStarts are not the offsets of the intervals the image's size needs: as many,
  increasing, within the strip, each interval of at least one byte. A strip's frame may give
  another size than the image's fields ([ndpi.md §4](../ndpi.md#4-the-image)): the
  description gives the fields', and the stream decodes to the frame's.
- **JPEG XR** (Compression 22610). The fields do not say what the streams hold: Hamamatsu
  writes 8-bit YCbCr of 3 samples whatever they are ([zarrify/ndpi.md
  §3](../ndpi.md#3-levels)). Where the parser read the header of the image's first stream
  (its first tile that holds bytes, or its strip), its pixel format is one the profile reads and
  its size is the unit's, the image's samples are the stream's: `shape` has the pixel format's
  samples last, or none for one sample; `item_bits` is the pixel format's, 8 or 16; and the
  chain is `jpegxr`. The declarations are the fields' all the same. Any other JPEG XR image
  (an IFD of no positive Magnification, whose stream the parser does not read) is as its fields
  say, in a `source` chain.
- **`source`** has `format` `ndpi` in an NDPI.
