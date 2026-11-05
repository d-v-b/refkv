Changed 2026-10-25: an edge chunk's zero padding is a range of the output's one zero data
source, not a literal range of each reference, so that the archive holds the zeros once
([§7.2](../../nifti.md#72-chunks)); no chunk's values change, nor which chunks are copied.
