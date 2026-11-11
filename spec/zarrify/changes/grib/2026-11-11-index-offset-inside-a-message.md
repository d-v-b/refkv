Changed 2026-11-11: an index that lists an offset inside a message, or one at which no message the
checks accept starts, is one the file disagrees with ([§8.2](../../grib.md#82-finding-the-messages)):
the run walks the file from there, and the root names no index. The run read the bytes inside a
message as a message of their own, and rejected the input for a listed offset the walk never
reaches, so the output depended on the index. And the walk reads a section longer than its 64 KiB
window whole, where it read the window again until the read budget rejected the input.
