Changed 2026-10-25: a track whose sync samples are followed in decode order by pictures
presented before them (leading pictures: a camera's B-frames after each IDR picture, or an open
group's) is zarrified, not refused, while each leading picture is presented after the sync
sample before; a chunk that holds a leading picture of the sync sample it would decode from
decodes from the sync sample before that ([§8.2](../../mp4.md#82-tracks), [§8.3](../../mp4.md#83-chunks)). A track whose
sync sample table does not increase, or names a sample past the last, is refused.
