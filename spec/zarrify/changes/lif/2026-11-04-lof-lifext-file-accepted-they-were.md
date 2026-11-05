Changed 2026-11-04: a LOF ([§2.5](../../lif.md#25-leica-object-files)) and a LIFEXT file
([§2.6](../../lif.md#26-lifext-files)) are accepted, where they were rejected; a LIF file's output does not
change. The root's source metadata has a member `file` for them, an image's a member `parent`, and
an image of a LOF need not have a `memory`.
