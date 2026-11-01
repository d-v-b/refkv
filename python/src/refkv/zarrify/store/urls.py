"""Store URLs (spec/zarrify.md §1.4
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>, §1.5
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>): a store URL's
checks, the URL of each object, the listing endpoint and key prefix of an S3-style store, and
the repository, revision and directory of a Hub store.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

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


HUB = "https://huggingface.co"
"""The Hugging Face Hub's endpoint, unless the application gives another (`HF_ENDPOINT`)."""

_HUB_NAME = re.compile(r"[A-Za-z0-9._-]+\Z")


def hub_endpoint(endpoint: str | None = None) -> str:
    """The Hub's endpoint `E` (§1.5
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>): `endpoint`, else
    the environment's `HF_ENDPOINT`, else `HUB`, without a trailing `/` (as `huggingface_hub`
    reads it). A ValueError if it is not an http(s) URL with an authority, without userinfo, a
    query or a fragment."""
    e = (endpoint if endpoint is not None else os.environ.get("HF_ENDPOINT") or HUB).rstrip("/")
    m = _SPLIT.match(e)
    if m is None or not is_uri_reference(e):
        raise ValueError(f"the Hub endpoint {e!r} is not a valid URI")
    scheme, authority, _, query, fragment = m.groups()
    if scheme is None or scheme.lower() not in ("http", "https") or not authority or "@" in authority \
            or query is not None or fragment is not None:
        raise ValueError(f"the Hub endpoint {e!r} is not an http(s) URL without userinfo, query or fragment")
    return e


@dataclass(frozen=True)
class HubRepo:
    """A Hub store (§1.5
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>): its endpoint,
    kind (`models` or `datasets`), owner, name, revision `r` and directory `D` (empty, or ending
    in `/`), the last two percent-decoded."""

    endpoint: str
    kind: str
    owner: str
    name: str
    revision: str
    directory: str

    @property
    def api(self) -> str:
        """`A`: the repository's API URL."""
        return f"{self.endpoint}/api/{self.kind}/{self.owner}/{self.name}"

    def revision_url(self) -> str:
        return f"{self.api}/revision/{query_encode(self.revision)}?expand%5B%5D=sha&expand%5B%5D=gated"

    def tree_url(self, commit: str) -> str:
        d = "/" + _pct(self.directory[:-1].encode(), _PATH_SAFE) if self.directory else ""
        return f"{self.api}/tree/{commit}{d}?recursive=true"

    def base(self, commit: str) -> str:
        """The store's base (§1.4
        <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>) at `commit`."""
        kind = "/datasets/" if self.kind == "datasets" else "/"
        return f"{self.endpoint}{kind}{self.owner}/{self.name}/resolve/{commit}/{object_url('', self.directory)}"


def _under(url: str, endpoint: str) -> str | None:
    """The rest of `url`'s path after `endpoint`'s and a `/`, when `url` has `endpoint`'s scheme
    and authority (in any case) and its path starts so; else None."""
    m, e = _SPLIT.match(url), _SPLIT.match(endpoint)
    if m is None or e is None or m.group(1) is None or m.group(2) is None:
        return None
    if (m.group(1).lower(), m.group(2).lower()) != (e.group(1).lower(), e.group(2).lower()):
        return None
    path, base = m.group(3), e.group(3) + "/"
    return path[len(base):] if path.startswith(base) else None


def hub_repo(url: str, endpoint: str) -> HubRepo | None:
    """The Hub store that the store URL `url` names under `endpoint` (§1.5
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>), None if `url`
    is not under it, or Rejected if it is but does not name one."""
    rest = _under(url, endpoint)
    if rest is None:
        return None
    check_store_url(url)
    segments = rest.split("/")
    kind = "models"
    if segments[0] == "datasets":
        kind, segments = "datasets", segments[1:]
    # owner, name, "tree", rev, dir segments..., "" (the trailing /)
    if len(segments) < 5 or segments[2] != "tree" or segments[-1] != "" or \
            not all(_HUB_NAME.match(n) and n not in (".", "..") for n in segments[:2]) or \
            any(not x for x in segments[3:-1]):
        raise Rejected(f"Hub store URL {url!r} is not <owner>/<repo>/tree/<rev>/[<dir>/] "
                       "(or that after datasets/)")
    try:
        revision = _percent_decode(segments[3]).decode("utf-8")
        directory = "".join(_percent_decode(x).decode("utf-8") + "/" for x in segments[4:-1])
    except UnicodeDecodeError:
        raise Rejected(f"the revision or directory of {url!r} is not UTF-8") from None
    if any(x in (".", "..") for x in directory.split("/")[:-1]):
        raise Rejected(f"the directory of {url!r} has a segment . or ..")
    return HubRepo(endpoint, kind, segments[0], segments[1], revision, directory)


def hub_page(url: str, endpoint: str) -> bool:
    """Whether the file input `url` would be a Hub store if it ended in `/` (§1.5
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>): a web page
    that is rejected without being read."""
    split = _SPLIT.match(url)
    if split is None or split.group(4) is not None or split.group(5) is not None or url.endswith("/"):
        return False
    try:
        return hub_repo(url + "/", endpoint) is not None
    except Rejected:
        return False
