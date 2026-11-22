# LIF: stored arrays

What the LIF parser describes as stored arrays ([stored-array.md](../../stored-array.md)), part of [the LIF profile](../lif.md).

Every image of a LIF, LOF or LIFEXT file
([lif.md §2.3](../lif.md#23-images)) is a stored array, keyed by the element that
holds the XML that declares it, a kind of element that may declare several arrays: `header/xml`,
or, of a LOF, `description/xml`, with the image's index among that XML's images, in document
order. The facts give the key: each image's `declared_by` is that element's id and the index, a
pair. Below, a dimension's `N` and `s` are its `NumberOfElements` and `BytesInc`, `e` is the bytes
of a sample, and `o` the channels' `BytesInc` in ascending order
([lif.md §3.2](../lif.md#32-channels)): the sample of channel `c` at the index `i` of
each dimension lies `o_c + Σ i × s` bytes into the image's bytes, which start with the data of its
memory block.

- **A LAS X project** ([lif.md §8](../lif.md#8-las-x-documents)) is a store whose
  LOFs' IRs are grafted, each with its descriptions ([stored-array.md §6](../../stored-array.md#6-in-the-ir)): an image is its LOF's,
  keyed by `description/xml` under the LOF's object, and the store's parser renumbers the id in
  `declared_by`. Its bytes start at the `Offset` of the document's `Block`, counted in the block's
  data. An image the project holds in other files than one LOF is not read
  ([lif.md §8.4](../lif.md#84-the-images)), and has no description; the image of a
  LOF that a document that is no image names is read, and is described, though the profile
  presents none of it.
- **Declarations.** One: the key's element, the XML's whole text, in UTF-16, whose
  `DimensionDescription` and `ChannelDescription` elements say what the image's samples are
  (`DataType`, `Resolution`) and what its axes are (`DimID`); no range of the text is given. In
  a project it is the LOF's XML, which the parser reads the layout from, not the document that
  names the LOF.
- **`shape` and `axes`.** The axes are the image's dimensions, each one, a dimension of one
  element too, and, of an image of more than one channel, its channels, which take the position
  after the last dimension in the source's list. Storage order is read off the byte increments:
  the axes by their bytes a step (a dimension's `s`; the channels', the least difference of two
  of `o`), the largest first, and, of two of one step, the one the source lists later before the
  other (LAS X lists the fastest dimension first, and gives each the bytes of those before it). An
  axis of one element steps nowhere, and is placed by its `BytesInc` all the same. So X, Y and Z
  with three channels between Y and Z have `shape` `[N_Z, 3, N_Y, N_X]` and `axes`
  `[2, 3, 1, 0]`, and interleaved channels are the last axis. Along the channels' axis the
  elements are the channels in ascending order of `BytesInc`, which need not be the XML's.
  (liffile 2026.10.14 orders two dimensions of one `BytesInc` the other way; only dimensions of
  one element can be so in an image with a grid.)
- **`item_bits`** is `8 × e`: the channels' `Resolution`, rounded up to 8, 16, 32 or 64
  ([lif.md §3.1](../lif.md#31-data-types)). The parser refuses an image whose
  channels differ.
- **The grid.** From the fastest axis on, the span of the axes so far is the bytes from their
  first sample to the end of their last: `e` before any, and with each axis the place of its last
  element more. An axis of more than one element packs when it steps by exactly the span of the
  faster ones, the channels evenly so. The axes that pack, from the fastest up to the first that
  does not, are within a unit, `Each` of their length; every slower axis is `Each(1)`; the edge
  is `short`. So an image whose every axis packs is one unit, as LAS X writes most; one whose
  rows have bytes between them (stride-aligned RGB rows) has a unit a row; and one whose planes
  or channels lie apart has a unit for each. A unit is a row of the map: the `data` element of
  the image's memory block (`blocks/<i>/data`) with the `range` of the unit's bytes, or that
  element alone where the unit is all of it. Bytes of the block before, between or after the
  units are no part of the array. (liffile 2026.10.14 reads an image's samples end to end from
  the start of its block, whatever bytes the increments leave before or between them, but for
  stride-aligned rows of interleaved samples; and it reads a project's LOF from the start of its
  block whatever the `Offset`.)
- **The chain** is one, empty: the samples are stored as they are.
- **No grid** for an image whose axes do not nest: one with an axis of more than one element that
  steps by fewer bytes than the faster axes span. Its samples then share bytes, and are no array
  of distinct elements (a `BytesInc` of 0 repeats a plane, and a smaller one than the plane's
  bytes overlaps it), or lie in an order that no order of the axes gives (channels unevenly
  apart, with a dimension's steps between them). The profile presents such an image plane by
  plane all the same. And no grid for an image whose units' rows alone would pass the file's
  chunk map limit ([stored-array.md §6](../../stored-array.md#6-in-the-ir)), which are not built. Either image has its `shape`, `axes`
  and `item_bits`. What the parser refuses (bit-packed samples, channels of two types, channels
  neither planar nor interleaved, rows that overlap) has no IR.
- **The grid** is dense: LIF has no fill value.
