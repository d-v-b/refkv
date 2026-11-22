Changed 2026-11-01: the root's `index` names the index whose offsets the run used, where it
named the first index read even when another one's offsets were used, and the run tries each
index in turn ([§5.1](../../grib.md#51-the-root), [§8.2](../../grib.md#82-finding-the-messages)). With no other change to
any output, the text states what refkv already did: the names' tables are data files of this
convention ([§3.2](../../grib.md#32-names)); the Earth's shapes, the Lambert projection and the rejections are
written out ([§2.4](../../grib.md#24-grids), [§4](../../grib.md#4-georeferencing), [§8.4](../../grib.md#84-checks-and-rejection)); every
index read is held ([§5.2](../../grib.md#52-the-ir-mirror)); and the file is read as a store as
[spec/zarrify.md §1.8](../../../zarrify.md#18-files-read-as-stores) says ([§8](../../grib.md#8-grib-profile)).
