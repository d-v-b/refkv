Changed 2026-11-04: a binary table's `rows` is a `struct` array over its rows where its columns
tile its row, and stays bytes otherwise, the reason in its node's `not_struct`; a column's
`field` names its field ([§4](../../fits.md#4-tables), [§5.2](../../fits.md#52-an-hdus-node)).
