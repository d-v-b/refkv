Changed 2026-11-04: a mode 0 file with IMOD's stamp is `int8` when bit 0 of IMOD's flags is set
or `nversion` declares the standard, and `uint8` otherwise ([§3](../../mrc.md#3-data-types)); the earlier
rule read the bit inverted, against IMOD's documentation, so a stamped mode 0 file's data type
changes.
