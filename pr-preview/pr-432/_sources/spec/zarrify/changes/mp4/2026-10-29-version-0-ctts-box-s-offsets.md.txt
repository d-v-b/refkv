Changed 2026-10-29: a version 0 `ctts` box's offsets are signed, as version 1's are, where they
were unsigned: Apple's encoder writes negative ones, which made each picture they belong to seem
presented after every later sync sample, and the track's video was not zarrified
([§2](../../mp4.md#2-boxes), [§3](../../mp4.md#3-tracks-and-samples)).
