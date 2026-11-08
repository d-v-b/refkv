Changed 2026-11-08: an integer or bitfield attribute past 2^53 − 1 in magnitude is a JSON integer,
exact, where it was the string of its digits ([§6](../../hdf5.md#6-attributes)); and so is a fill
value's integer whatever its place in the data type: an enumeration's, and a compound's integer or
enumeration member's, were the strings of their digits past 2^53
([§3](../../hdf5.md#3-datasets)). The output of a file with such an attribute or fill value
changes, and so does its mirror's view.
