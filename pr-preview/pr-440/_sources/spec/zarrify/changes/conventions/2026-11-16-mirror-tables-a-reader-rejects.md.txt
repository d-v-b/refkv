Changed 2026-11-16, with no change to any output: a reader rejects a table of the IR mirror whose
`version` is not `2`, whose `rows` is not the number of rows the array `rows` stores, or whose
`elements` is not the number of rows they expand to ([§8.2](../../../conventions.md#82-the-table)).
The 65536 bytes of a document of the view are what the members of its children spend: the members
`$vz`, `$form` and `$partial` spend none of them, and the last child's member may be larger than
what is left, so a document may hold more than 65536 bytes, as refkv's producer writes it
([§8.7](../../../conventions.md#87-the-view)).
