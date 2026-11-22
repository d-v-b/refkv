Changed 2026-11-04: a fragmented file is zarrified, where it was rejected: a track's samples are its
tables' and then its track fragments', in the file's order ([§3](../../mp4.md#3-tracks-and-samples)), and the
moof boxes are parsed ([§2](../../mp4.md#2-boxes), [§5.3](../../mp4.md#53-elements)). A track's `duration` runs to where its
last sample ends when its fragments do ([§5.2](../../mp4.md#52-the-tracks)). The moof boxes together are at most
64 MiB, and a traf box needs a tfhd box and a track with a trex box ([§8.1](../../mp4.md#81-boxes)).
