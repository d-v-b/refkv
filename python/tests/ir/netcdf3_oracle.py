"""What netCDF-C reads of a netCDF3 file, through netCDF4-python, against a zarrified hierarchy of it
(spec/zarrify/netcdf3.md <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md>): each
global attribute, and each variable's values, shape, data type, dimension names and attributes. Used
by test_netcdf3.py and js/test/netcdf3/verify.py."""

import base64
import math
import struct
from pathlib import Path
from typing import Any, cast

import netCDF4
import numpy as np
import zarr
from zarr.core.metadata import ArrayV3Metadata


def array(group: zarr.Group, name: str) -> zarr.Array:
    """The array `name` of `group`."""
    a = group[name]
    assert isinstance(a, zarr.Array), name
    return a


def attrs(node: zarr.Array | zarr.Group) -> dict[str, Any]:
    """A node's attributes, as JSON."""
    return cast("dict[str, Any]", node.attrs.asdict())


def own(node: zarr.Array | zarr.Group) -> dict[str, Any]:
    """A node's source metadata, `refkv_zarrified.netcdf3` (spec/zarrify/netcdf3.md §6
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#6-source-metadata>)."""
    return cast("dict[str, Any]", attrs(node).get("refkv_zarrified", {}).get("netcdf3", {}))


def json_of(v) -> object:
    """A netCDF4-python attribute value as the convention writes it (spec/zarrify/netcdf3.md §5
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#5-attributes>)."""
    if isinstance(v, str):
        return v
    a = np.asarray(v)
    if a.dtype.kind == "f":
        out = ["NaN" if math.isnan(x) else "Infinity" if x == math.inf else "-Infinity" if x == -math.inf else x
               for x in a.ravel().tolist()]
    else:
        out = a.ravel().tolist()
    return out[0] if a.ndim == 0 else out


def same(a: np.ndarray, b: np.ndarray) -> bool:
    return np.array_equal(a, b, equal_nan=a.dtype.kind == "f")


def problems(path: Path, root: zarr.Group, ds: netCDF4.Dataset) -> list[str]:
    """Where the zarrified file differs from what netCDF-C reads."""
    out = []
    for k in ds.ncattrs():
        got = attrs(root).get(k)
        # text that is not UTF-8 is kept as ISO 8859-1; netCDF4-python replaces what it cannot decode
        if isinstance(got, dict) and "latin1" in got:
            got = str(got["latin1"]).encode("latin1").decode("utf-8", "replace")
        if got != json_of(ds.getncattr(k)):
            out.append(f"{path.name}: global {k}: {got!r} != {json_of(ds.getncattr(k))!r}")
    for name, v in ds.variables.items():
        a = array(root, name)
        want = np.asarray(v[...])
        got = np.asarray(a[...])
        if a.shape != v.shape or got.dtype != want.dtype.newbyteorder("=") or not same(got, want):
            out.append(f"{path.name}: {name}: {a.shape} {got.dtype} != {v.shape} {want.dtype}")
        assert isinstance(a.metadata, ArrayV3Metadata)
        if a.metadata.dimension_names != v.dimensions:
            out.append(f"{path.name}: {name}: dimensions {a.metadata.dimension_names} != {v.dimensions}")
        not_presented = own(a).get("unpresented_attributes", {})
        for k in v.ncattrs():
            if k == "_FillValue" and not_presented.get(k) == "fill_value":
                # one xarray's Zarr backend does not decode (spec/zarrify/netcdf3.md §5
                # <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/netcdf3.md#5-attributes>): its
                # value is the fill value where it is of the variable's type
                if np.asarray(v.getncattr(k)).dtype == want.dtype and np.size(v.getncattr(k)) == 1:
                    fill = a.metadata.fill_value
                    if np.asarray(fill, want.dtype).tobytes() != np.asarray(v.getncattr(k), want.dtype).tobytes():
                        out.append(f"{path.name}: {name}: the fill value {fill!r} != {v.getncattr(k)!r}")
                continue
            want_attr = json_of(v.getncattr(k))
            if k == "_FillValue" and want.dtype.kind == "f":
                want_attr = base64.b64encode(struct.pack("<d", float(v.getncattr(k)))).decode()
            elif k == "_FillValue":
                want_attr = int(v.getncattr(k))
            if a.attrs.get(k) != want_attr:
                out.append(f"{path.name}: {name}.{k}: {a.attrs.get(k)!r} != {want_attr!r}")
    return out
