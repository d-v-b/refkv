"""The layout `refkv.virtualizarr` (spec/layout.md
<https://github.com/d-v-b/refkv/blob/main/spec/layout.md>): a file presented as VirtualiZarr's own
parsers present it, so that code written against VirtualiZarr's parser of its format finds the
arrays, dimension names and attributes it expects. Its chunk references, chunk grids and codecs are
those of refkv's own output, and it writes none of refkv's declarations and no `refkv_source`
mirror. Each input's are made from its parser's description (and, where its module says so, the
IR) with its convention's routines (`refkv.build.routines`), with no output of refkv's: the layout
is that output's peer for every format it presents.
It writes its documents with `dump`, so it runs without the `layouts` extra.

    python -m refkv.zarrify data.nc out.vzip --layout refkv.virtualizarr

The reference is VirtualiZarr 2.8.0, read profile by profile. Each profile the layout presents is a
module of this package named for the profile, whose `write(parsed, token)` presents an input of
that profile, and whose docstring says how, after which of VirtualiZarr's parsers, and where that
parser's presentation is a defect rather than a contract, so that the layout keeps refkv's. The
layout finds the modules by discovery (`formats` is the profiles it finds), so a profile is added or
changed in its own module alone; a module whose name starts with `_` holds what the others share.

`check` finds no problem on its output but `json` at a document that holds a `NaN` or `Infinity`
token (only where a source attribute is not finite and `nonfinite` is `"token"`: zarr-python
writes those tokens, and VirtualiZarr's metadata is written by zarr-python). It copies nothing of
refkv's own output, so it is checked with no bases; given that output as a base, `check` would
report `copy` where a profile's module says so.
"""

from collections.abc import Callable, Mapping, Sequence
from typing import ClassVar

from refkv._modules import submodules
from refkv.build import Parsed, Written

# each profile's writer, by the profile its module is named for
WRITERS: Mapping[str, Callable[[Parsed, bool], Written]] = {
    profile: module.write for profile, module in submodules(__name__, __path__).items()}


class VirtualiZarr:
    name = "refkv.virtualizarr"
    formats: ClassVar[Mapping[str, frozenset[int]]] = {profile: frozenset({0}) for profile in WRITERS}


    def options(self, given: Mapping[str, object]) -> dict[str, object]:
        """`{"nonfinite": "token"}` by default: whether a NaN or infinite attribute is a bare JSON
        token, as zarr-python writes it, or the text `"NaN"`, `"Infinity"` or `"-Infinity"`
        (`"text"`), which strict JSON readers parse."""
        unknown = sorted(set(given) - {"nonfinite"})
        if unknown:
            raise ValueError(f"unknown options {unknown}: refkv.virtualizarr takes nonfinite (token or text)")
        nonfinite = given.get("nonfinite", "token")
        if nonfinite not in ("token", "text"):
            raise ValueError(f"nonfinite is token or text, not {nonfinite!r}")
        return {"nonfinite": nonfinite}

    def write(self, inputs: Sequence[Parsed], options: Mapping[str, object]) -> Written:
        """The input as VirtualiZarr's parser of its format presents it, by its profile's module:
        made from the parser's description with the convention's routines, none from refkv's own
        output."""
        [parsed] = inputs
        writer = WRITERS.get(parsed.profile)
        if writer is None:
            raise ValueError(f"refkv.virtualizarr presents no {parsed.profile} input")
        return writer(parsed, options["nonfinite"] == "token")
