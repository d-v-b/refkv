Changed 2026-11-19, with no change to any fixture's output: canonical form names every gap `gaps/`
with its offset as name index, whatever name the producer gave it, so refkv's `gaps/<offset>/<j>`,
the name of a gap repeated inside a run's members, becomes the gap's own `gaps/<offset>`
([§8.1](../../../conventions.md#81-elements),
[§8.8](../../../conventions.md#88-canonical-form) step 1). A run holds no derived element, no
alias and no element in a derived space ([§8.1](../../../conventions.md#81-elements)). A reader
rejects a table whose runs break those rules, whose runs have more than `N` members besides each
run's member 0, or whose runs' leaves, counted member by member, are more than `N`, with the
messages [§8.3](../../../conventions.md#83-column-runs) gives.
