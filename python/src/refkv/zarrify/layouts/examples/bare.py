"""The example layout `refkv.example.bare` (spec/layout.md
<https://github.com/d-v-b/refkv/blob/main/spec/layout.md>), the smallest plugin: refkv's own
hierarchy for every file profile, with none of refkv's declarations and without the IR mirror
`refkv_source/`. With the option `{"dataset": false}` it leaves out the dataset layouts too
(spec/conventions.md §9
<https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#9-dataset-layouts>), keeping the
image layout alone; a dataset group it keeps no longer says it is one, as its own declaration
said. Its documents are the base's, written with `dump`, so it runs without the `layouts` extra.

    python -m refkv.zarrify image.ome.tif out.vzip --layout refkv.example.bare

Every array is the base's array, document and chunks, with the node's attributes as
`Base.attributes` gives them, so `check` finds no problem of any code on its output: the
references are where `copy` puts them (no `copy`), no node keeps refkv's members (no `refkv`),
and every node's parent is a group the base wrote (no `parent`, no `orphan`).
"""

from collections.abc import Mapping

from refkv.zarrify.layouts import Base, Entries


class Bare:
    name = "refkv.example.bare"
    formats = "*"

    def options(self, given: Mapping[str, object]) -> dict[str, object]:
        """`{"dataset": true}` by default: whether to keep the dataset layouts."""
        unknown = sorted(set(given) - {"dataset"})
        if unknown:
            raise ValueError(f"unknown options {unknown}: refkv.example.bare takes dataset (true or false)")
        dataset = given.get("dataset", True)
        if type(dataset) is not bool:
            raise ValueError(f"dataset is true or false, not {dataset!r}")
        return {"dataset": dataset}

    def write(self, base: Base, options: Mapping[str, object]) -> Entries:
        """The base's entries without refkv's declarations, but those of the left-out nodes."""
        return base.entries(declarations=False, mirror=False, datasets=bool(options["dataset"]))
