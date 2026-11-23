Changed 2026-10-23: the array's codec is `refkv.stream`
([conventions §10](../../../conventions.md#10-coded-streams)), which MP4 tracks share, in place of
this convention's own `mp3`; its data type is `float32`, the decoder's own samples, in place of
`int16`; a chunk's header gives each frame's size and output position, in place of the count of
samples to keep ([§4.1](../../mp3.md#41-the-array), [§8.2](../../mp3.md#82-chunks)).
