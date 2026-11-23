Changed 2026-11-22: a frame's or fragment's form gives its codec as the file's Transfer Syntax UID,
`{"transfer_syntax": T}`, in place of the Zarr codec refkv picks for it (`{"name": "bytes",
"endian": E}`, `{"name": "packbits"}`, `{"name": "jpeg2k"}` and so on)
([§5.4](../../dicom.md#54-elements)): the projection decides the codecs from the transfer syntax, as
it did. The IR mirror of every file with pixel data changes; no other output does.
