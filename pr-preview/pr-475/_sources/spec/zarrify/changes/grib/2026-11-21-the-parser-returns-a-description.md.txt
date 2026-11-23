Changed 2026-11-21, with no change to any output of this convention: the store run's parser returns a
description of the file ([§8.7](../../grib.md#87-the-description)), each field with what of it the
parser does not interpret, in place of facts that held which fields are presented and the root's
source metadata; the projection decides those, and rejects a file of which no field is presented,
as refkv's plan of the file.
