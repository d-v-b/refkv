# CZI

refkv turns a file into a Zarr hierarchy by writing a small archive of references
to the file's bytes, without copying them ([overview](../README.md)). This page is
about Zeiss CZI files; the convention and the profile are
[spec/zarrify/czi.md](../../../spec/zarrify/czi.md).

## Supported and not supported

### In the browser

The browser code ([js/](../../../js/README.md)) reads Zeiss CZI (file version 1, one file):

- Every series (scene, block, ...) as an image of a bioformats2raw layout,
  its pyramid levels found from the subblocks; subblocks in no level kept as
  tile arrays under `tiles/`.
- Subblocks uncompressed, JPEG, JPEG XR, LZW, lossless JPEG or zstd, as the
  table below says, of every ZISRAW pixel type the table lists: gray, BGR and
  BGRA; integer, float and complex.

Not supported, and refused with HTTP 422: camera and system raw subblocks,
lossless JPEG with a color pixel type, and CZI files split over several files.
A subblock whose stream does not fit its pixel type, as 8-bit JPEG under Gray16,
is kept as bytes and has no array.

| Compression | pixel types | codec | zarr-python | Neuroglancer fork |
|---|---|---|---|---|
| uncompressed, zstd | all | `bytes`, `zstd`, `numcodecs.shuffle` | yes | yes |
| JPEG | Gray8, Bgr24 | `imagecodecs_jpeg` | yes | yes |
| JPEG, 12-bit | Gray16 | `imagecodecs_jpeg` | yes | no: its decoder is 8-bit, so it refuses the array |
| JPEG XR | Gray8, Gray16, Gray32Float, Bgr24, Bgr48, Bgr96Float, Bgra32 | `imagecodecs_jpegxr` | yes | yes |
| LZW | all | `bytes`, `imagecodecs_lzw` | yes | yes |
| lossless JPEG, under its own value or JPEG's | Gray8, Gray16 | `imagecodecs_ljpeg` | yes | yes |

zarr-python needs `refkv[images]` for the `imagecodecs_` codecs. The fork's column
is its branch `vzip`, read from its source; these subblocks have not been shown
in it. Its data types limit it too, whatever the codec.

## Try it

[example.py](example.py) zarrifies a public file and plots it with matplotlib. Its
dependencies are in its header (PEP 723), so it runs on its own, with refkv from PyPI:

```bash
uv run docs/zarrify/czi/example.py
```
