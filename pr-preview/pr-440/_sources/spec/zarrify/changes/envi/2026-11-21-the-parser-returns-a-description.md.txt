Changed 2026-11-21, with no change to any output of this convention: the profile's parser returns
a description of the raster, the header's fields as written and as the profile reads them, the
data file's key and size and the data's range in it, in place of facts that held the output's Zarr
data type, byte order and source metadata; the projection makes the data type, the transpose order,
the codecs, the chunk cut, the axis names, the fill value and the root's source metadata from the
description ([§8.7](../../envi.md#87-the-description)).
