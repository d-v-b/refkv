Changed 2026-11-08: an integer or bitfield attribute past 2^53 − 1 in magnitude is a JSON integer,
exact, where it was the string of its digits ([§6](../../hdf5.md#6-attributes)), and so is an
enumeration member's value in a described datatype ([§7.2](../../hdf5.md#72-datasets)); a netCDF
`_FillValue` of one integer is xarray's integer, exactly, whatever its magnitude, where it was
only up to 2^53 − 1 ([§6](../../hdf5.md#6-attributes)); and a fill value's
integer is exact whatever its place in the data type: an enumeration's, and a compound's integer or
enumeration member's, were the strings of their digits past 2^53 ([§3](../../hdf5.md#3-datasets)).
The output of a file with such an attribute or fill value changes. The mirror of nearly every file
changes too (of every fixture): its view shows undefined addresses (2^64 − 1: the superblock's
free-space, driver and extension addresses, a symbol-table node's siblings, a heap's huge index)
as JSON integers
([conventions §8.7](../../../conventions.md#87-the-view)).
