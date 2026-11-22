Changed 2026-11-22: the IR grafts the MP4 file of every channel whose bytes read as one, zarrified
or not, so a channel refkv's convention doesn't zarrify for its track, its chroma or the padding
now has its MP4 file's elements in the mirror ([§5.3](../../h5j.md#53-elements)); and the parser
returns a description of the file, from which the projection decides the channels
([§8.6](../../h5j.md#86-the-description)).
