Changed 2026-11-10: an IFD is tiled when it has TileWidth and TileLength, and the tiles of a tiled
IFD with neither TileOffsets nor TileByteCounts are listed in its StripOffsets and StripByteCounts
([§2](../../tiff.md#2-the-source)), as libtiff and tifffile read such an image and as its
stored-array description takes it ([stored-array §7](../../../stored-array.md#7-the-formats)); an
IFD was tiled when it had TileWidth and TileOffsets. A tiled image whose tiles are listed under the
strips' tags has the layout it would have with them under the tiles' tags, where it was read as an
image in strips, which rejected it when its tables did not have a strip image's count of values;
in the source metadata the members of its `…/strips` have a tile's shape
([§5.1](../../tiff.md#51-elements)). An IFD with TileWidth and TileOffsets and no TileLength is in
strips, where it was rejected for its missing TileLength. The output of a file whose IFDs with
TileWidth or TileLength have TileWidth, TileLength, TileOffsets and TileByteCounts does not change.
