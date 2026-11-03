"""refkv-phkv's version, from git tags: the same one as refkv's.

It is python/tools/version.py (see its docstring for the scheme and the commands), pointed at
this distribution's bindings crate, rust/refkv-phkv-python, whose committed version is the
placeholder 0.0.0. Both distributions take their version from the same `v*` tags, so a release
is one number for both.

    version.py show | pep440 [V] | write [TAG] | run -- CMD...
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
_REFKV = HERE.parent / "python" / "tools" / "version.py"


def _load():
    spec = importlib.util.spec_from_file_location("refkv_version", _REFKV)
    if spec is None or spec.loader is None:
        raise SystemExit(f"version.py: cannot load {_REFKV}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses and typing look a module up by name
    spec.loader.exec_module(module)
    crate_dir = HERE.parent / "rust" / "refkv-phkv-python"
    module.CRATE_DIR = crate_dir
    module.CARGO_TOML = crate_dir / "Cargo.toml"
    module.CARGO_LOCK = crate_dir / "Cargo.lock"
    module.CRATE = "refkv-phkv-python"
    return module


if __name__ == "__main__":
    sys.exit(_load().main(sys.argv[1:]))
