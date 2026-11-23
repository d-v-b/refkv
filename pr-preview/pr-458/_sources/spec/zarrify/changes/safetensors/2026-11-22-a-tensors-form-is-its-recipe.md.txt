Changed 2026-11-22: a tensor's data element's form is its recipe alone, `{"recipe": [["src", 0,
null]]}`, with no geometry (its Zarr data type, shape and bits) and no codec (its Zarr byte order)
([§5.3](../../safetensors.md#53-elements)): its header entry gives its type and shape, and the
projection decides its data type and codecs from them, as it did. The IR mirror of every file with
a tensor of data changes; no other output does.
