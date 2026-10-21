"""N5 stores through the IR (refkv.virtualize → refkv.ir.store → rust/refkv-ir/src/n5.rs;
spec/virtualize/n5.md <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/n5.md>). One test
of the accepted fixture stores: the hierarchy outside refkv_source is the frozen reference's
(conformance/virtualize/reference/refkv_reference/n5, revision 25), the references are each a
whole object at the reference's keys, and refkv_source is the store IR's mirror, which rebuilds
every node's document byte for byte and the listing. One test of the recorded ignored keys, and
one of the rejected fixtures (each rejected by both)."""

import io
import json
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

from refkv import _core
from refkv.ir.store import StoreIrOutput, virtualize_store
from refkv.virtualize import Rejected, _virtualize as virtualize  # the profile output, not the store
from refkv.virtualize.common import same
from refkv_reference.revision import STORES_REVISION

from ir_helpers import LEGACY_SOURCE, as_current, current_names

FIXTURES = Path(__file__).parents[3] / "fixtures" / "n5"
STORES = sorted(p for p in FIXTURES.iterdir() if p.is_dir())
URL = "https://data.test/n5/{}/"


def files(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_accepted_stores_have_the_reference_hierarchy_and_a_store_mirror():
    from refkv_reference import virtualize as frozen

    accepted = 0
    for root in STORES:
        if root.name.startswith("n5_reject_"):
            continue
        url = URL.format(root.name)
        _, ref = frozen(str(root), url)
        fmt, out = virtualize(str(root), url=url)
        assert fmt == "n5" and isinstance(out, StoreIrOutput), root.name
        docs = {k: json.loads(v) for k, v in out.entries.items() if not k.startswith("refkv_source/")}
        want = current_names({k: v for k, v in ref.docs.items() if not k.startswith(LEGACY_SOURCE)})
        assert want["zarr.json"]["attributes"]["refkv_virtualized"].pop("revision") == STORES_REVISION
        assert same(docs, want), root.name
        assert out.refs == {as_current(k): [("object", ref.origins.get(k, k), 0, n)] for k, n in ref.chunks}, root.name
        assert out.sources == ref.sources, root.name
        # refkv_source: the mirror, which rebuilds the listing and each node's document
        own = json.loads(out.entries["refkv_source/zarr.json"])["attributes"]["refkv_virtualized"]["n5"]
        stored = files(root)
        assert own["ir"]["objects"] == len(stored) and "empty" not in own, root.name
        held = 0
        for key, size, data in _core.rebuild_store(out.entries.get):
            assert size == len(stored[key]), (root.name, key)
            if data is not None:
                assert key.endswith("attributes.json") and data == stored[key], (root.name, key)
                held += 1
        # held: exactly the documents of the nodes (a node's attributes.json beside its zarr.json)
        nodes = [k for k in stored if k.endswith("attributes.json")
                 and k.removesuffix("attributes.json") + "zarr.json" in out.entries]
        assert held == len(nodes) >= 1, root.name
        accepted += 1
    assert accepted == 17


def test_ignored_keys_are_recorded_beside_the_mirror():
    """The listing's recorded ignored keys (spec/virtualize.md §1.4
    <https://github.com/d-v-b/refkv/blob/main/spec/virtualize.md#14-store-inputs>) are the member
    `ignored` of refkv_source's source metadata, in UTF-8 byte order; a folder marker is not."""
    doc = b'{"dimensions": [4], "blockSize": [2], "dataType": "uint8", "compression": {"type": "raw"}}'
    objects = {"attributes.json": doc, "0": b"x", "1": b"", "é/./x": b"y", "a//0": b"z", "m/": b""}

    def opener(req, timeout=None):
        class R(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        if "list-type=2" in req.full_url:
            contents = "".join(f"<Contents><Key>s/{escape(k)}</Key><Size>{len(v)}</Size></Contents>"
                               for k, v in sorted(objects.items()))
            return R(f"<ListBucketResult><IsTruncated>false</IsTruncated>{contents}</ListBucketResult>".encode())
        key = req.full_url.removeprefix("http://h/b/s/")
        a, b = req.headers["Range"].removeprefix("bytes=").split("-")
        return R(objects[key][int(a):int(b) + 1])

    out, _, _ = virtualize_store("http://h/b/s/", fmt="n5", opener=opener)
    own = json.loads(out.entries["refkv_source/zarr.json"])["attributes"]["refkv_virtualized"]["n5"]
    assert own["ignored"] == ["a//0", "é/./x"]
    assert "refkv_source/empty.json" not in out.entries
    assert out.refs == {"0": [("object", "0", 0, 1)]}


@pytest.mark.parametrize("root", [p for p in STORES if p.name.startswith("n5_reject_")], ids=lambda p: p.name)
def test_rejects_what_the_reference_rejects(root):
    from refkv_reference import virtualize as frozen

    url = URL.format(root.name)
    with pytest.raises(Rejected):
        frozen(str(root), url)
    with pytest.raises(Rejected):
        virtualize(str(root), url=url)


@pytest.fixture(scope="module")
def n5_probes(tmp_path_factory) -> Path:
    """The hostile N5 stores at the tests' sizes (python/tests/ir/gen_n5_probes.py --small)."""
    import subprocess
    import sys

    out = tmp_path_factory.mktemp("n5_probes")
    subprocess.run([sys.executable, "python/tests/ir/gen_n5_probes.py", str(out), "--small"],
                   cwd=Path(__file__).parents[3], check=True, capture_output=True)
    return out


def test_hostile_stores_are_refused_or_virtualized_as_the_reference_does(n5_probes):
    """Each probe store is rejected by both, or gives the reference's hierarchy outside
    refkv_source and a mirror that rebuilds every node's document; none takes long."""
    import time

    from refkv_reference import virtualize as frozen

    seen = 0
    for root in sorted(n5_probes.iterdir()):
        url = URL.format(root.name)
        try:
            _, ref = frozen(str(root), url)
        except Rejected:
            ref = None
        t = time.monotonic()
        try:
            _, out = virtualize(str(root), url=url)
        except Rejected:
            out = None
        assert time.monotonic() - t < 20, root.name
        assert (ref is None) == (out is None), root.name
        seen += 1
        if out is None:
            continue
        docs = {k: json.loads(v) for k, v in out.entries.items() if not k.startswith("refkv_source/")}
        want = current_names({k: v for k, v in ref.docs.items() if not k.startswith(LEGACY_SOURCE)})
        del want["zarr.json"]["attributes"]["refkv_virtualized"]["revision"]
        assert same(docs, want), root.name
        assert out.refs == {as_current(k): [("object", ref.origins.get(k, k), 0, n)] for k, n in ref.chunks}, root.name
        stored = files(root)
        for key, size, data in _core.rebuild_store(out.entries.get):
            assert size == len(stored[key]) and data in (None, stored[key]), (root.name, key)
    assert seen == 13


def test_under_a_root_dataset_the_mirror_and_the_other_objects_are_keys_of_refkv_source():
    """An array has no children: the root dataset's mirror and other objects are plain keys
    under refkv_source/ (spec/virtualize/n5.md §6
    <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/n5.md#6-other-objects>), and an empty
    block is in the mirror's listing, not in a refkv_source/empty.json."""
    for name in ("n5_root_dataset_objects", "n5_root_dataset_empty_block"):
        _, out = virtualize(str(FIXTURES / name), url=URL.format(name))
        assert json.loads(out.entries["zarr.json"])["node_type"] == "array", name
        assert [k for k in out.entries if k.endswith("zarr.json") and not k.startswith("refkv_source/")] == ["zarr.json"]
        assert "refkv_source/zarr.json" in out.entries and "refkv_source/empty.json" not in out.entries, name
        listing = {k: n for k, n, _ in _core.rebuild_store(out.entries.get)}
        assert listing == {k: len(v) for k, v in files(FIXTURES / name).items()}, name
    _, out = virtualize(str(FIXTURES / "n5_root_dataset_objects"), url=URL.format("x"))
    size = (FIXTURES / "n5_root_dataset_objects" / "README").stat().st_size
    assert out.refs["refkv_source/objects/README"] == [("object", "README", 0, size)]
