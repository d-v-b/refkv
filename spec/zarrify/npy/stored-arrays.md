# NumPy: stored arrays

What the NumPy parser describes as stored arrays ([stored-array.md](../../stored-array.md)), part of [the NumPy profile](../npy.md).

A `.npy` file's array, and each stored
(method 0) or deflated (method 8) `.npz` member's whose header was read, is a stored array, keyed by
its `header` element, with no index; that element is its one declaration (its `descr`,
`fortran_order` and `shape`). Its key is found by its path: a `.npy` file's `header` element, or a
`.npz` member's `entries/<i>/data/header`, `i` the member's position in the archive's central
directory, which is its position among the source metadata's `members`, presented or not; a member
with no description (one whose header was not read) has no such element. For an array the profile
presents, the parser's description gives the key too: its entry of `arrays` holds `stored`, the
`header` element's id, beside its `path` and its header's text.
`shape` is the header's, reversed for a Fortran-order array of two or more axes, whose `axes` then
give the header's order; `item_bits` is eight times the size of an element of its `descr` as NumPy
lays it out, whether or not the profile presents the array: a type string (with its one-character
codes, such as `<h` or `?`; a subarray, `(2,3)<f4`; fields joined by commas, `<f8,<i4`) or a
structure (with its padding fields, fields of shape, fields whose type is a (type, shape) tuple, and
fields of no bytes, such as `|S0`, which add none). It is absent for a `descr` of no size: an object
array's; one of 0 bytes; one whose size is the machine's (`l`, `L`, `g`, `G`, `p`, `P`), which a
header does not give; and a structure with two fields of one name, which NumPy refuses. A presented
array is one unit, its `data` element, in an empty chain: each axis is `Each` of its length, or an
empty `List` for an axis of length 0 (which has no unit, and no row), under `short`, dense: NumPy
defines no fill value. An array whose data is not presented (its `descr` has no data type, or
its data does not fit) has no grid, nor, by the general fallback ([stored-array.md §1](../../stored-array.md#1-stored-arrays)), has one
whose element's bits would pass 2^64 (an array of no elements may be of such a type), whose
`item_bits` is then absent too. A deflated member's `header` and `data` elements lie in a
derived space, what its deflate stream inflates to
([npy.md §5.3](../npy.md#53-elements)): its unit is its `data` element there, in the same
empty chain, since inflating the stream is the IR's transform of the derived element and no codec
of the array. No range of the file holds such a unit; a projection references the derived
element's own bytes, the stream, and decodes them.
