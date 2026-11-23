Changed 2026-11-22: the `voxels` element's form gives the header's `datatype` code in its geometry
in place of the Zarr data type ([§5.3](../../nifti.md#53-elements)), for a single file, a gzipped
file and a header-and-image pair alike: the projection decides the output's data type from the
code, as it did. The IR mirror of every file and pair changes; no other output does.
