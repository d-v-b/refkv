Changed 2026-10-28: HEVC video tracks (`hvc1` and `hev1` sample entries) are zarrified as H.264
tracks are: 4:2:0 of 8 bits (Main) or 10 (Main 10, an iPhone's HDR video), the planes `uint8` or
`uint16`, the `refkv.stream` format `"hevc"` with the `hvcC` box as its setup
([§4.1](../../mp4.md#41-video-tracks), [§8.2](../../mp4.md#82-tracks)); their IRAP pictures are the sync samples, and
their RASL and RADL pictures leading pictures, under the rules H.264's open groups follow
([§8.3](../../mp4.md#83-chunks)). A video chunk holds at most 32 MiB of luma counted in bytes, so half as
many 10-bit pictures as 8-bit ones; a video track's source metadata has its `bit_depth`
([§5.2](../../mp4.md#52-the-tracks)).
