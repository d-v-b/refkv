Changed 2026-11-08: a cue sheet's `lead_in` and offsets past 2^53 − 1 are JSON integers, exact, where
they were the strings of their digits ([§5.1](../../flac.md#51-the-root),
[conventions §6](../../../conventions.md#6-source-metadata-as-json)), and so are such integers in
the mirror's view. The output of a file with such an integer changes.
