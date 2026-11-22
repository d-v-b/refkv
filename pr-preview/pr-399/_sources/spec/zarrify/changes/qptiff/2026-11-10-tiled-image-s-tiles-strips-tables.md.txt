Changed 2026-11-10: an IFD is tiled when it has TileWidth and TileLength, and the tiles of a tiled
IFD with neither TileOffsets nor TileByteCounts are listed in its StripOffsets and StripByteCounts,
as in the TIFF convention, which [§4.2](../../qptiff.md#42-tiles-and-strips) cites; an IFD was tiled
when it had TileWidth and TileOffsets. The output of a file whose IFDs with TileWidth or TileLength
have TileWidth, TileLength, TileOffsets and TileByteCounts does not change.
