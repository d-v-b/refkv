Changed 2026-11-06: a producer reads every IFD's tile and strip tables whatever their size, and
lists the tiles or strips of every IFD with both tables, as [§5.1](../../tiff.md#51-elements)
says ([§5.3](../../tiff.md#53-values)); it left the tables of more than 64 KiB of an IFD the
image does not use unread, and listed none of that IFD's tiles. The source metadata of a file
with such an IFD (an OME-TIFF's other series, a large page beside the image) changes; its image
does not.
