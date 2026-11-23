Changed 2026-11-04: an image of a `float` type with Predictor 3, the floating-point predictor, and
LZW, Deflate, zstd or LZMA has a layout, `imagecodecs_floatpred` in place of `bytes`
([§4.3](../../tiff.md#43-data-types-and-codecs)), and its `omero` windows sampled through the predictor
([§4.4](../../tiff.md#44-arrays)); Predictor 3 with an integer type, with no compression or with JPEG is rejected;
no output of a file accepted before changed.
