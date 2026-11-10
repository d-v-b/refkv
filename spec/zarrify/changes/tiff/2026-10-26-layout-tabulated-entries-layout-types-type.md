Changed 2026-10-26, with no change to any output: the layout is tabulated, its entries in
[Layout](../../tiff.md#layout) and its types in type tables
([conventions §11.5](../../../conventions.md#115-layout-tables)); the QPTIFF convention shares
[`TiffRoot`](../../tiff.md#type-tiffroot) and [`TiffChunk`](../../tiff.md#type-tiffchunk); and a tile's or strip's recipe
frames it with the IFD's JPEG prefix whenever that prefix is not empty (the Adobe marker of 3
samples included, with or without JPEGTables) and the tile is more than 2 bytes, as refkv
already wrote ([§5.1](../../tiff.md#51-elements)).
