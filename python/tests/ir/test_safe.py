"""SAFE through the IR (refkv.zarrify, the Rust core natively): the directory form as a store
run, whose output references ranges of its band files and data sources, and the zip form as a
file run. Equivalence with the frozen reference is compare.py's; the IR, its mirror and every
rejection are rust/refkv-ir/tests/safe.rs's."""

import re
from pathlib import Path

import pytest

from refkv.ir.store import StoreIrOutput
from refkv.zarrify import Rejected
from refkv.zarrify import _zarrify as zarrify  # the profile output, not the store
from refkv.zarrify.common import Output
from refkv.zarrify.store import object_url

FIXTURES = Path(__file__).parents[3] / "fixtures" / "safe"
URL = "https://data.test/safe/{}"


def test_both_forms_through_the_ir():
    # the directory form: one url source per object used (the band files), in order of first
    # use, each pinning its size; then the data sources; refkv_source the store IR's mirror
    d = FIXTURES / "safe_l1c"
    fmt, out = zarrify(str(d), url=URL.format("safe_l1c/"))
    assert fmt == "safe"
    assert isinstance(out, StoreIrOutput)
    urls, data, refs = out.table()
    firsts = []
    for k in sorted(refs, key=str.encode):
        for r in refs[k]:
            if isinstance(r, tuple) and r[0] < len(urls) and r[0] not in firsts:
                firsts.append(r[0])
    assert firsts == list(range(len(urls)))
    assert all(u.startswith(URL.format("safe_l1c/")) for u in urls)
    assert data
    assert {object_url(out.url, k): n for k, n in out.sizes.items() if object_url(out.url, k) in urls} == \
        {u: (d / u[len(out.url):]).stat().st_size for u in urls}
    assert "refkv_source/ir/held/zarr.json" in out.entries
    assert out.summary["form"] == "store"
    # the zip form: source 0 the file, then the data sources; nothing copied but the mirror
    z = FIXTURES / "safe_l1c.SAFE.zip"
    fmt, zout = zarrify(str(z), url=URL.format(z.name))
    assert fmt == "safe"
    assert isinstance(zout, Output)
    assert zout.summary["form"] == "zip"
    assert {k for k in zout.bytes_entries if not k.startswith("refkv_source/")} == \
        {k for k in out.entries if not k.startswith("refkv_source/")}


def test_a_product_the_core_refuses_is_rejected():
    with pytest.raises(Rejected, match=re.escape("the root has both MTD_MSIL1C.xml and MTD_MSIL2A.xml")):
        zarrify(str(FIXTURES / "safe_reject_two_mtd"), url=URL.format("x/"))


def test_a_zip_file_the_core_refuses_is_rejected():
    with pytest.raises(Rejected, match="a zip entry is encrypted"):
        zarrify(str(FIXTURES / "safe_reject_zip_encrypted.SAFE.zip"), url=URL.format("x.SAFE.zip"))
