Changed 2026-10-27: an ASCII value is never in line, whatever its size, and is at its 64-bit
offset ([§5.1](../../ndpi.md#51-elements), [§7](../../ndpi.md#7-ndpi-profile)); McuStartsHighBytes, like the tile tables'
high bytes, holds the 32-bit values it stores, and McuStarts of one in-line LONG, whose value
has its entry's high word, MUST have that word in McuStartsHighBytes, which is not added to it
([§7](../../ndpi.md#7-ndpi-profile)).
