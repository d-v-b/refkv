Changed 2026-10-30: a tile file's tiles may be lossless JPEG (compression 5), each tile a chunk
of the codec `imagecodecs_ljpeg`, where such a file was rejected; and an ETS header of version
0x30003, whose `dimensions` is 0 and which lists no sizes, is read, every coordinate but the
column, the row and the level of size 1 ([§3](../../vsi.md#3-the-tile-files), [§4.1](../../vsi.md#41-the-pyramid)); a stack's second tile
file name, `frame_t_0.ets`, is probed only when its first, `frame_t.ets`, is absent, in a
second batch ([§9.2](../../vsi.md#92-the-tile-files)).
