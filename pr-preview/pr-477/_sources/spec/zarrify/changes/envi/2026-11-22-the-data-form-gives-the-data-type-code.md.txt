Changed 2026-11-22: the `data` element's form gives the header's `data type` code in its geometry
in place of the Zarr data type ([§5.3](../../envi.md#53-elements)): the projection decides the
output's data type from the code, as it did. The IR mirror of every file changes; no other output
does.
