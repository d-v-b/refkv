Changed 2026-10-24: a video chunk holds whole groups of pictures, from a sync sample, in place
of `F` pictures from a multiple of `F`; a group longer than `F` pictures is split. Where the
chunks' lengths differ, the planes' chunk grid is the zarr-extensions `rectilinear` grid
([§4.1](../../mp4.md#41-video-tracks), [§8.3](../../mp4.md#83-chunks)).
