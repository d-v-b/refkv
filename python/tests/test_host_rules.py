"""The host rules of the Rust core (rust/refkv-ir/src/host), by name through `_core.coding`: a
result as JSON, a rejection as `_core.Rejected`, and a usage error or bad parameters as
ValueError."""

from __future__ import annotations

import json

import pytest

from refkv import _core


def rule(name: str, **params: object) -> object:
    return json.loads(_core.coding(name, json.dumps(params)))


def test_rules_by_name():
    assert rule("object_url", store_url="http://h/", key="a b") == "http://h/a%20b"
    assert rule("input", location="https://h/a/") == {"kind": "store"}
    assert rule("late", keys=["zarr.json", "foozarr.json"], lazy=["refkv_source/"]) == [True, False]


def test_rejects_as_rejected():
    with pytest.raises(_core.Rejected, match="not http or https"):
        rule("check_store_url", url="ftp://h/a/")


def test_refuses_a_usage_error_as_value_error():
    with pytest.raises(ValueError, match="or below it"):
        rule("scope_keys", location="https://h/a/x.vsi", scope=["https://e/"])
