"""python/tools/version.py: the version from git tags, and its PEP 440 spelling."""

import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "refkv_version_script", Path(__file__).resolve().parents[1] / "tools" / "version.py")
assert _spec is not None
assert _spec.loader is not None
version = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(version)


def test_version_for():
    cases = [
        (("v1.2.3", 0), "1.2.3", "1.2.3"),
        (("v1.2.3-rc.1", 0), "1.2.3-rc.1", "1.2.3rc1"),
        (("v1.2.3", 5), "1.2.4-dev.5", "1.2.4.dev5"),
        (("v0.9.9", 1), "0.9.10-dev.1", "0.9.10.dev1"),
        (("v1.2.3-rc.1", 2), "1.2.3-rc.2.dev.2", "1.2.3rc2.dev2"),
        ((None, 7), "0.0.0-dev.7", "0.0.0.dev7"),
        ((None, 0), "0.0.0-dev.0", "0.0.0.dev0"),
    ]
    for (tag, distance), semver, pep440 in cases:
        got = version.version_for(tag, distance)
        assert (got, version.pep440(got)) == (semver, pep440), (tag, distance)


@pytest.mark.parametrize("tag", ["v1.2", "1.2.3", "v1.2.3-beta.1", "v1.2.3.4", "vX.Y.Z"])
def test_version_for_unparseable_tag(tag):
    with pytest.raises(ValueError, match=r"is not vX\.Y\.Z or vX\.Y\.Z-rc\.K$"):
        version.version_for(tag, 0)


def test_version_for_not_semver():
    with pytest.raises(ValueError, match=r"^the version '1\.02\.3' \(from the tag 'v1\.02\.3'\) is not semver$"):
        version.version_for("v1.02.3", 0)


def test_pep440_not_a_derived_version():
    with pytest.raises(ValueError, match=r"is not X\.Y\.Z\[-rc\.K\]\[-dev\.N\]$"):
        version.pep440("1.2.3-alpha.1")
