# ND2: stored arrays

What the ND2 parser describes as stored arrays ([stored-array.md](../../stored-array.md)), part of [the ND2 profile](../nd2.md).

An ND2 file's frames are one stored array, and
a frame is a unit of it, not an array of its own: the source names the frames as one sequence
(the chunks `ImageDataSeq|<f>!`), declares one layout for all of them (`ImageAttributesLV!`) and
none in a frame's own chunk, and counts them by its experiment's loops. A frame is found by its
number in the chunk map, as an HDF5 chunk is by its index, and a frame the map does not name is a
unit the source does not hold. The array is keyed by the `ImageAttributesLV!` chunk's element
(`chunks/ImageAttributesLV!`), with no index, in every file, whichever frames it holds. Each frame
chunk the map names at or past the loops' count (a `frames/<f>` of the IR), which the image leaves
out ([nd2.md §4.1](../nd2.md#41-frames)), is an array of one frame, keyed by the
`frames` element, a kind of element that may declare several arrays: its index is its position
among those chunks, in the order of their numbers. The facts give the keys: `declared_by` is the id of the
`ImageAttributesLV!` chunk's element, and `unplaced` has `declared_by`, the id of `frames`, and
`count`, the arrays it keys.

- **Declarations.** The `lv` element of the `ImageAttributesLV!` chunk (its data: a lite variant,
  or the compressed record of one), then that of `ImageMetadataSeqLV|0!` where the file has one
  with data: the attributes give a sample's size and a pixel's components, and the picture's
  planes say what each component is. The experiment, which says what a frame's number means,
  declares nothing of a sample: the profile reads it.
- **`shape`** is the frames the loops count, N (the product of the loops' counts as
  [nd2.md §3](../nd2.md#3-metadata) flattens the experiment, 1 with no loops), then
  `uiHeight`, `uiWidth` and, where `uiComp` is more than 1, `uiComp`, a pixel's components being
  stored together. The array of a frame past the count has no first axis. `axes` is absent: ND2
  names its dimensions by member, and lists them in no order.
- **`item_bits`** is `uiBpcInMemory`: 8, 16 or 32.
- **The grid** is under `short`. Where a frame's rows are stored end to end (`uiWidthBytes` is the
  bytes of a row's samples), or the frames are compressed, a frame is a unit: `Each(1)` on the
  first axis, and `Each` of its length on every other. Where `uiWidthBytes` is more, each row's
  samples are followed by bytes that are no sample, and a row is a unit, `Each(1)` on the rows'
  axis too: the source holds one row's samples together and no two rows', so a unit of several
  rows would either hold bytes that are no element or be bytes a recipe joins
  ([stored-array.md §1](../../stored-array.md#1-stored-arrays)).
- **Rows.** Frame `f` below N, where the map names it, is at `f` on the first axis: its
  `frames/<f>/pixels` element, which starts after the frame's 8-byte timestamp, with `member`
  where the frames are a run. Of padded rows, row `r` is the `range` (`r` × `uiWidthBytes`, the
  bytes of a row's samples) of that element, taken before its recipe, which joins the rows
  ([stored-array.md §4.2](../../stored-array.md#42-rows)); one row whose samples end the file is the element alone. A frame past N is
  a `range` of its `frames/<f>/data` element, which holds the timestamp too: the rows' bytes
  from 8, or the stream, from 8 to the data's end; or, of padded rows, each row's samples from
  8 + `r` × `uiWidthBytes`.
- **The chain** is one for the file: empty, or `zlib` where `eCompression` is 0. The parser
  inflates no frame: a stream that does not inflate to the frame's bytes is described as one that
  does.
- **No grid** for a frame chunk past N that holds no frame as the attributes say: one whose
  header is no chunk header within the file; one whose header gives fewer bytes of data than the
  timestamp and `uiHeight` rows of `uiWidthBytes` (of compressed frames, no byte after the
  timestamp); and one whose rows' samples, or whose stream, the file does not hold to their end.
  Its description keeps its shape and `item_bits`. Below N the profile rejects such a file. An
  array whose rows would pass the chunk map's limit ([stored-array.md §6](../../stored-array.md#6-in-the-ir)) falls back before they
  are made: frames of padded rows that share their bytes have a row for each row of each.
- **Where readers differ.** A frame's bytes follow its chunk's name, of the length the chunk's
  header gives, as the nd2 library's chunk reader reads a chunk; nd2's frame reader takes every
  frame's name to be 4072 bytes, as NIS-Elements writes it, whatever the header says, and reads
  the same bytes only of such a file. The header's is the format's own declaration, so the frame
  keeps its grid. nd2 also counts the loops of some experiments otherwise (it reads a time loop
  whose periods have no validity list as no period, and the convention as every period), which
  moves no frame: N says only which frames the first array holds. And nd2 takes as a frame's
  number whatever Python's `int` reads after `ImageDataSeq|`, where the convention
  ([nd2.md §4.1](../nd2.md#41-frames)) takes a decimal number without leading zeros:
  in a file that names both `ImageDataSeq|0!` and `ImageDataSeq|00!`, nd2 reads the second as
  frame 0, and the description, with the profile, the first. The description follows the
  convention there, and no file a writer is known to make has such a name.
- **The grid** of the frames the loops count is sparse, a frame the chunk map does not name
  being a unit the source does not hold, and that of a frame past them dense: ND2 has no fill
  value.
