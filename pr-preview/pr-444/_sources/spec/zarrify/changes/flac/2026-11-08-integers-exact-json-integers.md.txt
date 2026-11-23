Changed 2026-11-08: a cue sheet's `lead_in` and offsets past 2^53 − 1 are JSON integers, exact, where
they were the strings of their digits ([§5.1](../../flac.md#51-the-root),
[conventions §6](../../../conventions.md#6-source-metadata-as-json)). The mirror of nearly every file
changes too (of every fixture): its view shows STREAMINFO's packed 64-bit field (past 2^53 − 1 at a
sample rate of 512 Hz or more), and a seek point's placeholder sample number (2^64 − 1), as JSON
integers ([conventions §8.7](../../../conventions.md#87-the-view)).
