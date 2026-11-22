# NRRD: stored arrays

What the NRRD parser describes as stored arrays ([stored-array.md](../../stored-array.md)), part of [the NRRD profile](../nrrd.md).

A NRRD input is one stored array, the one its
header declares, in the store IR the profile's run builds of the header file and, for a detached
header, its data file. It is keyed by the header file's object (the header has no element of its
own), with no index. The facts give the key: `array.declared_by` is the id of that object. Below,
the NRRD definition is Teem's, which
[zarrify/nrrd.md](../nrrd.md) cites.

- **Declarations.** The line of the `type` field, then the line of the `endian` field where the
  header has one: each the element `lines/<i>`, which holds the field's identifier, its descriptor
  and its line end.
- **`shape`** is the values of the `sizes` field, reversed: the data holds the header's last axis
  outermost. `axes` gives the header's order, the storage axes reversed (`[2, 1, 0]` for three
  axes).
- **`item_bits`** is eight times the bytes of one element of the `type` field's type
  ([nrrd.md §3](../nrrd.md#3-data-types-and-encodings)).
- **The grid** is one unit of the whole array: each axis `Each` of its size, under `short`. Its
  row is the `data` element of the data's file, whole: the header file for an attached header, and
  for a detached one the data file, another object of the store. For the raw encoding the element
  is the array's bytes, after the lines and bytes that `line skip` and `byte skip` pass over, or,
  with a `byte skip` of −1, those that end the file; bytes of the file after them are no part of
  the unit, as the NRRD definition says (pynrrd 1.1.3, which reads a file it is given by its path
  to its end, refuses one that has any). For the gzip encoding the element is the stream: from
  after the lines `line skip` passes over to the end of the file.
- **The chain** is one: empty for the raw encoding; `gzip` for the gzip encoding; and `skip`, then
  `gzip`, for the gzip encoding with a `byte skip` above 0, which the NRRD definition counts within
  the inflated bytes. `skip`'s `length` is the descriptor of the `byte skip` field, a range of the
  field's line. (pynrrd 1.1.3 skips those inflated bytes too, but first seeks as many bytes into
  the file, so it reads no such file that follows the definition.)
- **The stream is not inflated**, as the profile reads none of the data: the description is the
  same whatever the stream holds, and one that inflates to other than the skipped bytes and the
  array's is not detected. The array's bytes are the first the chain makes: the NRRD definition
  leaves what follows the data unread. (pynrrd 1.1.3 refuses a stream that inflates to more, and
  inflates the first member alone of a stream of several, which the `gunzip` program the NRRD
  definition names reads whole.)
- **No grid** for no input: the profile rejects what it does not read (the text, hex and bzip2
  encodings, the type `block`, several data files), and builds no IR of it.
- **The grid** is dense: NRRD has no fill value.
