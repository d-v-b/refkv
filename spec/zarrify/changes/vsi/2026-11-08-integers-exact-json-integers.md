Changed 2026-11-08: a value field's 64-bit integer past 2^53 − 1 in magnitude is a JSON integer, exact,
where it was the string of its digits ([§6.1](../../vsi.md#61-the-root)). The output of a file
with such a value changes.
