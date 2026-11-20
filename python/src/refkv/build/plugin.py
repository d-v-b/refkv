"""`apply`: the output a plugin layout writes of parsed inputs (spec/layout.md
<https://github.com/d-v-b/refkv/blob/main/spec/layout.md>). A plugin is given the inputs, each as
`read` read it, and nothing refkv built of them: it writes its entries from their IRs, and from
refkv's own output only where it asks for it (`project`). refkv seals what it returns."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from refkv import ReferenceStore
from refkv.zarrify.common import Rejected
from refkv.zarrify.layouts import (
    BASE_PROFILES,
    LayoutFailed,
    LayoutRejected,
    Planned,
    _text,
    described,
)

from .seal import SealError, seal

if TYPE_CHECKING:
    from .parsed import Parsed

__all__ = ["apply"]


def apply(planned: Planned, inputs: Sequence[Parsed], *, info: Mapping[str, object] | None = None) -> ReferenceStore:
    """The store of what the plugin layout `planned` (`refkv.zarrify.layouts.plan(layout)`) writes
    of `inputs`: each input is checked against the plugin's `formats`, before anything is built of
    it; the plugin's `write(inputs, options)` runs; and `seal` vouches for what it returned,
    `info` the store's, with the plugin's name as its `layouts`.

    Raises `LayoutRejected` for an input of a profile or version the plugin does not read, or
    one the plugin rejects (`refkv.zarrify.Rejected`), and `LayoutFailed` for a plugin that
    raises anything else or an output `seal` refuses. The inputs must be open where the plugin
    asks for the built-in output (`project`), or was read for checksums."""
    name = planned.name
    for parsed in inputs:
        if not planned.reads(parsed.profile, parsed.version):
            reads = "every profile" if isinstance(planned.formats, frozenset) else ", ".join(
                f"{k} v{', v'.join(str(v) for v in sorted(vs))}" for k, vs in sorted(planned.formats.items())
                if k in BASE_PROFILES and vs) or "no version of any"
            raise LayoutRejected(f"{name} does not read the {parsed.profile} profile's convention at version "
                                 f"{parsed.version} (it reads {reads})")
    try:  # seal reads what write returned, a plugin's mapping or generators among it: its code too
        written = planned.plugin.write(list(inputs), planned.fresh_options())
        return seal(written, inputs, info={**(info or {}), "layouts": [name]})
    except Rejected as e:
        raise LayoutRejected(f"{name}: {_text(e)}") from e
    except SealError as e:
        raise LayoutFailed(f"{name}: {e}") from e
    except BaseException as e:  # any of a plugin's, made a failure
        raise LayoutFailed(f"{name}: {described(e)}") from e
