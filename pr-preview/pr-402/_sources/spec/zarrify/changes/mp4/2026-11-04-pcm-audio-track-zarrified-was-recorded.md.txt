Changed 2026-11-04: a PCM audio track is zarrified, where it was recorded with a reason: its samples
are the file's own bytes, through the `bytes` codec, with the sample entries `sowt`, `twos`, `raw `,
`NONE`, `in32`, `fl32`, `fl64`, `ipcm`, `fpcm` and `lpcm` ([§4.2](../../mp4.md#42-audio-tracks),
[§8.2](../../mp4.md#82-tracks)). A version 2 sound description of `lpcm` is parsed ([§2](../../mp4.md#2-boxes)), and a PCM
track of one sample size is one element a chunk of the file, not one a sample
([§5.3](../../mp4.md#53-elements)), so the 2^24 samples do not bound it ([§8.1](../../mp4.md#81-boxes)); so is a run of such
a track's track fragment.
