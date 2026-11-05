Changed 2026-10-25: an edge chunk's zero padding is a range of the output's one zero data
source, not a literal range of each reference, so that the archive holds the zeros once
([§9.4](../../fits.md#94-chunks)); no chunk's values change, nor which chunks are copied.
