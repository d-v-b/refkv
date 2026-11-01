"""The safetensors profile's store form (spec/zarrify/safetensors.md §9
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#9-the-store-form>): a store of
safetensors files zarrified as one group, each presented shard a group of its tensors compared with
the safetensors library's reading of that file; the store's source metadata (shards not presented,
indexes and configurations); the reads; and the mirror's listing and held documents."""

import json
import zipfile

import safetensors
import zarr
from ir_helpers import FIXTURES
from obstore.store import LocalStore
from test_safetensors import expected, same_bits

import refkv
import refkv.zarr
from refkv import _core
from refkv._narrow import mapping, ndarray, sequence, string
from refkv.ir.store import zarrify_store

STORES = FIXTURES / "safetensors"


def zarrified(name: str, tmp_path):
    """(the output, its root group) of the store fixture `name`, a local directory served at
    https://data.test/<name>/: the archive written, and read with the fixtures as that host."""
    out, _ir, stats = zarrify_store(str(STORES / name), f"https://data.test/{name}/", fmt="safetensors")
    out.summary["reads"] = stats
    out.write(str(tmp_path / f"{name}.vzip"))
    reader = refkv.open(str(tmp_path / f"{name}.vzip"), registry={"https://data.test": LocalStore(STORES)})
    return out, zarr.open_group(refkv.zarr.store(reader), mode="r")


def test_matches_the_library_and_rebuilds(tmp_path):
    """Every presented shard's tensors are the library's reading of that file, bit for bit; the
    other objects are referenced whole; the mirror lists every object and holds each document
    the store form read whole, byte for byte."""
    for name in ("st_store_model", "st_store_mixed"):
        root_dir = STORES / name
        _, root = zarrified(name, tmp_path)
        meta = mapping(mapping(root.attrs["refkv_zarrified"])["safetensors"])
        files = [mapping(f) for f in sequence(meta["files"])]
        for f in files:
            if "path" not in f:
                continue
            group = root.get_group(string(f["path"]))
            own = mapping(mapping(group.attrs["refkv_zarrified"])["safetensors"])
            library = dict(safetensors.deserialize((root_dir / string(f["key"])).read_bytes()))
            for t in map(mapping, sequence(own["tensors"])):
                lib = library[string(t["name"])]
                got = ndarray(group.get_array(string(t["path"]))[...])
                same_bits(got, expected(lib["dtype"], lib["shape"], lib["data"]), f"{name}:{f['key']}:{t['name']}")
        stored = {p.relative_to(root_dir).as_posix(): p.read_bytes() for p in root_dir.rglob("*") if p.is_file()}
        with zipfile.ZipFile(tmp_path / f"{name}.vzip") as z:
            names = set(z.namelist())
            rebuilt = _core.rebuild_store(lambda k, z=z, names=names: z.read(k) if k in names else None)
        assert {k: n for k, n, _ in rebuilt} == {k: len(v) for k, v in stored.items()}, name
        held = {k: b for k, _, b in rebuilt if b is not None}
        refused = {string(c["key"]) for c in map(mapping, sequence(meta.get("configs", []))) if "problem" in c}
        documents = {k for k in stored if k.endswith((".safetensors.index.json", "config.json")) and k not in refused}
        assert held == {k: stored[k] for k in documents}, name


def test_writes_the_model_store(tmp_path):
    """A model's shards, its index and its configuration (spec/zarrify/safetensors.md §9.2
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#92-source-metadata>): a
    group per shard at its key, declaring the shard's own source metadata, the index's agreement,
    the configuration as written, and every other object referenced whole."""
    out, _ = zarrified("st_store_model", tmp_path)
    meta = json.loads(out.entries["zarr.json"])["attributes"]["refkv_zarrified"]["safetensors"]
    assert meta == {
        "files": [{"key": "model-00001-of-00002.safetensors", "path": "model-00001-of-00002.safetensors"},
                  {"key": "model-00002-of-00002.safetensors", "path": "model-00002-of-00002.safetensors"}],
        "indexes": [{"key": "model.safetensors.index.json", "agrees": True, "metadata": {"total_size": 0}}],
        "configs": [{"key": "config.json", "config": {
            "architectures": ["Tiny"], "quantization_config": {"expert_dtype": "fp4"}, "hidden_size": 4}}],
    }
    shard = json.loads(out.entries["model-00002-of-00002.safetensors/zarr.json"])["attributes"]
    assert [t["name"] for t in shard["refkv_zarrified"]["safetensors"]["tensors"]] == ["norm", "layers.1.w"]
    doc = json.loads(out.entries["model-00001-of-00002.safetensors/layers.0.w/zarr.json"])
    assert doc["data_type"] == "refkv.float8_e4m3fn"
    assert sorted(k for k in out.refs if k.startswith("refkv_source/objects/")) == [
        "refkv_source/objects/README.md", "refkv_source/objects/inference/generate.py"]
    assert out.summary["chunks"] == 5


def test_records_what_it_does_not_present(tmp_path):
    """A shard whose header fails a check, one whose key is not a Zarr path, an index that
    disagrees with its shards and a configuration that is not JSON are recorded, each with its
    reason, and the store is zarrified (spec/zarrify/safetensors.md §9.3
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safetensors.md#93-reading-and-checks>);
    a header past the first read is read in a second batch."""
    out, _ = zarrified("st_store_mixed", tmp_path)
    meta = json.loads(out.entries["zarr.json"])["attributes"]["refkv_zarrified"]["safetensors"]
    assert {f["key"]: f.get("path", f.get("reason")) for f in meta["files"]} == {
        "__hidden/x.safetensors": "name: a segment of its key is not a Zarr node name",
        "long/model.safetensors": "long/model.safetensors",
        "text/model.safetensors": "text/model.safetensors",
        "unet/diffusion_pytorch_model.safetensors": "unet/diffusion_pytorch_model.safetensors",
        "vae/diffusion_pytorch_model.safetensors":
            "the tensor a of shape [3] and data type F32 is 12 bytes, not the 8 its data_offsets give",
    }
    assert meta["indexes"] == [{"key": "text/model.safetensors.index.json", "agrees": False, "problem_count": 1,
                                "problems": ['"gone" is in "other.safetensors", which is not a presented shard']}]
    assert meta["configs"] == [{"key": "unet/config.json", "config": {"in_channels": 4}},
                               {"key": "vae/config.json", "problem": "invalid JSON: expecting a value at byte 11"}]
    assert sorted(k for k in out.refs if k.startswith("refkv_source/objects/")) == [
        "refkv_source/objects/__hidden/x.safetensors", "refkv_source/objects/vae/config.json",
        "refkv_source/objects/vae/diffusion_pytorch_model.safetensors"]
    assert mapping(out.summary["reads"])["batches"] == 2
    assert out.summary["unpresented"] == 2
