Changed 2026-11-21, with no change to any output of the fixtures: the profile's parser returns,
beside the IR, a description of the file, its library version, its Vgroups and Vdatas (an
attribute's value decoded) and the SD interface's datasets with their number types, dimension
records and storage, a chunked dataset's chunks listed ([§9.6](../../hdf4.md#96-the-description)).
It reads every Vdata's records' storage and every chunked dataset's chunk table and chunks, so a
broken linked element, compressed element or chunk table rejects the file even where the object it
belongs to is not presented ([§9.3](../../hdf4.md#93-checks)).
