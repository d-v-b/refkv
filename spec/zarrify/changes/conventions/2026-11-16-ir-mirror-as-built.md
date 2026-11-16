Changed 2026-11-16, with no change to any output: the IR mirror's text says what refkv's producer and
reader do where it said otherwise or nothing
([§8](../../../conventions.md#8-the-ir-mirror)). Of leaves that claim one byte, the one that starts
first keeps it, and canonical order decides only between those that start at one byte
([§8.8](../../../conventions.md#88-canonical-form)). A column names its column run by its position
among the rows of code 3 ([§8.2](../../../conventions.md#82-the-table)). The leaves of one member of a
run lie within its stride, and the gaps a producer adds are named `gaps/` with their offset as name index
([§8.1](../../../conventions.md#81-elements)). A producer may reject a source while it parses, with
`budget: more than N IR records` ([§8.3](../../../conventions.md#83-column-runs)). In a store's IR, a
held object has children and a length, chunks of one size have no length column, and Zarr v3 and
Precomputed stores are named among the stores ([§8.9](../../../conventions.md#89-stores)).
