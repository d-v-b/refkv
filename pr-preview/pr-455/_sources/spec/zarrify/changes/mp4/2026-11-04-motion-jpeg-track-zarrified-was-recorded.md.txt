Changed 2026-11-04: a Motion JPEG track is zarrified, where it was recorded with a reason: its
pictures are one array, a picture a chunk, each chunk its sample's own JPEG stream through
`imagecodecs_jpeg`, with the sample entries `jpeg`, `mjpa`, `dmb1` and `mp4v` of object type
indication 0x6C ([§4.3](../../mp4.md#43-motion-jpeg-tracks), [§8.2](../../mp4.md#82-tracks)). A zarrifier reads the frame
header of such a track's first sample ([§8.1](../../mp4.md#81-boxes)).
