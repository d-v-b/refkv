Changed 2026-11-04: an array has `dimension_names` from its `DIMENSION_LIST`, or from itself as a
dimension scale, a netCDF-4 dimension without a variable is not presented
([§3](../../hdf5.md#3-datasets)), and such a dataset's attribute of one element is a scalar and any dataset's
`_FillValue` in the form xarray reads ([§6](../../hdf5.md#6-attributes)).
