Changed 2026-11-05: an unscaled dataset, which no dimension scale names, has the phony dimension
names h5netcdf gives it with `phony_dims="sort"`, a fixed-length string's trailing axis is
`string<n>` in place of `null`, and an attribute of one element is a scalar only on a dataset
named by its dimension scales ([§3](../../hdf5.md#3-datasets), [§6](../../hdf5.md#6-attributes)).
