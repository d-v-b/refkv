"""A store's listing (spec/zarrify.md §1.5
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>): the objects and
continuation token of one S3 ListObjectsV2 response, and a Hub store's commit, the objects of one
page of its tree, and the page's next link. The rules are the Rust core's
(rust/refkv-ir/src/host/listing.rs); this module gives them Python's types.
"""

from __future__ import annotations

from refkv._rules import body, rule


def parse_listing(data: bytes, prefix: str) -> tuple[list[tuple[str, int]], str | None]:
    """The objects listed by one ListObjectsV2 response, and the continuation token if the
    listing is truncated (§1.5
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>), or
    Rejected."""
    r = rule("parse_listing", body=body(data), prefix=prefix)
    return [(k, n) for k, n in r["objects"]], r["token"]


def parse_hub_revision(data: bytes) -> str:
    """The commit `C` of a Hub revision's response (§1.5
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>), or Rejected:
    a gated repository's too."""
    return rule("parse_hub_revision", body=body(data))


def parse_hub_tree(data: bytes, prefix: str) -> tuple[list[tuple[str, int]], int]:
    """The objects (path, size) of one page of a Hub tree listing (§1.5
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>), its files
    under the directory `prefix` (its directories list nothing), and its number of entries."""
    r = rule("parse_hub_tree", body=body(data), prefix=prefix)
    return [(k, n) for k, n in r["objects"]], r["entries"]


def next_link(fields: list[str], url: str, endpoint: str) -> str | None:
    """The next link of a Hub tree response whose `Link` fields are `fields` and which `url`
    answered, resolved against `url` (§1.5
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>); None if it has
    none; Rejected if the fields are not links, give two next links, or one off `endpoint`'s
    origin."""
    return rule("next_link", fields=list(fields), url=url, endpoint=endpoint)
