"""The profile registry, spec/virtualize/profiles/<name>.json (one file per profile), as the Rust
core holds it (its copies, rust/refkv-ir/profiles/, which `refkv._core.registry` gives): its
entries, by name."""

import json

from refkv import _core

REGISTRY: tuple[dict, ...] = tuple(json.loads(_core.registry()))
