"""Narrowing of the nodes zarr opens (refkv._narrow's, for the zarr extra): each helper gives its
node as the type it names, or raises TypeError naming where the node was found."""

from __future__ import annotations

from typing import Any

import zarr


def array(v: object, where: str = "") -> zarr.Array[Any]:
    """`v` as a zarr array (a node of a group: `group["path"]`)."""
    if not isinstance(v, zarr.Array):
        raise TypeError(f"{where or 'node'}: expected an array, got {type(v).__name__}")
    return v


def group(v: object, where: str = "") -> zarr.Group:
    """`v` as a zarr group."""
    if not isinstance(v, zarr.Group):
        raise TypeError(f"{where or 'node'}: expected a group, got {type(v).__name__}")
    return v
