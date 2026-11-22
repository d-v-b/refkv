Changed 2026-10-24: a level without McuStarts whose strip's frame header is not the level's size
has the level's size spliced into the frame header when it needs the same MCU rows and columns,
and is otherwise not a level (rejected when it is level 0) ([§3](../../ndpi.md#3-levels),
[§4](../../ndpi.md#4-the-image)); the profile reads such a strip's header ([§7](../../ndpi.md#7-ndpi-profile)).
