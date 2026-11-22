# CZI: stored arrays

What the CZI parser describes as stored arrays ([stored-array.md](../../stored-array.md)), part of [the CZI profile](../czi.md).

Every subblock the parser reads is a stored
array, whether or not the profile places it: the `ZISRAWSUBBLOCK` segment an entry of the subblock
directory names, or, in a part with no directory, one that the part's walk finds
([czi.md §7.9](../czi.md#79-several-files)). A subblock is the one structure of a CZI
file that declares an array: a rectangle of pixels of its own stored size, pixel type and
compression. The file declares no array above it. A scene, a mosaic and a pyramid level are
regions of one pixel frame, in which each subblock's entry gives its place (the Start and Size of
its dimension entries), and there subblocks overlap, leave gaps, and differ in scale and in
encoding: the source cuts nothing into a grid of them. Assembling subblocks into images and levels
is the profile's ([czi.md §3](../czi.md#3-placement)), which reads each subblock's
place from its declarations. An array is keyed by its segment's struct (`subblocks/<i>`), with no
index; a segment that several entries name is one array, keyed by the struct of the first of them,
of which the others are aliases ([czi.md §5.3](../czi.md#53-elements)). The facts
give the keys: `declared_by` lists, for each entry in order, the id of its subblock's struct, null
for an entry of a subblock in another file, which that file's entries list again; and each
level's and each tile array's `declared_by` lists the ids of its subblocks' structs in the order
of its `cells` or its `members`. Each file of an image across files describes its subblocks in its
own IR, and the CZI store, which grafts them, renumbers the ids its facts give
([stored-array.md §2.1](../../stored-array.md#21-keys)).

- **Declarations.** The subblock's own copy of its entry: `subblocks/<i>/entry`, its 32 bytes,
  which hold the PixelType and the Compression, then `subblocks/<i>/dimensions`, its dimension
  entries, which hold the stored size and the subblock's place among the others, where the copy
  has any. The directory's entry of the subblock, where the file's directory has one
  (`directory/entries/<j>` and `directory/dimensions/<j>`, `j` a position of the key in
  `declared_by`), holds the same bytes wherever the array has a shape, but for the FilePart of an
  entry of or in a part.
- **`shape`** is the stored height (the StoredSize of the `Y` dimension entry), then the stored
  width (that of `X`), then the samples of a pixel type of several: 3 for Bgr24, Bgr48, Bgr96Float
  and Bgr192ComplexFloat, and 4 for Bgra32. Such a pixel type has the axis in every encoding,
  whether the encoding stores a pixel's samples together or a codec makes them. `axes` is absent:
  an entry names its dimensions by letter, and lists them in no order. The entry's other
  dimensions (`C`, `Z`, `T`, `S`, `M` and the rest) are no axes of the array: they say where the
  subblock lies.
- **`item_bits`** is the bits of one sample of the PixelType: 8, 16, 32 or 64.
- **The grid** is one unit, the whole subblock: each axis `Each` of its length, under `short`. Its
  row is the subblock's data, the `data` element: of uncompressed data its first bytes, as many as
  the array's (bytes after them are no part of the unit), and of any other all of it. The data of
  a Zstd1 subblock is two elements, its header and its frame, so its row is `ranges` of the file,
  all the data's bytes; so is the row of a subblock whose `data` element the finished IR made an
  alias, its bytes lying in an earlier element's, and a declaration that became one is the
  range of the file likewise.
- **The chain** is one, by the Compression: empty for 0; `tiff_lzw` for 2; `jpegxr` for 4; `zstd`
  for 5; `czi_zstd1` for 6; and `jpeg` for 1 (JpgFile) and 3 (lossless JPEG), whose streams'
  precisions, which the grid's conditions bound, are at most the samples' bits.
- **A grid only where the data is what the entry says**, as far as the parser reads without
  decoding it ([czi.md §7.4](../czi.md#74-codec-headers)): uncompressed data of at
  least the array's bytes; a Zstandard frame (after a Zstd1 header of either form) that names no
  dictionary and declares, where it declares one, a content size of the array's bytes, with hi-lo
  packing only on samples of 16 bits; an LZW stream that starts with the Clear code; a JPEG XR
  file whose container gives a pixel format the PixelType admits; and a JPEG stream whose frame
  header has the pixel type's samples as its components and a precision that its samples hold:
  for a lossless stream (SOF3), 2 to 8 with samples of 8 bits and 9 to 16 with samples of 16, and
  for a DCT stream (SOF0 to SOF2), under JpgFile alone, 8 with samples of 8 bits and 12 with
  samples of 16. These are the profile's conditions for a coded size
  ([czi.md §3.2](../czi.md#32-conforming-and-unplaced-subblocks)), but for two,
  which the profile leaves unplaced for reasons of its own, and whose row is the `data` value
  that keeps their bytes (`ranges` of the file, for Zstd1). A lossless stream of three components under JpgFile is described, in a
  `jpeg` chain, which makes the components in the frame's order: the profile does not place it
  because nothing says which component is which, a question the description does not ask. And a
  Zstandard frame that declares no content size is described: the profile does not place it
  because only decoding it says how many bytes it holds, and, as of any stream the parser does
  not decode, one that holds other than the array's bytes is not detected. Any other subblock has
  its `shape` and `item_bits` and no grid: the IR cannot say what its bytes are. (czifile
  2026.8.16 reads some of them, as other arrays than the entry declares: data or a frame of fewer
  bytes as an array of fewer rows, an 8-bit JPEG or JPEG XR stream under Gray16 as 8-bit samples,
  and a Zstd1 frame of 8-bit samples whose header says hi-lo packing as the frame's content.)
- **A stream of another size.** A JPEG or JPEG XR stream may be of another size than its entry's
  stored size, as some writers leave a pyramid's edge
  ([czi.md §3.2](../czi.md#32-conforming-and-unplaced-subblocks)): the description
  gives the entry's, and the stream decodes to its own, which is what czifile reads.
- **No shape** either, nor `item_bits`, for a subblock whose copy of its entry does not agree with
  the entry that names it ([czi.md §2.4](../czi.md#24-subblocks)): the file declares
  it twice, differently, and a reader follows one or the other (czifile follows the directory's
  entry where it comes to the subblock from the directory, and the subblock's own where it walks
  the segments).
- **Not described**: a `ZISRAWSUBBLOCK` segment that no entry names in a file with a directory,
  of which the parser reads the segment header alone; a subblock's attachment (a valid-pixel
  mask); and the attachments that hold images (a `JPG` thumbnail, an embedded `CZI` label or
  preview), which the parser keeps as bytes and reads nothing of.
- **The grid** is dense: CZI has no fill value.
