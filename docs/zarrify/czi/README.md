# CZI

refkv turns a file into a Zarr hierarchy by writing a small archive of references
to the file's bytes, without copying them ([overview](../README.md)). This page is
about Zeiss CZI files; the convention and the profile are
[spec/zarrify/czi.md](../../../spec/zarrify/czi.md).

## Supported and not supported

### In the browser

The browser code ([js/](../../../js/README.md)) reads Zeiss CZI (file version 1):

- Every series (scene, block, ...) as an image of a bioformats2raw layout,
  its pyramid levels found from the subblocks; subblocks in no level kept as
  tile arrays under `tiles/`.
- Subblocks uncompressed, JPEG, JPEG XR or zstd (`imagecodecs_jpegxr`,
  `numcodecs.shuffle` and `zstd` codecs as needed), of every ZISRAW pixel
  type: gray, BGR and BGRA; integer, float and complex.
- An image across numbered files (`name.czi`, `name(1).czi` or `name (1).czi`, ...). Give it the first
  file: it is read with the others beside it when its entries name them, or when its metadata says it
  holds fewer planes or tiles than the image has. A later file given as the input is refused, with
  the name of the first. Each file is a source of the archive. A file that lacks planes with no later
  file beside it, such as an acquisition that stopped early, costs a probe or two and is read
  all the same.

Not supported, and refused with HTTP 422: LZW, lossless JPEG and camera raw
subblocks.

## Try it

[example.py](example.py) zarrifies a public file and plots it with matplotlib. Its
dependencies are in its header (PEP 723), so it runs on its own, with refkv from PyPI:

```bash
uv run docs/zarrify/czi/example.py
```
