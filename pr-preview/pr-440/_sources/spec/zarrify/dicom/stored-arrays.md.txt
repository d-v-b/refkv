# DICOM: stored arrays

What the DICOM parser describes as stored arrays ([stored-array.md](../../stored-array.md)), part of [the DICOM profile](../dicom.md).

A DICOM file's stored arrays are its
Pixel Data (7FE0,0010), each lookup table of a `PALETTE COLOR` image, and each Pixel Data in an
item of a sequence the parser walks (an icon image's), whether or not the profile presents them.
The facts give the keys: `declared_by` is the id of the top-level Pixel Data's element;
`palette.declared_by` lists the ids of the red, green and blue tables' elements, where the tables
are arrays; and `nested` lists each element that keys the Pixel Data of its items, by its id
(`declared_by`), with how many it keys (`count`). A series' store form is composed of its files'
outputs, and has no descriptions of its own. Below, an attribute is the first element of its tag in
the Pixel Data's own dataset, absent where its value is empty (and, of an item's, where its VR in
explicit VR is not the one [dicom.md §2.2](../dicom.md#22-attributes) gives it, or
the IR does not hold its value); N is Number of Frames (0028,0008), 1 without the attribute, and S
is Samples per Pixel (0028,0002).

- **The top-level Pixel Data** is keyed by its element (`dataset/7FE00010`), with no index.
- **Declarations.** The element of the Transfer Syntax UID (`meta/00020010`), which gives the
  byte order of native samples and names the frames' encoding; then each of these elements of the
  dataset that has a value, a data element whole (its header, with its VR in explicit VR, and its
  value): Photometric Interpretation (0028,0004), Bits Allocated (0028,0100), Bits Stored
  (0028,0101), High Bit (0028,0102), Pixel Representation (0028,0103), Pixel Padding Value
  (0028,0120), Pixel Padding Range Limit (0028,0121), and the palette's descriptors
  (0028,1101)–(0028,1103), tables (0028,1201)–(0028,1203) and segmented tables
  (0028,1221)–(0028,1223).
- **`shape`** is N, where the dataset has a Number of Frames (an axis of 1 for a value of 1,
  and no axis without the attribute); then Rows (0028,0010) and Columns (0028,0011); and, for S
  above 1, S: after Columns where a pixel's samples are stored together, and before Rows where
  each sample is a plane (native data of Planar Configuration (0028,0006) 1, and RLE data, whose
  codec makes planes). A whole-slide image's frames are its tiles, and the array is the frames:
  placing them in the total pixel matrix is the profile's. `axes` is absent: DICOM names its
  dimensions by tag, and lists them in no order.
- **`item_bits`** is Bits Allocated.
- **The grid** is a frame a unit: `Each(1)` on the frames' axis and `Each` of its length on every
  other, under `short`. Native frames of Bits Allocated 1 follow each other with no padding
  ([dicom.md §7.5](../dicom.md#75-frames)): where N is above 1 and Rows × Columns is
  no multiple of 8 they are one unit together, `Each(N)` on the frames' axis.
- **Rows.** A native frame is the `frames/<f>` element: a member of the run the frames fold into
  (`frames/0`, with `member` f), or `frames/0` alone where the file holds one unit. The bytes of
  the value past the frames are no unit's. An encapsulated frame is the data of those of its
  fragments ([dicom.md §7.5](../dicom.md#75-frames)) that are not empty: the
  `fragments/<j>` element where one fragment holds it (with an Extended Offset Table, frame f's is
  `fragments/<f>`), and `ranges` of the source, a fragment's data each, in order, where several
  do.
- **The chain** is one, by the transfer syntax
  ([dicom.md §2.1](../dicom.md#21-transfer-syntax)): empty for native data, or
  `lsb_first` with `bits` 1 for Bits Allocated 1, in every byte order; `jpeg`, with no `tables`,
  for JPEG Baseline (1.2.840.10008.1.2.4.50), of Bits Allocated 8, and for lossless JPEG (.4.57
  and .4.70), whose Bits Allocated the parser takes as 8 or 16, every frame's precision, which it
  reads, at most that; `jpegls` for JPEG-LS (.4.80 and .4.81); `jpeg2k` for JPEG 2000 (.4.90 and
  .4.91); `dicom_rle` for RLE Lossless (1.2.840.10008.1.2.5); and `source`, with `format` `dicom`
  and `id` the Transfer Syntax UID, for HTJ2K (.4.201 to .4.203), which the list has no entry
  for. The description follows the attributes: a stream's own frame header is compared with them
  only where the profile does
  ([dicom.md §7.5](../dicom.md#75-frames)), and a stream decodes to its own. A JPEG
  stream of three samples is described as the file holds it, without the marker the profile
  writes before it.
- **No grid** for JPEG 2000 frames of Photometric Interpretation `YBR_FULL_422`, which the parser
  accepts only with chroma components of half the width: a frame's samples are then not an array
  of the image's shape, and what a decoder makes of them is not the stream's samples (OpenJPEG
  upsamples the chroma and converts to RGB). The parser refuses what the profile does not read
  (another transfer syntax, Float Pixel Data, a sparse tiling), and builds no IR of it.
- **A lookup table** of a `PALETTE COLOR` image whose tables the profile reads
  ([dicom.md §4.1](../dicom.md#41-the-palette): none segmented) is keyed by its own
  element (`dataset/00281201`, `dataset/00281202`, `dataset/00281203`), with no index. Its
  declarations are the Transfer Syntax UID's element and its descriptor's (`dataset/00281101` to
  `dataset/00281103`); `shape` is its entries, the descriptor's first value (65536 for 0);
  `item_bits` is 16 where the table's value holds two bytes an entry, else 8; and it is one unit
  in an empty chain, `Each` of its entries under `short`: the range of the table's element that
  holds the entries, after its header, the bytes of the value past them left out. Entries of one
  byte in a table of VR OW under Explicit VR Big Endian, whose 16-bit words that syntax swaps,
  have no grid. The tables beside a segmented one are not arrays: they are among the Pixel Data's
  declarations.
- **A Pixel Data in an item** is keyed by the element of the top-level dataset its items lie in
  (the sequence `dataset/<tag>`, whatever the depth of the item), a kind of element that may
  declare several arrays: its index is its position among the Pixel Data of that element's items,
  in the order of the file, each member of a run of items counted. Its declarations, `shape` and
  `item_bits` are as the top-level one's, read of its own item's attributes, the declaring
  elements with `member` where the items are a run; `shape` is absent where Rows, Columns or S is
  absent or 0, where S is above 1 and Planar Configuration is neither 0 nor 1, and where Number of
  Frames is not an integer of at least 1, and `item_bits` where Bits Allocated is absent or 0.
  A value of defined length is native, whatever the transfer syntax (PS3.5
  [A.4](https://dicom.nema.org/medical/dicom/current/output/chtml/part05/sect_A.4.html)), and has
  the grid and the chain of native frames, each unit a `range` of the Pixel Data's element, after
  its header (with `member` in a run), where the profile's rules for the top-level one hold of it
  ([dicom.md §3](../dicom.md#3-the-image)): Bits Allocated 8, 16 or 32, or 1 with S
  1; S 1, or 3 with a Photometric Interpretation of `RGB` or `YBR_FULL`; and a value that holds
  the frames. It has no grid otherwise, nor where its item's byte order is not the transfer
  syntax's, nor, under Explicit VR Big Endian, for Bits Allocated 1 or 8, whose bytes a VR of OW
  swaps. A value of undefined length, a fragment sequence the parser does not read as frames, has
  no grid.
- **The grid** is dense: DICOM has no fill value, and Pixel Padding Value is a declaration.
