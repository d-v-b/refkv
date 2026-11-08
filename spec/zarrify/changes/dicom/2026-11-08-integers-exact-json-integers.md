Changed 2026-11-08: an integer past 2^53 − 1 in magnitude (an `IS` value, and `SL SS UL US SV UV`) is a
JSON integer, exact, where it was the string of its digits; an `IS` value of more than 4300 digits
is its text ([§5.2](../../dicom.md#52-the-datasets-as-json)). The output of a file with such a value
changes.
