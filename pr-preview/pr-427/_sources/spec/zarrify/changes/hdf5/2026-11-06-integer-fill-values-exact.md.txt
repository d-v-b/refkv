Changed 2026-11-06: an integer dataset's fill value past 2^53 in magnitude is written as a JSON
integer, exact, as Zarr v3 writes one, where it was the string of its digits, as an attribute's
([§3](../../hdf5.md#3-datasets)). The output of a dataset whose fill value is such an integer
changes; no other output does.
