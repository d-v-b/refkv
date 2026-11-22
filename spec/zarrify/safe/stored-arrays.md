# SAFE: stored arrays

What the SAFE parser describes as stored arrays ([stored-array.md](../../stored-array.md)), part of [the SAFE profile](../safe.md).

Each band file of a Sentinel-2 product, a JP2
file, holds one image, a stored array, in both forms of the product: in the directory form's
store IR, whose parser describes its objects' arrays itself ([stored-array.md §6](../../stored-array.md#6-in-the-ir)), and in the zip
form's file IR. It is keyed by the band file's `codestream` struct (`boxes/<i>/codestream`, under
the band file's object or under its zip entry's `data`), with no index. The description gives the key ([safe.md §8.11](../safe.md#811-the-description)):
each band's `element` is the id of that struct. The profile presents every band file. The
product's other images (the quality masks, the preview image, the cloud and snow probabilities,
a mirror's quicklook) are in objects the parser does not read
([safe.md §2.1](../safe.md#21-objects)), and have no description.

- **Declarations.** The `siz` value, the codestream's SIZ marker segment, whose component triples
  give a sample's precision and sign; then the `contents` of each `jp2h` box before the
  codestream, the JP2 header, which holds the color specification. The parser parses the first,
  and holds the second as bytes.
- **`shape`** is `Ysiz`, then `Xsiz`, then, for an image of three components, 3: a pixel's
  components are together, last. `axes` is `[1, 0]`, or `[1, 0, 2]`: SIZ gives `Xsiz` before
  `Ysiz`, and `Csiz` is a count, after the extents it lists.
- **`item_bits`** is the size of the samples the codec makes: 8 for a precision of at most 8 bits,
  else 16. The precision itself (15 bits of a reflectance band) is SIZ's.
- **The grid** is the codestream's tiles: `Each` of `YTsiz` and of `XTsiz`, each at most its
  axis's length, then `Each(3)` for three components, under `short`: a tile at the image's right
  or bottom edge is cut to the image. (A tile size above the image's is SIZ's alone, where the
  profile reads its chunk shape.) A tile is a unit, and not the image one unit in a `jpeg2k`
  chain, because the parser locates every tile: it walks the codestream's tile-parts by their SOT
  markers, and accepts only a codestream of one tile-part a tile, in raster order
  ([safe.md §8.3](../safe.md#83-band-files)). Each tile is a row, the element
  `tiles/<k>`, whole: its tile-part, from its SOT marker through its packets. Every tile has a
  row. (GDAL reads a band file in blocks of its own choosing, the codestream's tiles or the
  whole image: the grid is SIZ's.)
- **The chain** is one, `jpeg2k_tile`, its `header` the range of the `codestream` struct from its
  start to the first SOT: the main header. A chain's header is its own file's, so each band file
  has a chain of its own.
- **No codestream is made.** A tile's bytes are no codestream without the main header, and the
  description holds none the source does not: the SIZ of its own that the profile gives each
  chunk, and the empty tiles that fill a chunk at an edge
  ([safe.md §8.6](../safe.md#86-chunks)), are the profile's.
- **No grid** for no band file: the parser rejects a product with a band file it does not walk
  to its end (a TLM, PLM or PPM marker, several tile-parts for a tile, an image or tile origin
  other than 0, components that are signed, subsampled or of unlike precisions), and builds no
  IR of it.
- **The grid** is dense: JPEG 2000 has no fill value.
