Changed 2026-11-08: an element's value that is an integer past 2^53 − 1 in magnitude is a JSON integer,
exact, where it was the string of its digits; one of more than 4300 digits is the string of the
text ([§6.1](../../safe.md#61-values)). The output of a product with such a value changes.
