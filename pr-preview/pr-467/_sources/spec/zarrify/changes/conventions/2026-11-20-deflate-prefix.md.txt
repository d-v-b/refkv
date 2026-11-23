Changed 2026-11-20: a derived element of transform `deflate` may give a `prefix` in its form, the
bytes at the start of what its stream inflates to that hold its values, and its space may then hold
shown values that end at or before it: a validator inflates the stream's first `prefix` bytes to
rebuild them ([§8.5](../../../conventions.md#85-forms-and-recipes),
[§8.8](../../../conventions.md#88-canonical-form)). A `deflate` element without one holds nothing, as
before. A `.npz` archive's deflated members give one
([npy.md §5.3](../../npy.md#53-elements)).
