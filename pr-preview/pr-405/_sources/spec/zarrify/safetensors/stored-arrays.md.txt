# safetensors: stored arrays

What the safetensors parser describes as stored arrays ([stored-array.md](../../stored-array.md)), part of [the safetensors profile](../safetensors.md).

Every tensor of a header
that passes the profile's checks ([safetensors.md §8.2](../safetensors.md#82-checks))
is a stored array, whether or not the profile presents it, keyed by the file's `header` element, a
kind of element that may declare several arrays: a tensor's index is its position among the
header's tensors, in the order the header writes them (`__metadata__` is none of them). The
description gives the key: `elements.header`
([safetensors.md §8.5](../safetensors.md#85-the-description)), and a tensor's index is its position
in the description's `tensors`.

- **The store form** ([safetensors.md §9](../safetensors.md#9-the-store-form))
  describes each shard's tensors itself, not by graft. Every shard whose header passed the checks
  is described in place, whether or not the profile presents it: it has a file's elements, its
  entry of the store's description holds its header's description with them, and its tensors are
  keyed by its own `header`. A shard whose header fails a check has no descriptions.
- **Declarations.** One: the range of the header that holds the entry's `dtype` value, a JSON
  string as the header writes it, with its quotes and any escapes. The format fixes the elements' byte order
  (little-endian) and their order (the last index varying fastest), which no place declares.
- **`shape`** is the entry's `shape`, empty for a scalar. `axes` is absent: a tensor is stored in
  the order of its `shape`.
- **`item_bits`** is the bits of one element of the `dtype`
  ([safetensors.md §3](../safetensors.md#3-data-types)), from 4 to 64; a complex
  number is one element. There is no `pad_from`: a tensor's elements pack unbroken into whole
  bytes.
- **The grid.** A tensor is one unit: each axis is `Each` of its length, or an empty `List` for
  an axis of length 0, under `short`. Its row is its data's element, `tensors/<i>`. A tensor with an axis of length 0
  holds no bytes, and has no unit and no row.
- **The chain** is empty for elements of whole bytes. Elements of 4 or 6 bits (`F4`, `F6_E2M3`
  and `F6_E3M2`) are in `lsb_first`, its `bits` their size: the buffer packs them from each
  byte's least significant bit
  ([safetensors.md §3.2](../safetensors.md#32-packed-data-types)). The library
  reads a tensor's bytes and none of its elements, so this order rests on that section: for `F4`
  it is the order of PyTorch's `float4_e2m1fn_x2`, which the library writes, and for the 6-bit
  types, of which no writer was found, the same order by analogy.
- **The grid** is dense: safetensors has no fill value.
- **No grid** for no tensor of a checked header, but at the descriptions' limit's fallback
  ([stored-array.md §6](../../stored-array.md#6-in-the-ir)): the checks leave no tensor whose storage the IR does not model, and the
  chunk map's limit has room for the row of any tensor that holds a byte. A header reaches the
  descriptions' limit only where its tensors' shapes have more than 2^23 axes in all, each a
  description's entry and two bytes of the header: tensors then fall back, or the file keeps no
  descriptions.
