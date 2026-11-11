Changed 2026-11-11: a tile-compressed image whose table's columns take more bytes a row than
`NAXIS1` is rejected ([§9.3](../../fits.md#93-checks)), where its rows, read past the table's end,
crashed the parser.
