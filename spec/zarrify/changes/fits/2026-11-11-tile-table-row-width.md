Changed 2026-11-11: a tile-compressed image whose table's columns do not take `NAXIS1` bytes a row is
rejected ([§9.3](../../fits.md#93-checks)). Its rows were read at the columns' width: a smaller
`NAXIS1` crashed the parser, reading past the table's end, and a larger one read every row after
the first at the wrong offset.
