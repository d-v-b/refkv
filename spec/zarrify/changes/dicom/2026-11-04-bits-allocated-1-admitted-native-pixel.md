Changed 2026-11-04: Bits Allocated 1 is admitted for native pixel data
([§3](../../dicom.md#3-the-image)), its array `bool` through zarr-extensions' bit-packing `packbits`
([§4](../../dicom.md#4-output)), each frame a chunk where every frame starts on a byte, else one chunk
([§7.5](../../dicom.md#75-frames)).
