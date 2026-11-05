Changed 2026-10-27: images in strips and LZW (Compression 5) have a layout, which they had in
the QPTIFF convention only: a level in strips has chunks of its rows per strip, a short last
strip padded to them when uncompressed (with a range of the output's zero data source,
[§7.3](../../tiff.md#73-chunks)) or LZW ([§4.5](../../tiff.md#45-strips)), and LZW is `refkv.tiff_lzw`
([§4.3](../../tiff.md#43-data-types-and-codecs)); the table of [§2](../../tiff.md#2-the-source) holds the strip tags,
which the profile now checks in every IFD it reads ([§7.1](../../tiff.md#71-reading)); no output of a file
accepted before changed.
