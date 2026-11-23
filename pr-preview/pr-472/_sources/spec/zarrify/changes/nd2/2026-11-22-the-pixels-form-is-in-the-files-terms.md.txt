Changed 2026-11-22: a frame's `pixels` form is in the file's terms
([§5.3](../../nd2.md#53-elements)): its geometry gives its bits per component in memory
(`uiBpcInMemory`) in place of the Zarr data type, and a compressed frame's codec is
`{"compression": "zlib"}` in place of the Zarr codec chain `["zlib", "bytes"]`; an uncompressed
frame's form has no codec. The projection decides the data type and codecs, as it did. The IR
mirror of every file with a placed frame changes; no other output does.
