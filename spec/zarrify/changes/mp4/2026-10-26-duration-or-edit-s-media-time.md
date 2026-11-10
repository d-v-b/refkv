Changed 2026-10-26: a duration or an edit's `media_time` past 2^53 − 1 in magnitude is the
string of its digits, as [conventions §6](../../../conventions.md#6-source-metadata-as-json) writes an
integer, where it was a JSON number ([§5](../../mp4.md#5-source-metadata)); a track whose ID an earlier
zarrified track has is not zarrified, its reason saying so, where both were written to one group
([§8.2](../../mp4.md#82-tracks)). The layout is tabulated, its entries in [Layout](../../mp4.md#layout) and its types in
type tables ([conventions §11.5](../../../conventions.md#115-layout-tables)); the summary is a table
too. And the text says what refkv already wrote: a four-character code is a plain string of its
bytes' code points ([§5](../../mp4.md#5-source-metadata)), and a chunk of `time` is not a `refkv.stream`
chunk ([§8.3](../../mp4.md#83-chunks)).
