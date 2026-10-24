"""The OME-Zarr profile (spec/virtualize/ome-zarr.md, §11
<https://github.com/d-v-b/refkv/blob/main/spec/virtualize/ome-zarr.md#11-ome-zarr-profile>): OME-Zarr
0.4 on Zarr v2 as OME-Zarr 0.5 on Zarr v3."""

from refkv_reference.ome_zarr.virtualize import declares_04, virtualize_ome_zarr

__all__ = ["declares_04", "virtualize_ome_zarr"]
