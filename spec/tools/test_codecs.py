"""The codec registry (spec/zarrify/codecs/<name>.json) and what derives from it (profiles.py)."""

import json
import re
import tomllib

import profiles
import pytest

ROOT = profiles.ROOT


def test_the_registry_names_every_codec_refkv_writes_and_provides():
    codecs = {c["name"]: c for c in profiles.load_codecs()}
    core = set()
    for path in (ROOT / "rust" / "refkv-ir" / "src").rglob("*.rs"):
        core |= set(re.findall(r'"(imagecodecs_\w+|numcodecs\.\w+|refkv\.\w+)"', path.read_text(encoding="utf-8")))
    assert core <= set(codecs), core - set(codecs)
    pyproject = tomllib.loads((ROOT / "python" / "pyproject.toml").read_text(encoding="utf-8"))
    entry_points = set(pyproject["project"]["entry-points"]["zarr.codecs"])
    assert entry_points == {n for n, c in codecs.items() if c["package"] == "refkv"}
    assert all(c["extra"] in pyproject["project"]["optional-dependencies"] for c in codecs.values())


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"named_by": "someone"}, "is not one of"),
        ({"extra": "video"}, "is not one of"),
        ({"section": "conventions.md#3-arrays"}, "name their section"),
        ({"name": "x.lzma"}, "name the file"),
        ({"rows": [{"step": 5, "codec": "c", "when": "w"}]}, "a step of 1 to 4"),
    ],
)
def test_a_malformed_codec_entry(tmp_path, monkeypatch, change, message):
    entry = json.loads((profiles.CODECS_DIR / "imagecodecs_lzma.json").read_text(encoding="utf-8"))
    (tmp_path / "imagecodecs_lzma.json").write_text(json.dumps(entry | change), encoding="utf-8")
    monkeypatch.setattr(profiles, "CODECS_DIR", tmp_path)
    with pytest.raises(ValueError, match=message):
        profiles.load_codecs()


def test_a_refkv_codec_named_otherwise(tmp_path, monkeypatch):
    entry = json.loads((profiles.CODECS_DIR / "refkv.skip.json").read_text(encoding="utf-8"))
    (tmp_path / "skip.json").write_text(json.dumps(entry | {"name": "skip"}), encoding="utf-8")
    monkeypatch.setattr(profiles, "CODECS_DIR", tmp_path)
    with pytest.raises(ValueError, match=r"named refkv\.<codec>"):
        profiles.load_codecs()
