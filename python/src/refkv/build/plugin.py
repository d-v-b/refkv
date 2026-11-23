"""`apply`: the output a plugin layout writes of parsed inputs (spec/layout.md
<https://github.com/d-v-b/refkv/blob/main/spec/layout.md>). A plugin is given the inputs, each as
`read` read it, and nothing refkv built of them: it writes its entries from their IRs, and from
refkv's own output only where it asks for it (`project`). refkv seals what it returns."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from refkv import ReferenceStore
from refkv.errors import Rejected
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


def _own(error: BaseException | None, inputs: Sequence[Parsed]) -> bool:
    """Whether `error` is one of refkv's own reads of an input (a source's bytes, the reads its
    built-in output makes), which the input recorded, and not of a plugin's code: `apply` raises
    it as it is, as `zarrify` without a plugin would. Told by identity, so that no code of the
    error's own runs."""
    return error is not None and any(error is p._own_error for p in inputs)  # noqa: SLF001


def apply(planned: Planned, inputs: Sequence[Parsed], *, info: Mapping[str, object] | None = None) -> ReferenceStore:
    """The store of what the plugin layout `planned` (`refkv.zarrify.layouts.plan(layout)`) writes
    of `inputs`: each input is checked against the plugin's `formats`, before anything is built of
    it; the plugin's `write(inputs, options)` runs; and `seal` vouches for what it returned,
    `info` the store's, with the plugin's name as its `layouts`.

    Raises `LayoutRejected` for an input of a profile or version the plugin does not read, or
    one the plugin rejects (`refkv.zarrify.Rejected`), and `LayoutFailed` for a plugin that
    raises anything else or an output `seal` refuses. An error of refkv's own reads of an input
    is neither: a read the built-in output makes that fails (where the plugin asked for that
    output), a source's bytes the plugin asked for (`Parsed.bytes`) that cannot be read, or a
    range that cannot be read to checksum it, raises what it raises without a
    plugin (`OSError`, or an error of `refkv.errors`). The inputs must be open where the plugin
    asks for the built-in output (`project`), or was read for checksums. The sources' URLs are
    not held to the reader policy here: `save_vzip` holds them, as for refkv's own output."""
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
        return seal(written, inputs, info={**(info or {}), "layouts": [name]}, check_sources=False)
    except Rejected as e:
        raise LayoutRejected(f"{name}: {_text(e)}") from e
    except SealError as e:
        cause = e.__cause__
        if cause is not None and _own(cause, inputs):
            raise cause from None  # a range refkv could not read to checksum it: the input's error
        raise LayoutFailed(f"{name}: {e}") from e
    except BaseException as e:  # any of a plugin's, made a failure
        if _own(e, inputs):
            raise
        raise LayoutFailed(f"{name}: {described(e)}") from e
