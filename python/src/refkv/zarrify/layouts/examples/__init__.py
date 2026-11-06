"""Example layout plugins that ship with refkv (spec/layout.md
<https://github.com/d-v-b/refkv/blob/main/spec/layout.md>), one module each, registered under the
names `refkv.example.<name>` (`python -m refkv.zarrify --layout refkv.example.bare in.tif
out.vzip`). Each module imports only the standard library and refkv's public API, so that a
user can copy one into a package of their own, rename it, and change it. They are examples, not
refkv's layouts: refkv's conventions do not specify their output.

- `bare`: refkv's image layout with none of refkv's declarations and no mirror.
- `ome_zarr_06`: an OME-Zarr 0.6 draft image of a TIFF (NDPI, QPTIFF, IMS) or of HDF5 datasets.
- `tiff_pages`: every main-chain IFD of a TIFF, with its SubIFDs as its pyramid, from the IR.
- `hdf5_rechunk`: contiguous HDF5 datasets re-tiled into chunks of a shape the options give.
- `xarray_v2`: an HDF5 or NetCDF-4 file as Zarr v2 with consolidated metadata, for xarray.
"""
