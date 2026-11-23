# NIfTI: stored arrays

What the NIfTI parser describes as stored arrays ([stored-array.md](../../stored-array.md)), part of [the NIfTI profile](../nifti.md).

A NIfTI file's voxels are one stored
array, of a single file, of a file gzipped whole and of a header-and-image pair alike, as nibabel
reads them. The facts give its key: `declared_by` is the id of the element that declares it. Below,
`n` is the header's `dim[0]`, and `v` its `vox_offset`.

- **A single file's array** is keyed by its `header` element, with no index. That element is its
  one declaration: the header, whose `sizeof_hdr` gives the file's byte order, with its `dim`, its
  `datatype` and `bitpix`, and what else a profile reads of the voxels (`scl_slope`, `scl_inter`).
- **`shape`** is `dim[n]` to `dim[1]`: the header lists its dimensions the fastest-varying first,
  so `axes` is `n − 1` down to 0 for an `n` of 2 or more, and absent for 1. Each of the
  `n` dimensions is an axis, the sixth and the seventh too, which the parser accepts only at 1;
  the values of `dim` past `dim[n]` are not read.
- **`item_bits`** is the bits of a voxel of the header's `datatype`, which `bitpix` repeats (the
  parser refuses a header where the two differ). A voxel is one element: an RGB24 or RGBA32 voxel
  of 24 or 32 bits, a complex one of 64 or 128.
- **The grid** is one unit: every axis `Each` of its length, under `short`. Its row is the
  `voxels` element, the voxels' bytes from `v`, in an empty chain.
- **A pair's array** is the single file's, described by the pair's store parser itself
  ([stored-array.md §6](../../stored-array.md#6-in-the-ir)): its key and its declaration are the `header` element of the header file,
  and its row the `voxels` element of the image file, where `v` counts.
- **A gzipped file's array** has the single file's `shape`, `axes`, `item_bits` and grid, but its
  header lies in its stream, where no element holds it. It is keyed by the file's one element,
  `voxels`, the whole file, and has no declarations: a profile reads the header from the facts'
  `gzip.head`, the stream's first bytes as the parser inflated them. Its row is that element, in
  the chain `skip`, `gzip`: the stream inflates to the header, the extender and the extensions,
  then the voxels, and `skip`'s `length` is `v`, a value, since no place holds the field. The
  parser inflates the start of the stream alone, and accepts the file only where the size its
  trailer records is `v` plus the voxels' bytes, modulo 2^32.
- **No grid** for no file the parser accepts: it refuses one whose voxels it does not read as one
  array (a `datatype` it has no size for, a dimension below 1, a sixth or seventh dimension above
  1, voxels the file does not hold).
- **The grid** is dense: NIfTI has no fill value.
