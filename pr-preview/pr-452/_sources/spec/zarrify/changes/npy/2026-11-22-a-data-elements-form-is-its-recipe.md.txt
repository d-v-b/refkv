Changed 2026-11-22: an array's `data` element's form is its recipe alone, `{"recipe": [["src", 0,
null]]}`, with no geometry (its Zarr data type, shape and order) and no codec (its Zarr byte order)
([§5.3](../../npy.md#53-elements)): the header gives its type, order and shape, and the projection
decides its data type and codecs from them, as it did. The IR mirror of every file and archive
whose data is presented changes; no other output does.
