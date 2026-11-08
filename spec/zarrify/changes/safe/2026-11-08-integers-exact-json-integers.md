Changed 2026-11-08: an element's value that is an integer from −2^63 to 2^64 − 1 is a JSON integer,
exact, where one past 2^53 − 1 in magnitude was the string of its digits; one outside that range,
which the convention sets since the products' metadata declares none, is the string of the text
([§6.1](../../safe.md#61-values)). The output of a product with such a value changes.
