"""The profile registry, spec/zarrify/profiles/<name>.json (one file per profile), as the Rust
core holds it (its copies, rust/refkv-ir/profiles/, which `refkv._core.registry` gives): its
entries, by name."""

import json
from collections.abc import Mapping

from refkv import _core
from refkv._narrow import JSON, mapping, sequence

REGISTRY: tuple[Mapping[str, JSON], ...] = tuple(
    mapping(e, "registry entry") for e in sequence(json.loads(_core.registry()), "registry"))
