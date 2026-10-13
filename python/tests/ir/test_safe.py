"""SAFE through the IR (vzip.virtualize, the Rust core natively): the directory form as a store
run, whose output references ranges of its band files and data sources, and the zip form as a
file run. Equivalence with the frozen reference is compare.py's; the IR, its mirror and every
rejection are rust/vzip-ir/tests/safe.rs's."""

from pathlib import Path

import pytest

from vzip.ir.store import StoreIrOutput
from vzip.virtualize import Rejected, virtualize
from vzip.virtualize.common import Output
from vzip.virtualize.store import object_url

FIXTURES = Path(__file__).parents[3] / "fixtures" / "safe"
URL = "https://data.test/safe/{}"


def test_both_forms_through_the_ir():
    # the directory form: one url source per object used (the band files), in order of first
    # use, each pinning its size; then the data sources; vzip_source the store IR's mirror
    d = FIXTURES / "safe_l1c"
    fmt, out = virtualize(str(d), url=URL.format("safe_l1c/"))
    assert fmt == "safe" and isinstance(out, StoreIrOutput)
    urls, data, refs = out.table()
    firsts = []
    for k in sorted(refs, key=str.encode):
        for r in refs[k]:
            if isinstance(r, tuple) and r[0] < len(urls) and r[0] not in firsts:
                firsts.append(r[0])
    assert firsts == list(range(len(urls)))
    assert all(u.startswith(URL.format("safe_l1c/")) for u in urls) and data
    assert {object_url(out.url, k): n for k, n in out.sizes.items() if object_url(out.url, k) in urls} == \
        {u: (d / u[len(out.url):]).stat().st_size for u in urls}
    assert "vzip_source/ir/held/zarr.json" in out.entries and out.summary["form"] == "store"
    # the zip form: source 0 the file, then the data sources; nothing copied but the mirror
    z = FIXTURES / "safe_l1c.SAFE.zip"
    fmt, zout = virtualize(str(z), url=URL.format(z.name))
    assert fmt == "safe" and isinstance(zout, Output) and zout.summary["form"] == "zip"
    assert {k for k in zout.bytes_entries if not k.startswith("vzip_source/")} == \
        {k for k in out.entries if not k.startswith("vzip_source/")}


def test_a_product_the_core_refuses_is_rejected():
    with pytest.raises(Rejected, match="the root has both MTD_MSIL1C.xml and MTD_MSIL2A.xml"):
        virtualize(str(FIXTURES / "safe_reject_two_mtd"), url=URL.format("x/"))


def test_a_zip_file_the_core_refuses_is_rejected():
    with pytest.raises(Rejected, match="a zip entry is encrypted"):
        virtualize(str(FIXTURES / "safe_reject_zip_encrypted.SAFE.zip"), url=URL.format("x.SAFE.zip"))
