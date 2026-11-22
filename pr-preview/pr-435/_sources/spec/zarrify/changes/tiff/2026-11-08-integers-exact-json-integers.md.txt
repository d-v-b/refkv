Changed 2026-11-08, with no change to the convention's own rules: the mirror's view shows a tag's
integer past 2^53 − 1 (a LONG8 or IFD8 value) as a JSON integer, exact, where it was
`{"$vz": "int", "v": digits}` ([§5.1](../../tiff.md#51-elements),
[conventions §8.7](../../../conventions.md#87-the-view)). The mirror of a file with such a value
changes.
