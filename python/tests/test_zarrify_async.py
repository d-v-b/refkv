"""`refkv.zarrify.zarrify_async` and `zarrify_store_async`: the coroutines `zarrify` and
`zarrify_store` wait for, which zarrify an input without blocking the event loop."""

import asyncio
import sys
import threading
import time
from pathlib import Path

import pytest

from refkv import _sync
from refkv.ir import planner
from refkv.policy import Policy
from refkv.zarrify import NOT_SUPPORTED, Rejected, zarrify, zarrify_async, zarrify_store, zarrify_store_async

sys.path.insert(0, str(Path(__file__).parents[2] / "conformance" / "zarrify"))
from proxy import Proxy

FIXTURES = Path(__file__).parents[2] / "fixtures"
LOOPBACK = Policy(allow_private_hosts=True)  # the proxy serves the fixtures on 127.0.0.1


@pytest.fixture(scope="module")
def proxy(tmp_path_factory):
    return Proxy(FIXTURES, tmp_path_factory.mktemp("cache"))


# (fixture, how it is read): files through the Rust core's read planner (a TIFF, NIfTI, ND2, an
# MP4), a VSI slide (a file whose tile files make it a store run), and stores (Zarr v2, OME-Zarr
# 0.4, N5), each from a local path and over HTTP
INPUTS = [
    ("tiff/jpeg_gray.tif", "file"),
    ("nifti/nifti_block_x_split.nii", "file"),
    ("nd2/nd2_tz_uint16.nd2", "file"),
    ("mp4/mp4_h264_aac.mp4", "file"),
    ("vsi/vsi_rgb_jpeg.vsi", "file"),
    ("tiff/jpeg_gray.tif", "http"),
    ("nd2/nd2_tz_uint16.nd2", "http"),
    ("vsi/vsi_raw_tzc.vsi", "http"),
    ("zarr2/zarr2_hierarchy", "dir"),
    ("ome-zarr/ome_zarr_image_2d", "http store"),
    ("n5/n5_hierarchy", "http store"),
]


def _saved(store, path, policy):
    store.save(path, policy=policy)
    return path.read_bytes()


@pytest.mark.parametrize(("name", "how"), INPUTS, ids=str)
@pytest.mark.parametrize("checksums", [False, True])
def test_zarrify_async_gives_zarrify_s_store(proxy, tmp_path, name, how, checksums):
    """Awaited, `zarrify_async` (and, for a store, `zarrify_store_async`) gives the store
    `zarrify` gives: the same info, and the same archive saved, byte for byte."""
    url = None
    policy = LOOPBACK if how in ("http", "http store") else None
    location = {"file": str(FIXTURES / name), "dir": str(FIXTURES / name),
                "http": proxy.local(name), "http store": proxy.local_store(name)}[how]
    if how == "dir":
        url = "https://data.test/store/"
    expected = zarrify(location, url, checksums=checksums, policy=policy)
    got = asyncio.run(zarrify_async(location, url, checksums=checksums, policy=policy))
    assert got.info == expected.info
    assert _saved(got, tmp_path / "async.vzip", policy) == _saved(expected, tmp_path / "sync.vzip", policy)
    if how in ("dir", "http store"):
        stored = asyncio.run(zarrify_store_async(location, url, checksums=checksums, policy=policy))
        assert stored.info == zarrify_store(location, url, checksums=checksums, policy=policy).info
        assert _saved(stored, tmp_path / "store.vzip", policy) == _saved(expected, tmp_path / "sync.vzip", policy)


def test_zarrify_async_calls_run_concurrently_on_one_loop(monkeypatch):
    """Several `zarrify_async` calls gathered on one loop read at the same time: a read of one
    input is in flight while another's is, and each gives the store `zarrify` gives."""
    names = ["nd2/nd2_tz_uint16.nd2", "tiff/jpeg_gray.tif", "nifti/nifti_block_x_split.nii",
             "mp4/mp4_h264_aac.mp4"]
    reads: list[tuple[int, float, float]] = []  # (transport, start, end) of each read
    lock = threading.Lock()
    fetch = planner.FileTransport.fetch

    def slow(self, spans):
        start = time.perf_counter()
        time.sleep(0.05)  # a remote read's latency
        got = fetch(self, spans)
        with lock:
            reads.append((id(self), start, time.perf_counter()))
        return got

    monkeypatch.setattr(planner.FileTransport, "fetch", slow)

    async def main():
        return await asyncio.gather(*(zarrify_async(str(FIXTURES / n)) for n in names))

    stores = asyncio.run(main())
    assert len({r[0] for r in reads}) == len(names)  # every input read
    assert any(a[0] != b[0] and a[1] < b[2] and b[1] < a[2] for a in reads for b in reads)
    for n, store in zip(names, stores, strict=True):
        assert store.info == zarrify(str(FIXTURES / n)).info


def test_zarrify_async_rejects_what_zarrify_rejects(tmp_path):
    """An input no profile accepts raises the `Rejected` that `zarrify` raises."""
    path = tmp_path / "noise.bin"
    path.write_bytes(bytes(range(256)) * 4)
    with pytest.raises(Rejected) as blocking:
        zarrify(str(path))
    with pytest.raises(Rejected) as awaited:
        asyncio.run(zarrify_async(str(path)))
    assert str(awaited.value) == str(blocking.value) == NOT_SUPPORTED


def test_zarrify_on_refkv_s_own_loop_raises():
    """The blocking `zarrify`, called from a coroutine on refkv's own event loop, would block
    the loop it waits on: it raises `refkv._sync.run`'s RuntimeError instead."""

    async def blocking_inside():
        return zarrify(str(FIXTURES / "nifti" / "nifti_block_x_split.nii"))

    with pytest.raises(RuntimeError, match="refkv's own event loop"):
        _sync.run(blocking_inside())
