Changed 2026-11-07: a band of padded rows that lie more than 65536 bytes apart holds at most 1024
rows ([§3.4](../../lif.md#34-rows-and-bands)), so that its chunk meets
[zarrify §1.2](../../../zarrify.md#12-input)'s read rule; a file whose bands held more, such as one of
many line-interleaved channels over more than 1024 rows, is cut in smaller bands, where it was
rejected.
