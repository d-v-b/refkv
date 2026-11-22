Changed 2026-11-17: a dataset of `char8` is an array of `null_terminated_bytes` strings of its last
axis's length, where it was not presented (`char8`) ([§3.3](../../hdf4.md#33-text)); a Vdata of a
writer's class is a table, an array of a `struct` per record or, of fields not interlaced, a group of
an array per field, where it was not presented (`table`) ([§4](../../hdf4.md#4-tables)); the root's
members include the tables the walk does not reach ([§2.1](../../hdf4.md#21-groups)); the new reasons
are a table's ([§8.4](../../hdf4.md#84-what-is-not-presented)), and its source metadata is given
([§8.2](../../hdf4.md#82-datasets)). The sections after [§3](../../hdf4.md#3-datasets) are renumbered by one.
