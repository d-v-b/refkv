Changed 2026-11-04: subblocks in LZW have a layout, their codecs `bytes` and `imagecodecs_lzw`, and
so do Gray8 and Gray16 subblocks in lossless JPEG, `imagecodecs_ljpeg`, whether under Compression 3
or JpgFile, and Gray16 subblocks of 12-bit JPEG, `imagecodecs_jpeg`
([§3.1](../../czi.md#31-pixel-types-and-codecs)). Any other stream under those Compressions has no coded size,
so a Gray16 subblock of 8-bit JPEG, which rejected the input, is unplaced ([§3.2](../../czi.md#32-conforming-and-unplaced-subblocks)). Lossless JPEG with Bgr24 or Bgr48, JPEG with
Bgr48 and raw data are still rejected. No output of a file accepted before changed.
