"""Every format's rejected inputs, from one table per fixture directory,
conformance/zarrify/rejects/<dir>.json, which rust/refkv-ir/build.rs also turns into the Rust
tests (one per case): each input in fixtures/<dir> is rejected by the Python host
(`refkv.zarrify._zarrify`, a file by its first bytes or a store by its objects), with a
message that holds the table's, or the case's `host` message where the host rejects it before
the profile is reached; and every fixture named as rejected is in its table."""

import json

import pytest
from ir_helpers import FIXTURES, ROOT

from refkv.zarrify import Rejected
from refkv.zarrify import _zarrify as zarrify
from refkv.zarrify import zarrify as zarrify_named
from refkv.zarrify.profile_classes import CLASSES

TABLES = sorted((ROOT / "conformance" / "zarrify" / "rejects").glob("*.json"))


def rejected_name(name: str) -> bool:
    """Whether a fixture's name says it is rejected (conformance/zarrify/HARNESS.md:
    `*_reject_*`, `unsupported_*`); a name starting with `_` is a slide's tile directory, part of
    an input, not one."""
    return not name.startswith("_") and ("reject" in name or name.startswith("unsupported"))


def table(path) -> list[dict]:
    return json.loads(path.read_text())["cases"]


CASES = [pytest.param(FIXTURES / t.stem, case, id=f"{t.stem}/{case['input']}") for t in TABLES for case in table(t)]


@pytest.mark.parametrize(("directory", "case"), CASES)
def test_the_host_rejects(directory, case):
    path = directory / case["input"]
    url = f"https://data.test/{directory.name}/{case['input']}" + ("/" if path.is_dir() else "")
    # a case with a profile is that profile's named input (spec/zarrify.md §1.7
    # <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#17-named-inputs-and-layouts>)
    named = [c for c in CLASSES.values() if c.profile == case.get("profile")]

    def run() -> None:
        if named:
            zarrify_named(named[0](str(path), url))
        else:
            zarrify(str(path), url)

    with pytest.raises(Rejected) as e:
        run()
    assert case.get("host", case["message"]) in str(e.value)


@pytest.mark.parametrize("path", TABLES, ids=lambda t: t.stem)
def test_every_rejected_fixture_is_in_its_table(path):
    fixtures = FIXTURES / path.stem
    listed = {case["input"] for case in table(path)}
    named = {p.name for p in fixtures.iterdir() if rejected_name(p.name)}
    assert named <= listed, f"not in {path}: {sorted(named - listed)}"
    assert all((fixtures / name).exists() for name in listed)


def test_every_format_with_rejected_fixtures_has_a_table():
    """A fixture directory that holds rejected inputs has a table of them."""
    dirs = {d for d in FIXTURES.iterdir() if d.is_dir() and d.name != "generators"
            and any(rejected_name(p.name) for p in d.iterdir())}
    tabled = {FIXTURES / t.stem for t in TABLES}
    assert dirs <= tabled, sorted(d.name for d in dirs - tabled)
