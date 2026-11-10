Changed 2026-10-29: the HDF5 profile has the signature that was the IMS profile's, at its
precedence, 70, and its reading chooses the IMS profile for an Imaris file
([§1.2](../../../zarrify.md#12-input), [hdf5.md §8.1](../../hdf5.md#81-choosing-the-profile)); an Imaris file's
output does not change, and an HDF5 file that is not one is the HDF5 profile's, where the IMS
profile rejected it.
