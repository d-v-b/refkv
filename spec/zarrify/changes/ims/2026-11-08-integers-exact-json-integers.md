Changed 2026-11-08, with no change to the convention's own rules: the mirror's view shows an integer past
2^53 − 1 as a JSON integer, exact, where it was `{"$vz": "int", "v": digits}`
([conventions §8.7](../../../conventions.md#87-the-view)), as the example of
[§5.1](../../ims.md#51-the-ir-mirror) now shows. Every file's mirror changes (an HDF5 superblock's
undefined address is such an integer).
