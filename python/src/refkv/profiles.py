"""The profile registry, spec/zarrify/profiles/<name>.json (one file per profile), as the Rust
core holds it (its copies, rust/refkv-ir/profiles/, which `refkv._core.registry` gives): its
entries, by name."""

import json
from collections.abc import Mapping

from refkv import _core
from refkv._narrow import JSON, mapping, sequence, string

REGISTRY: tuple[Mapping[str, JSON], ...] = tuple(
    mapping(e, "registry entry") for e in sequence(json.loads(_core.registry()), "registry"))


def _run_beside(e: Mapping[str, JSON]) -> Mapping[str, JSON] | None:
    """An entry's store run, if it reads a file with the files beside it."""
    run = e["store_run"]
    return None if run is None or mapping(run, "store run")["form"] == "listed" else mapping(run, "store run")


_BESIDE = {string(e["name"], "name"): (e, run) for e in REGISTRY if (run := _run_beside(e)) is not None}
# The file profiles whose input is read with the files beside it, as a store run that probes for
# them: (the format's title, the named file) as a rejection names them.
BESIDE: dict[str, tuple[str, str]] = {
    name: (string(e["title"], "title"), string(run["named"], "named")) for name, (e, run) in _BESIDE.items()}
# Those of them that read a file with the files beside it only when it names some, or for some of
# their files (spec/zarrify.md §1.9
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#19-sources-beside-the-input>):
# a file run finds out, and starts over (`ir.NamesSiblingsError`).
ON_DEMAND = frozenset(name for name, (_, run) in _BESIDE.items() if run["form"] == "on_demand")
# Those whose writers name files in another case than they have (the same section's **Case**).
CASE = frozenset(name for name, (_, run) in _BESIDE.items() if run.get("case") is True)
