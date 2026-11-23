Changed 2026-11-04: modes 3 and 101 are admitted ([§3](../../mrc.md#3-data-types)): mode 3 as a `struct` of two
`int16`, mode 101 as `uint8` through zarr-extensions' bit-packing `packbits`, its chunks cut where
values start on a byte ([§4.1](../../mrc.md#41-the-array)).
