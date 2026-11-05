Changed 2026-10-26, with no change to any output: the layout is tabulated, its entries in
[Layout](../../czi.md#layout) and its types in type tables
([conventions §11.5](../../../conventions.md#115-layout-tables)), and the source's structures, the
values read from the metadata XML, the codecs and the cases of a coded size are tables; the
prose also states what refkv already wrote and it left unsaid: when the largest divisor band of
a large uncompressed tile holds less than 2^20 bytes, its bands are short bands of the most rows
within 2^24 bytes, and the level's or tile array's grid is rectilinear
([§4.3](../../czi.md#43-image-arrays), [§4.4](../../czi.md#44-tiles)); and it drops the per-element arrays under
`refkv_source` that the IR mirror replaced in
[revision 21](../../../history/virtualize/REVISIONS.md#revision-21-the-ir-mirror-is-the-source-metadata-of-tiff-nd2-and-czi)
([§7.5](../../czi.md#75-chunk-references)), and names the subblock segment `ZISRAWSUBBLOCK`
([§5.3](../../czi.md#53-elements)).
