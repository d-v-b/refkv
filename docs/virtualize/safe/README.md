# Sentinel-2 SAFE

refkv turns a file into a Zarr hierarchy by writing a small archive of references
to the file's bytes, without copying them ([overview](../README.md)). This page is
about Sentinel-2 Level-1C and Level-2A products, as a `.SAFE` directory or a `.SAFE.zip`; the convention and the profile are
[spec/virtualize/safe.md](../../../spec/virtualize/safe.md).

## Supported and not supported

### In the browser

The browser code ([js/](../../../js/README.md)) reads Sentinel-2 SAFE (Level-1C and Level-2A, a `.SAFE.zip` file or a `.SAFE` directory listed with S3 ListObjectsV2):

- One group per resolution (`r10m`, `r20m`, `r60m`) and one array per band,
  one chunk per JPEG 2000 tile, with the zarr-conventions `proj` and
  `spatial` metadata; the radiometric offsets and quantification values on
  each band, and every file of the product kept on `refkv_source`.
