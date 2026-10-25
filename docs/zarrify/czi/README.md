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
- Subblocks uncompressed, JPEG, JPEG XR or zstd (`imagecodecs_jpegxr`,
  `numcodecs.shuffle` and `zstd` codecs as needed), of every ZISRAW pixel
  type: gray, BGR and BGRA; integer, float and complex.

Not supported, and refused with HTTP 422: LZW, lossless JPEG and camera raw
subblocks, and CZI files split over several files.

## Try it

[example.py](example.py) zarrifies a public file and plots it with matplotlib. Its
dependencies are in its header (PEP 723), so it runs on its own (uv builds refkv from GitHub,
which needs a Rust toolchain, until a release on PyPI has `refkv.zarrify`):

```bash
uv run docs/zarrify/czi/example.py
```
