Changed 2026-11-04: an IFD image the layout cannot present (a macro image of another kind, or a
stack's focus map or mask) is left out and recorded in the root's `not_presented`, where it
rejected the slide ([§4.4](../../vsi.md#44-ifd-images-that-are-not-presented)); and a tile file's tiles may be
PNG (compression 8) or BMP (compression 9), each tile a chunk of the codec `imagecodecs_png` or
`imagecodecs_bmp`, where such a file was rejected ([§3](../../vsi.md#3-the-tile-files), [§4.1](../../vsi.md#41-the-pyramid)).
