Changed 2026-11-04: AV1 (`av01`) and VP9 (`vp09`) tracks are zarrified, through the `refkv.stream`
formats `"av1"` and `"vp9"`, and so are 10-bit H.264 and HEVC of 4:2:2 or of 12 bits, where each was
recorded with a reason ([§4.1](../../mp4.md#41-video-tracks), [§8.2](../../mp4.md#82-tracks)). A video track's source
metadata has the member `chroma` ([§5.2](../../mp4.md#52-the-tracks)).
