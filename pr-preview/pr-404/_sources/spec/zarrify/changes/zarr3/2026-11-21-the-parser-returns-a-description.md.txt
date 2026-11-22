Changed 2026-11-21, with no change to any output: the profile's parser returns a description of the
store, listed or zipped, each candidate's `zarr.json` as parsed, in place of facts that held the
hierarchy's decisions; the projection classifies the candidates, leaves out a zipped array with a
compressed chunk, and decides what the hierarchy writes of each node from the description, and
rejects the input where the convention says ([§7.6](../../zarr3.md#76-the-description)). The
summary is now [§7.7](../../zarr3.md#77-summary).
