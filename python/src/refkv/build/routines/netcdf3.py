"""The netCDF3 convention's routine: `netcdf3_hierarchy`, the convention's hierarchy of a netCDF3
file (`Netcdf3Hierarchy`)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from refkv import _core
from refkv.zarrify.common import Rejected

from ._hierarchy import _Hierarchy

if TYPE_CHECKING:
    from refkv.build.parsed import Parsed

__all__ = ["Netcdf3Hierarchy", "netcdf3_hierarchy"]


class Netcdf3Hierarchy(_Hierarchy):
    """The netCDF3 convention's hierarchy of a netCDF3 file (spec/zarrify/netcdf3.md §2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#2-the-hierarchy>), as refkv's
    own output presents it but for its declarations and its IR mirror: the root, then an array per
    variable the convention presents, in the header's order, each node's attributes (§5
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#5-attributes>) and source
    metadata (§6 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#6-source-metadata>),
    and each array's document and chunks (§3
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#3-variables>), as values of
    the input. `netcdf3_hierarchy` makes it."""

    def __init__(self, parsed: Parsed, netcdf3: _core.Netcdf3) -> None:
        try:
            given = netcdf3.hierarchy()
        except _core.Rejected as e:
            raise Rejected(str(e)) from None
        super().__init__(parsed, given)


def netcdf3_hierarchy(parsed: Parsed) -> Netcdf3Hierarchy:
    """The netCDF3 convention's hierarchy of a netCDF3 file (spec/zarrify/netcdf3.md §2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#2-the-hierarchy>): the
    convention's decisions, which variables are nodes, the attributes presented, the arrays' data
    types, fill values, codecs, dimension names and chunks, made from the parser's description
    (§7.7 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#77-the-description>) by
    the function refkv's own projection calls. It reads nothing, and asks for no output of refkv's.
    ValueError for an input that is not a netCDF3 file."""
    netcdf3 = parsed._view_as(_core.Netcdf3)  # noqa: SLF001
    if netcdf3 is None:
        raise ValueError(f"a netCDF3 routine reads a netCDF3 file, not a {parsed.profile} input")
    return Netcdf3Hierarchy(parsed, netcdf3)
