Changed 2026-11-21: the projection makes refkv's plan of a file or store (which tensors and shards
are presented, their paths and groups, each array's data type and fill value, the source metadata,
whether an index agrees) from the parser's description, which replaces the facts
([§8.5](../../safetensors.md#85-the-description), [§9.7](../../safetensors.md#97-the-description)).
A store's shard that passes the checks but is not presented for its key is now described in place,
as a presented one is, so the IR does not depend on the hierarchy ([§9.5](../../safetensors.md#95-the-ir-mirror)):
its mirror holds its length, header and tensors' data as elements, and its tensors' stored arrays
are keyed by its header rather than by its object. No other output changes.
