Changed 2026-11-08: an integer card, and `BSCALE`, `BZERO`, `BLANK`, `TSCALn`, `TZEROn`, `TNULLn` and
`TBCOLn` of one, past 2^53 − 1 in magnitude is a JSON integer, exact, where it was the string of
its digits ([§5.2](../../fits.md#52-an-hdus-node), [§5.3](../../fits.md#53-cards)). The output of a
file with such a card changes.
