Changed 2026-11-21, with no change to any output: the profile's parser returns a description of the
store, each node's documents as parsed, in place of facts that held the hierarchy's decisions; the
projection decides which arrays are presented, each array's `zarr.json` and the source metadata
from the description, and rejects the input for a document that fails the convention there
([§7.3](../../zarr2.md#73-the-description)). The summary is now [§7.4](../../zarr2.md#74-summary).
