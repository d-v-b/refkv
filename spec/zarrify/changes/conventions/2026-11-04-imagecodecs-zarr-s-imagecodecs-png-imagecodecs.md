Changed 2026-11-04: imagecodecs-zarr's
`imagecodecs_png` and `imagecodecs_bmp` decode a chunk that is one PNG or BMP file, and its
`imagecodecs_lzw` decodes an LZW chunk that holds exactly the chunk's bytes, as CZI's LZW subblocks
do ([§3](../../../conventions.md#3-arrays)).
