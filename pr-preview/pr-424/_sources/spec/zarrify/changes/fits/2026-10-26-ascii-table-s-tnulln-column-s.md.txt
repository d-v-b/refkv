Changed 2026-10-26: an ASCII table's `TNULLn` is its column's `null`, a string, where it was
`null`, and an integer card of an HDU's or a column's member past 2^53 − 1 is the string of its
digits, as in the cards, where it was `null` ([§5.2](../../fits.md#52-an-hdus-node)); the layout is
tabulated, its entries in [Layout](../../fits.md#layout) and its types in type tables
([conventions §11.5](../../../conventions.md#115-layout-tables)), and the text says what refkv already
wrote: a table's `rows` array is present, without chunks, when `NAXIS1 × NAXIS2` is 0
([§4](../../fits.md#4-tables)); a compression's `dither_seed` is `null` without `ZDITHER0`
([§5.1](../../fits.md#51-the-root)); a real card value that is not finite in binary64 is `{"text": t}`
([§5.3](../../fits.md#53-cards)); and a tile-compressed image's summary carries its `compression`
([§9.5](../../fits.md#95-summary)).
