Changed 2026-11-19: a column of a column run may take encoding 4, its values as arithmetic pieces
`(v, d, c)` in `column_values` ([§8.3](../../../conventions.md#83-column-runs)), and canonical form
gives it to a column that encodings 3, 0 and 2 do not hold and whose pieces, each as long as it can
be from member 0 on, are fewer than a third of its members
([§8.8](../../../conventions.md#88-canonical-form) step 6). The mirror tables of the fixtures with
such columns (185 of them, among them NIfTI extensions, ND2 metadata, MP4 PCM samples chunk by
chunk, Imaris and HDF4 elements) change and hold fewer rows; no other output changes. A reader
rejects a column of encoding 4 with a piece of no member, or whose pieces are not its column run's
members.
