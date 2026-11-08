Changed 2026-11-08: a duration or an edit's media time past 2^53 − 1 in magnitude is a JSON integer, exact,
where it was the string of its digits ([§5.1](../../mp4.md#51-the-root),
[§5.2](../../mp4.md#52-the-tracks)), and so is any such integer in the mirror's view (a version-1
box's duration of 2^64 − 1, unknown, among them;
[conventions §8.7](../../../conventions.md#87-the-view)). The output of a file with such a value
changes; no fixture has one.
