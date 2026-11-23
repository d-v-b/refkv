Changed 2026-11-22: an image's `data` element's form gives its BITPIX and shape in its geometry in
place of the Zarr data type ([§6.1](../../fits.md#61-elements)): the projection decides the
output's data type from BITPIX, as it did. The IR mirror of every file with an image stored without
compression changes (a tile-compressed image has no `data` element); no other output does.
