Changed 2026-11-04: an image across numbered files is read ([§7.9](../../czi.md#79-several-files)), where a
FilePart other than 0 was rejected; a file whose XML says it has more planes or tiles than its
directory holds is probed for its parts and read as a store, and so is the one file of the public
corpus it finds, `Image_1_2023_08_18__14_32_31_964.czi`, which is a set of four; the root has `parts`
in that case ([§5.1](../../czi.md#51-the-root)). Every other file's output is what it was.
