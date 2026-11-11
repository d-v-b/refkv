Changed 2026-11-11: an index that lists an offset inside a message is one the file disagrees with
([§8.2](../../grib.md#82-finding-the-messages)): the run walks the file from that message's end,
and the root names no index. The run read the bytes at that offset as a message of their own, so
the output depended on the index.
