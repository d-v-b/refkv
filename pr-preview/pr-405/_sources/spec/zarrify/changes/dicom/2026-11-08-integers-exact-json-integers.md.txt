Changed 2026-11-08: an `SV` or `UV` value past 2^53 − 1 in magnitude is a JSON integer, exact, where it
was the string of its digits; and an `IS` value is an integer only when it is a valid `IS`, of at
most 12 characters and from −2^31 to 2^31 − 1, and otherwise its text, where one up to 16 digits
was a number and a longer one the string of its digits; an `IS` value with one that is not valid
has no typed values in a gathered sequence's bounds, where one within 64 bits had
([§5.2](../../dicom.md#52-the-datasets-as-json)). The output of a file with such a value changes.
