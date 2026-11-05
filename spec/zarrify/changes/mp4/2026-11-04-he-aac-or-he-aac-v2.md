Changed 2026-11-04: an HE-AAC or HE-AAC v2 track is zarrified, where it was recorded with a reason
or, signaled backward-compatibly, declared at half its rate: its frames decode to 2048 samples
([§4.2](../../mp4.md#42-audio-tracks), [§8.2](../../mp4.md#82-tracks)), a chunk's lead-in is 64 frames
([§8.3](../../mp4.md#83-chunks)), and its noise is a chunk's own
([conventions §10](../../../conventions.md#10-coded-streams)).
