Changed 2026-10-23: each `omero` channel's window is sampled from the coarsest level's data, as
in the TIFF convention, when refkv decodes its compression (none, LZW, Deflate) and the image is
not RGB, so that a `float32` channel has a window too, and an integer channel without a sampled
window has the type's range ([§4.3](../../qptiff.md#43-arrays)); the profile reads the sampled chunks
([§7](../../qptiff.md#7-qptiff-profile)).
