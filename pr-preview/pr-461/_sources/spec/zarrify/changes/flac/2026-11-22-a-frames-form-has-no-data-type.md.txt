Changed 2026-11-22: a frame's form gives no data type in its geometry, only its block size and
channels ([§5.3](../../flac.md#53-elements)): its codec's `bits_per_sample` gives the samples'
width, and refkv's plan decides the output's data type, as it did. The IR mirror of every file
changes; no other output does.
