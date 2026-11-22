Changed 2026-11-21: the profile's parser returns a description of the file, each object as the
file gives it, in place of facts that held the hierarchy's nodes; the projection decides which
objects are nodes, the arrays' Zarr data types, codecs, fill values and dimension names, and the
attributes within their budget, from the description
([§8.7](../../hdf5.md#87-the-description)). Every dataset's storage is now described in the IR
mirror whatever the room, presented or not ([§7.3](../../hdf5.md#73-the-ir-mirror)): an output
changes only for a file past its room or with a dataset of an implicit chunk index that it does not
present, whose chunks the mirror now describes one by one; no fixture's output changes.
