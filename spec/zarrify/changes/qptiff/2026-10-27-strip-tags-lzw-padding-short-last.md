Changed 2026-10-27, with no change to the output of the inputs accepted before but the unusual
ones below: the strip tags, LZW and the padding of a short last strip are now the TIFF
convention's ([tiff.md §4.3](../../tiff.md#43-data-types-and-codecs),
[tiff.md §4.5](../../tiff.md#45-strips)), which [§2](../../qptiff.md#2-the-source), [§4.1](../../qptiff.md#41-data-types-and-codecs)
and [§4.2](../../qptiff.md#42-tiles-and-strips) cite; a level whose short last strip cannot be padded is still
left out here; a strip padded with more than 2^22 zero bytes now rejects the input
([§4.2](../../qptiff.md#42-tiles-and-strips)), and a short last LZW strip is read up to `3 × rows × B + 64`
bytes, as [tiff.md §7.1](../../tiff.md#71-reading) says, where the whole strip was read (a stream that
spends more than a Clear code and a literal on each byte it decodes no longer reaches its size);
the zero padding is a prefix of the output's one zero data source, the same bytes and range as
before when every padded strip lacks the same count.
