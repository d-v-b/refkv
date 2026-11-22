Changed 2026-11-08: a header field's 64-bit integer past 2^53 − 1 in magnitude is a JSON integer, exact,
where it was the string of its digits ([§5.1](../../nifti.md#51-the-root)). The output of a NIfTI-2
file with such a field changes.
