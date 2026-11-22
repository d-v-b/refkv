Changed 2026-11-05: a zip file whose first entry is not under a `.SAFE` directory is the Zarr v3
profile's zipped form, tested before this profile's zip form
([§8.1](../../safe.md#81-choosing-the-profile)), so such a file is rejected by that profile, as a
store whose root has no `zarr.json`, rather than by [§8.4](../../safe.md#84-the-zip-form).
