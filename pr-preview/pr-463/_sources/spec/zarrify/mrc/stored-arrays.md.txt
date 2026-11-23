# MRC: stored arrays

What the MRC parser describes as stored arrays ([stored-array.md](../../stored-array.md)), part of [the MRC profile](../mrc.md).

An MRC file's data is one stored array, keyed by
the file's `header` element, with no index. The facts give the key: `declared_by` is that element's
id. The dataset layout ([spec/zarrify/mrc-dataset.md](../mrc-dataset.md)) is a second layout of
the same file, and has no description of its own.

- **Declarations.** Ranges of the `header` element, in this order: `machst`, (212, 4), whose first
  byte gives the byte order of the header's fields and of the values; `mode`, (12, 4); and, of a
  mode 0 file, whose values' sign they decide
  ([mrc.md §3](../mrc.md#3-data-types)), `nversion`, (108, 4), IMOD's stamp, (152, 4),
  and IMOD's flags, (156, 4).
- **`shape`** is `nz`, `ny` and `nx`, the sections, the rows and the columns, whatever the kind of
  file ([mrc.md §4.1](../mrc.md#41-the-array)): an image is one section, and a volume
  stack's sections are those of all its volumes, which the profile cuts by `mz`. `axes` is
  `[2, 1, 0]`: the header gives `nx` first. The axis map (`mapc`, `mapr`, `maps`) names the axes
  and reorders no value, so it changes neither.
- **`item_bits`** is the mode's: 8 for mode 0; 16 for modes 1, 6 and 12; 32 for mode 2, and for
  mode 3, whose element is a pair of 16-bit integers; 64 for mode 4, a pair of 32-bit floats; and
  4 for mode 101, with `pad_from` 2, the columns' axis: each row ends on a whole byte.
- **The grid** is one unit, the whole array: each axis `Each` of its length, under `short`. Its
  row is the `data` element. The profile's checks
  ([mrc.md §8.1](../mrc.md#81-header)) leave no axis of length 0, and no data that
  is not whole in the file, so no array of an MRC file lacks a grid but by the limits of
  [stored-array.md §6](../../stored-array.md#6-in-the-ir).
- **The chain** is empty, but for mode 101's, `lsb_first` with `bits` 4: a row's first value is
  the low half of its first byte. A row of an odd `nx` ends on half a byte of padding, the high
  half of its last byte, which decodes to the low half, after the row's last value, as `pad_from`
  says.
- **A file gzipped whole** ([mrc.md §8.4](../mrc.md#84-gzipped-files)) has no
  `header` element: its bytes are the stream, which holds the header. Its array is keyed by its
  `data` element, the stream, as `declared_by` says, and that element is its one unit. It has no
  declarations: the header is the first 1024 bytes the stream inflates to, which no place names:
  the parser gives the inflated bytes no derived space yet. Its `shape`, `item_bits` and `pad_from` are those
  of the header the parser inflated. Its chain ends with `skip`, then `gzip`: the values are what
  the stream inflates to, after the header and the extended header. `skip`'s `length`, 1024 plus
  `nsymbt`, is a value, since no place holds `nsymbt` either.
- **The grid** is dense: MRC has no fill value.
