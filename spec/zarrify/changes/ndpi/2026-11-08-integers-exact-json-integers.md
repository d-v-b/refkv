Changed 2026-11-08: `ExposureTime` past 2^53 − 1 is a JSON integer, exact, where it was the string of its
digits ([§5](../../ndpi.md#5-source-metadata)), and so are such integers in the mirror's view. The
output of a file with such a value changes.
