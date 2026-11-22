Changed 2026-11-04: `byte skip` with the gzip encoding is read, as the bytes a `refkv.skip` codec
passes over in the decompressed data ([§4.1](../../nrrd.md#41-the-array)), where the profile rejected it; −1
stays rejected ([§8.2](../../nrrd.md#82-the-fields)).
