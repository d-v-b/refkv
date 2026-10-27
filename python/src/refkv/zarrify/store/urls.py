"""Store URLs (spec/zarrify.md §1.4
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>, §1.5
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>): a store URL's
checks, the URL of each object, and the listing endpoint and key prefix of an S3-style store.
"""

from __future__ import annotations

import re

from refkv.uri import is_uri_reference
from refkv.zarrify.common import Rejected

_SPLIT = re.compile(r"^(?:([^:/?#]+):)?(?://([^/?#]*))?([^?#]*)(?:\?([^#]*))?(?:#(.*))?\Z")
_UNRESERVED = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_PATH_SAFE = _UNRESERVED | frozenset(b"!$&'()*+,;=:@/")


def _pct(data: bytes, safe: frozenset[int]) -> str:
    return "".join(chr(b) if b in safe else f"%{b:02X}" for b in data)


def object_url(store_url: str, key: str) -> str:
    """The URL of the object with relative key `key` (§1.4
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>)."""
    return store_url + _pct(key.encode(), _PATH_SAFE)


def query_encode(s: str) -> str:
    """`enc` of §1.5
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>: every byte but
    the unreserved ones percent-encoded."""
    return _pct(s.encode(), _UNRESERVED)


def check_store_url(url: str) -> tuple[str, str, str]:
    """(scheme, authority, path) of a valid store URL (§1.4
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>), or Rejected."""
    m = _SPLIT.match(url)
    if not is_uri_reference(url) or m is None:
        raise Rejected(f"{url!r} is not a valid URI")
    scheme, authority, path, query, fragment = m.groups()
    if scheme is None or scheme.lower() not in ("http", "https"):
        raise Rejected(f"store URL {url!r} is not http or https")
    if authority is None or "@" in authority:
        raise Rejected(f"store URL {url!r} has no authority, or has userinfo")
    if query is not None or fragment is not None:
        raise Rejected(f"store URL {url!r} has a query or a fragment")
    if not path.endswith("/"):
        raise Rejected(f"store URL {url!r} does not end in /")
    return scheme, authority, path


def _percent_decode(s: str) -> bytes:
    out = bytearray()
    i = 0
    while i < len(s):
        if s[i] == "%":
            out.append(int(s[i + 1 : i + 3], 16))
            i += 3
        else:
            out.append(ord(s[i]))
            i += 1
    return bytes(out)


def listing_endpoint(url: str) -> tuple[str, str]:
    """(endpoint, prefix P) of a store URL (§1.5
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>)."""
    scheme, authority, path = check_store_url(url)
    host = authority[: authority.index("]") + 1] if authority.startswith("[") else authority.rsplit(":", 1)[0]
    host = host.lower()
    labels = host.split(".")
    virtual = host.endswith(".amazonaws.com") and any(
        lab == "s3" or lab.startswith("s3-") for lab in labels[1:])
    if virtual:
        endpoint, prefix_path = f"{scheme}://{authority}/", path[1:]
    else:
        if path == "/":
            raise Rejected(f"path-style store URL {url!r} names no bucket")
        bucket, _, rest = path[1:].partition("/")
        if not bucket:
            raise Rejected(f"path-style store URL {url!r} has an empty bucket")
        endpoint, prefix_path = f"{scheme}://{authority}/{bucket}/", rest
    try:
        prefix = _percent_decode(prefix_path).decode("utf-8")
    except UnicodeDecodeError:
        raise Rejected(f"the key prefix of {url!r} is not UTF-8") from None
    return endpoint, prefix
