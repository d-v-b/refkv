Changed 2026-11-08, with no change to any output: a header field's integer is a JSON integer, exact
whatever its magnitude ([§5.1](../../mrc.md#51-the-root),
[conventions §6](../../../conventions.md#6-source-metadata-as-json)); MRC's integer fields are of
32 bits, so none was past 2^53 − 1.
