Changed 2026-11-10: an ImageJ hyperstack, a file without OME-XML whose IFD 0's ImageDescription
starts with `ImageJ=` and gives `images` above 1, has the planes its description gives, over `t`,
`c` and `z`: one main-chain IFD a plane, or, in one IFD, IFD 0's strips repeated after it, as
ImageJ writes a stack of more than 4 GB ([§4.1](../../tiff.md#41-planes)); the planes are named
`imagej/planes/<p>`, and a stack in one IFD has elements for the strips of its later planes
([§5.1](../../tiff.md#51-elements), [§7.3](../../tiff.md#73-chunks)). Such a file was rejected as a
stack of pages, or read as its first plane alone.
