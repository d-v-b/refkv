# GRIB: stored arrays

What the GRIB parser describes as stored arrays ([stored-array.md](../../stored-array.md)), part of [the GRIB profile](../grib.md).

Every message of a GRIB edition 2 file has
descriptions, whether or not the profile presents its fields, keyed by its element
(`messages/<i>`), a kind of element that may declare several arrays: each bitmap one of its
sections 6 holds (indicator 0) and each of its fields (a section 7, with the sections 3, 5 and 6
in effect at it) is an array, indexed in the order of their sections, a bitmap before its field.
The file's store parser describes them itself ([stored-array.md §6](../../stored-array.md#6-in-the-ir)). A field's array is the values
its section 7 codes, in the file's order: how fields stack into a variable, where on its grid a
bitmap puts a field's values, and which way a grid's rows run are the profile's. The facts give
the keys: each entry of `fields` has `declared_by`, the id of its message's element, `array`, the
index of its values' array, and `mask`, the index of the bitmap that says which points its values
are (its own, or, under indicator 254, the message's last one before it), or null. Below, an octet
is numbered from 1 in its section, a section is the element `messages/<i>/sections/<j>`, and `n`
is section 5's number of values (octets 6–9).

- **Declarations.** A field's: its section 3 (the grid's number of points, its template, `Ni`,
  `Nj` and the scanning mode); its section 5 (`n`, the data representation template and its
  octets: the reference value `R` at octets 12–15, the binary scale factor `E` at 16–17 and the
  decimal scale factor `D` at 18–19, the type of the original values, complex packing's missing
  value management at octet 23 and its substitutes for missing values at 24–31); the first 6
  octets of its section 6, which end
  with the bitmap indicator (the section's element, or the `header` element of a section that
  holds more); and, where a bitmap says which points its values are, that bitmap's bits (the
  `bitmap` element of its section 6). A bitmap's: the section 3 in effect, and the first 6
  octets of its section 6, likewise.
- **A bitmap** is a bit for each point of the grid, in the file's order, 1 where the point has a
  value: `shape` is section 3's number of points (octets 7–10), one axis, and `item_bits` is 1. It
  is one unit in an empty chain, `Each` of the points (an empty `List` for none) under `short`,
  and its row is the section's `bitmap` element, the octets after the first 6, or the range of its
  first octets that hold the points' bits where it holds more. A bitmap whose section holds fewer
  bits than the grid has points has no grid.
- **A field's `shape`.** Where every point has a value (indicator 255, and `n` the grid's number
  of points) and the grid is one whose rows the parser reads (template 3.0, 3.1, 3.10, 3.20, 3.30
  or 3.40, defined by its template, with no list of the points of each row, and `Ni` × `Nj` its
  number of points), `shape` is `Nj`, then `Ni`, with `axes` `[1, 0]`, since section 3 gives `Ni`
  first; or, where the scanning mode's bit 3 (`0x20`) says that consecutive points run along a
  column, `Ni`, then `Nj`, with no `axes`. Rows that alternate in direction (bit 4, `0x10`) are as
  the file stores them. Every other field is one axis of its `n` coded values, in order: one on a
  reduced grid, on a 3.101 grid or on a grid the parser does not read; one whose `n` is not the
  grid's number of points; and one with a bitmap, of any indicator. A field with a bitmap is the
  values of the points the bitmap keeps, and no value is made for a point it leaves out: the
  bitmap's own array, or for a predefined bitmap (indicator 1–253) the indicator, says which they
  are.
- **Units.** A field is one unit: each axis `Each` of its length, or an empty `List` for an axis
  of length 0, under `short`. Its row is its section 7's element with the range of the octets
  that hold its values, which start after the section's first 5; a field of no values, and one
  whose values no octet codes, has no row.
- **IEEE floats** (template 5.4, of the precision 1 or 2 at octet 12) are stored as they are:
  `item_bits` is 32 or 64, the elements big-endian, the chain empty, and the row's range the
  `n` values' octets.
- **A constant field** codes no value: simple packing, JPEG 2000, PNG or CCSDS packing (5.0, 5.40,
  5.41, 5.42) of a bit width of 0 (octet 20), and complex packing (5.2, 5.3) of no groups (octets
  32–35). Every value is the reference value `R`, an IEEE binary32, big-endian: `item_bits` is
  32, the chain is empty, and the grid is sparse, with no row, its fill given: the place of `R`
  (section 5's octets 12–15). ecCodes and NCEP's g2c read `R` itself whatever the decimal scale factor `D` is,
  as their encoders write it, where FM 92's regulation 92.9.4 would give `R × 10^−D`.
- **A field in simple packing** (5.0) is its integers as section 7 stores them: `item_bits` is
  the bit width (octet 20), whatever it is, the chain empty, and the row's range the octets that
  hold `n` integers.
- **A packed field** is the integers its packing codes, in the codec of the integers, each
  parameter a range of section 5 ([stored-array.md §3.2](../../stored-array.md#32-the-list)): `grib_complex` for 5.2 and for 5.3, with
  `differencing` (missing value management 0; widths of at most 64 bits; an order of 1 or 2, and
  descriptors of at most 8 octets), `item_bits` 64; `jpeg2k` for 5.40, whose codestream's samples,
  in the image's order whatever its rows, are the unit's integers, `item_bits` the bit width in
  the whole octets that hold it (8 × ⌈`bits` / 8⌉); `grib_png` for 5.41 and `grib_ccsds` for 5.42
  (unsigned samples of at most 32 bits), `item_bits` the bit width. The row's range is all of
  section 7 after its first 5 octets. The parser reads no octet of section 7 after them, so a
  stream that is not its codec's, or holds other than `n` integers, or a PNG image of other pixels
  than the bit width's, is not detected.
- **Values.** The description stops at the integers `X`, which the profile makes the field's
  values of with `R`, `E` and `D`, its declarations: FM 92's regulation 92.9.4 gives
  `(X × 2^E + R) × 10^−D`. ecCodes computes it in binary64, `X` and `R` converted first, each
  operation rounded, in that order, with `2^E` 1 multiplied by 2 as many times as `E`'s magnitude
  (divided, for a negative `E`), and `10^−D` 1 divided by 10 as many times as `D`'s magnitude
  (multiplied, for a negative `D`); `R` is an IEEE binary32, big-endian, and `E` and `D` are each
  an integer whose first bit is its sign and whose other 15, the most significant first, are its
  magnitude.
- **`source`**, with `format` `grib`, `id` the data representation template's number and
  `parameters` the place of section 5's octets after it (from octet 12), for any other template
  (the spectral 5.50 and 5.51, 5.61, 5.200), and for one of those above with parameters its codec
  does not take (IEEE floats of another precision, complex packing of other options, signed CCSDS
  samples, a section 5 shorter than its template's octets). Complex packing with groups and
  missing value management 1 or 2 is `source` too: a coded value whose bits are all 1 (in a group
  of width 0, a group's reference whose bits are all 1) is a missing value, and under 2 so is one
  that is 1 less, for which the field codes no integer, so that an element would have to be made
  for it. The field then has no `item_bits`, and its row's range is section 7 after its first 5
  octets.
- **No grid** for a field whose section 7 holds fewer octets than its IEEE floats or its simple
  packing's integers take, and for one of another packing, named or `source`, with values to code
  and a section 7 of 5 octets: no unit of no bytes is named.
- **The grid** is dense, but a constant field's: GRIB has no fill value.
