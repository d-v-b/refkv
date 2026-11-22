Changed 2026-11-20, with no change to any output but the IR mirror of an archive with a deflated
member: a deflated `.npz` member whose preamble and header were read has them, and its data, as
elements of what its deflate stream inflates to, the space of its `data` element, whose form gives
where the header ends (`prefix`) and, for a presented array, the size of what the stream inflates to
([§5.3](../../npy.md#53-elements)); the view shows its preamble and header. The description's
`stored` is no longer null for such a member, and its `deflate` holds only the central directory's
CRC-32 and uncompressed size ([§8.6](../../npy.md#86-the-description)).
