"""Routines of a format's convention that a layout may share with refkv's own projection, each the
binding of the function that projection calls (the design of d-v-b/refkv#278, "The routines").

A format's routines are a module of this package, named for the format, that lists them in its
`__all__`; its docstring says what they make. The package exports every such module's names,
found by discovery when it is imported, so that a format adds or changes its routines in its own
module alone. A module whose name starts with `_` holds what the routines share, and exports
nothing."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from refkv._modules import submodules


def _discover(package: str, path: Iterable[str]) -> dict[str, Any]:
    """Every name the modules of `package` (at `path`) export, by name, but those of modules whose
    name starts with `_`. ImportError where two modules export one name."""
    found: dict[str, Any] = {}
    for module in submodules(package, path).values():
        for name in module.__all__:
            if name in found:
                raise ImportError(f"routine modules export {name} twice, the second time from {module.__name__}")
            found[name] = getattr(module, name)
    return found


_EXPORTED = _discover(__name__, __path__)
globals().update(_EXPORTED)
__all__: list[str] = []
__all__ += sorted(_EXPORTED)  # noqa: PLE0605 (the names discovery finds)

