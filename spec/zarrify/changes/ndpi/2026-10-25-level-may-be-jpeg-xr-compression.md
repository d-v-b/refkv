Changed 2026-10-25: a level may be JPEG XR (Compression 22610), tiled or in one strip, each tile
or strip a chunk, its data type and samples from the JPEG XR stream header and not the tags
([§3](../../ndpi.md#3-levels), [§4](../../ndpi.md#4-the-image)); the tile tables' high bytes (65324, 65325) extend them
([§7](../../ndpi.md#7-ndpi-profile)); the root has the source metadata of [§5](../../ndpi.md#5-source-metadata), the
fluorescence filter, wavelength and exposure time, and a single channel's `omero` label is its
filter's name (d-v-b/refkv#123).
