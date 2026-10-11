"""`vzip.zarr`: a vzip store read through zarr's store interface gives what the vzip
store gives (the mutations it rejects are in test_refstore.py)."""

import subprocess
import sys

import pytest
from zarr.abc.store import OffsetByteRequest, RangeByteRequest, SuffixByteRequest
from zarr.core.buffer import default_buffer_prototype
from zarr.core.sync import sync

import vzip
import vzip.zarr
from vzip import OffsetRequest, RangeRequest, SuffixRequest

from test_store_operations import VALUES, _stores

PROTO = default_buffer_prototype()


def test_zarr_reads_what_the_store_reads(tmp_path):
    """get (each byte request), get_partial_values, exists, list, list_prefix and
    list_dir, through zarr, for each store and for `vzip.zarr.open`."""
    requests = {None: None, RangeByteRequest(1, 3): RangeRequest(1, 3),
                OffsetByteRequest(2): OffsetRequest(2), SuffixByteRequest(2): SuffixRequest(2)}
    stores = {name: vzip.zarr.store(s) for name, s in _stores(tmp_path).items()}
    stores["vzip.zarr.open"] = vzip.zarr.open(tmp_path / "None.vzip")

    async def read(zs):
        got = {}
        for key in [*VALUES, "nope", "__vz__/shared"]:
            for zr, r in requests.items():
                b = await zs.get(key, PROTO, zr)
                got[key, r] = None if b is None else b.to_bytes()
        partial = await zs.get_partial_values(PROTO, [("a", RangeByteRequest(0, 2)), ("nope", None)])
        return (got, [None if b is None else b.to_bytes() for b in partial],
                {k: await zs.exists(k) for k in ["a", "b", "nope", "__vz__/shared"]},
                [k async for k in zs.list()], [k async for k in zs.list_prefix("d/")],
                [k async for k in zs.list_dir("d")])

    for name, zs in stores.items():
        assert zs.read_only and not zs.supports_writes and zs.supports_listing, name
        s = zs.reader
        want = {(k, r): s.get(k, r) for k in [*VALUES, "nope", "__vz__/shared"]
                for r in requests.values()}
        assert sync(read(zs)) == (
            want, [b"pl", None], {"a": True, "b": True, "nope": False, "__vz__/shared": False},
            ["a", "b", "c", "d/e", "d/f/g"], ["d/e", "d/f/g"], ["e", "f"]), name


def test_store_refuses_another_type():
    with pytest.raises(TypeError, match="wraps a vzip store"):
        vzip.zarr.store({"a": b""})


def test_without_zarr_the_adapter_names_its_extra():
    code = ("import sys; sys.modules['zarr'] = None\n"
            "import vzip\n"
            "try:\n"
            "    vzip.zarr\n"
            "except ImportError as e:\n"
            "    print(e)\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert "vzip.zarr needs 'zarr', which vzip's 'zarr' extra installs" in out.stdout
    assert "vzip[zarr]" in out.stdout
