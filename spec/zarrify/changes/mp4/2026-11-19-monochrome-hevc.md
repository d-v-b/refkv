Changed 2026-11-19: an HEVC track of 4:0:0 (monochrome) pictures of 8, 10 or 12 bits, which was
recorded as not zarrified, is zarrified, its `y` array alone, its `chroma` `"4:0:0"`
([§4.1](../../mp4.md#41-video-tracks), [§8.2](../../mp4.md#82-tracks)); its samples' stored arrays
are one plane where they were the `source` fallback
([stored-array §7](../../../stored-array.md#7-the-formats)). Janelia's H5J files hold such tracks.
The rejection message of a track of no zarrified track names 4:0:0. The fixture
`mp4_hevc_gray12.mp4` has such a track.
