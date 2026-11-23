# netCDF3: stored arrays

What the netCDF3 parser describes as stored arrays ([stored-array.md](../../stored-array.md)), part of [the netCDF3 profile](../netcdf3.md).

Every variable of the header is a
stored array, whether or not the profile presents it, keyed by the file's `header` element, a kind
of element that may declare several arrays: a variable's index is its position in the header's
variable list. The parser's description gives the key: its `elements` has the `header` element's
id, and the variable at position `k` of its `variables` is the array of index `k`
([netcdf3.md §7.7](../netcdf3.md#77-the-description)).

- **Declarations.** The range of the header that holds the variable's `nc_type`, 4 bytes, which
  names its type ([netcdf3.md §3.1](../netcdf3.md#31-data-types)). The format fixes the byte order (big-endian) and the
  order of the elements (the last index varying fastest), which no place declares.
- **`shape`** is the lengths of the variable's dimensions, in order, a record variable's first the
  record count ([netcdf3.md §7.3](../netcdf3.md#73-records)), which no place holds for
  a streaming file; empty for a scalar. `axes` is absent.
- **`item_bits`** is the bits of the type: 8, 16, 32 or 64. There is no `pad_from`.
- **The grid.** A fixed-size variable is one unit, each axis `Each` of its length, under `short`;
  its row is its data's element, `variables/<k>`. A record variable is a unit per record: `Each(1)`
  along the record axis and `Each` of its length along each other, under `short`; the row of
  record `r` is the `records` element, with the range of its slice, `r × recsize` past the
  variable's own offset in a record. A record variable of no records has no row.
- **The grid** is dense: the file stores every unit, a fixed-size variable's data and each of the
  records it counts, so the grid has no fill value. A netCDF-C file written without fill holds
  bytes in every unit all the same, whatever they are.
- **The chain** is empty: the format stores every value as it is.
