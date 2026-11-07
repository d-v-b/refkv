Changed 2026-11-07: every output's sources are numbered by one rule, first use over the reference
entries in the order of their keys ([§1.2](../../../zarrify.md#12-input)), its `url` sources first:
a file's data sources, numbered in the order the profile listed its references, and the files of a
file read as a store ([§1.8](../../../zarrify.md#18-files-read-as-stores),
[§1.9](../../../zarrify.md#19-sources-beside-the-input)), in the order its document gave, are
renumbered where that order differs. A reference entry but a chunk of a sharded array reads at
most 1024 ranges of `url` sources, merged as refkv's readers merge them, or the input is rejected;
a payload over 65519 bytes only with its checksums counts each checksum at its largest, 6 bytes, so
an output fails, and is not written, whatever the bytes' checksums; and a repeat of the references'
budget is a reference to the source ranges of an earlier one, whatever elements they are cut from.
