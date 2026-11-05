Changed 2026-10-27: an image of an integer type with Predictor 2 (horizontal differencing) and
LZW, Deflate or zstd has a layout, its codecs `imagecodecs_delta` before `bytes`
([§4.3](../../tiff.md#43-data-types-and-codecs)), and its `omero` windows sampled through the predictor
([§4.4](../../tiff.md#44-arrays)); any other predictor, and Predictor 2 with a `float` type, with no
compression or with JPEG, is still rejected; no output of a file accepted before changed.
