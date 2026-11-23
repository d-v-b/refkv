"""Store URLs (spec/zarrify.md §1.4
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>, §1.5
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>): a store URL's
checks, the URL of each object, the listing endpoint and key prefix of an S3-style store, and
the repository, revision and directory of a Hub store. The rules are the Rust core's
(rust/refkv-ir/src/host/urls.rs); a local directory's `file:` URL, which this module checks
against the file system, is the host's.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from refkv._rules import DEFAULTS, rule
from refkv.errors import Rejected
from refkv.uri import file_path, file_uri, is_uri_reference

_SPLIT = re.compile(r"^(?:([^:/?#]+):)?(?://([^/?#]*))?([^?#]*)(?:\?([^#]*))?(?:#(.*))?\Z")


def object_url(store_url: str, key: str) -> str:
    """The URL of the object with relative key `key` (§1.4
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>)."""
    return rule("object_url", store_url=store_url, key=key)


def object_urls(store_url: str, keys: list[str]) -> list[str]:
    """`object_url` of each key, in one call of the core."""
    return rule("object_urls", store_url=store_url, keys=keys)


def query_encode(s: str) -> str:
    """`enc` of §1.5
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>: every byte but
    the unreserved ones percent-encoded."""
    return rule("query_encode", s=s)


def check_store_url(url: str) -> tuple[str, str, str]:
    """(scheme, authority, path) of a valid store URL (§1.4
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>), or Rejected."""
    scheme, authority, path = rule("check_store_url", url=url)
    return scheme, authority, path


def directory_url(path: str) -> str:
    """The `file:` URL of the local directory at `path`, ending in `/`: the store URL a local
    directory is named by when the caller gives none (§1.4
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>)."""
    url = file_uri(path)
    return url if url.endswith("/") else url + "/"


def check_directory_url(url: str, path: str) -> None:
    """Check `url`, a `file:` URL given as the store URL of the local directory at `path` (§1.4
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>): it names that
    directory itself, on this machine (no host), ending in `/`, without a query or a fragment;
    or Rejected. A `file:` URL names no other store: not another directory, whose files the
    output would then claim, nor a store read over the network. FileNotFoundError when `path`
    is no directory."""
    if not Path(path).is_dir():
        raise FileNotFoundError(f"{path}: no such directory")
    m = _SPLIT.match(url)
    if not is_uri_reference(url) or m is None:
        raise Rejected(f"{url!r} is not a valid URI")
    _, _, path_part, query, fragment = m.groups()
    if query is not None or fragment is not None or not path_part.endswith("/"):
        raise Rejected(f"store URL {url!r} has a query or a fragment, or does not end in /")
    try:
        named = file_path(url)
    except ValueError as e:  # a host (a UNC path), or a path no local file has
        raise Rejected(f"store URL {url!r} names no local directory: {e}") from None
    try:
        same = Path(named).samefile(path)
    except OSError:
        same = False
    if not same:
        raise Rejected(f"store URL {url!r} is not the URL of the directory {path!r}: a local directory is "
                       f"named by its own file: URL ({directory_url(path)}), or by the http(s) URL it is served from")


def listing_endpoint(url: str) -> tuple[str, str]:
    """(endpoint, prefix P) of a store URL (§1.5
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>)."""
    endpoint, prefix = rule("listing_endpoint", url=url)
    return endpoint, prefix


def listing_url(endpoint: str, prefix: str, token: str | None) -> str:
    """The URL of a ListObjectsV2 request (§1.5
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>): the first
    page's, or with `token`, the page after the response that gave it."""
    return rule("listing_url", endpoint=endpoint, prefix=prefix, token=token)


HUB: str = DEFAULTS["hub"]
"""The Hugging Face Hub's endpoint, unless the application gives another (`HF_ENDPOINT`)."""

def hub_endpoint(endpoint: str | None = None) -> str:
    """The Hub's endpoint `E` (§1.5
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>): `endpoint`, else
    the environment's `HF_ENDPOINT`, else `HUB`, without a trailing `/` (as `huggingface_hub`
    reads it). A ValueError if it is not an http(s) URL with an authority, without userinfo, a
    query or a fragment."""
    e = endpoint if endpoint is not None else os.environ.get("HF_ENDPOINT") or HUB
    return rule("hub_endpoint", endpoint=e)


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

    def _json(self) -> dict[str, str]:
        return {"endpoint": self.endpoint, "kind": self.kind, "owner": self.owner, "name": self.name,
                "revision": self.revision, "directory": self.directory}

    @property
    def api(self) -> str:
        """`A`: the repository's API URL."""
        return f"{self.endpoint}/api/{self.kind}/{self.owner}/{self.name}"

    def revision_url(self) -> str:
        return rule("hub_urls", repo=self._json(), commit=None)["revision_url"]

    def tree_url(self, commit: str) -> str:
        return rule("hub_urls", repo=self._json(), commit=commit)["tree_url"]

    def base(self, commit: str) -> str:
        """The store's base (§1.4
        <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>) at `commit`."""
        return rule("hub_urls", repo=self._json(), commit=commit)["base"]


def hub_repo(url: str, endpoint: str) -> HubRepo | None:
    """The Hub store that the store URL `url` names under `endpoint` (§1.5
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>), None if `url`
    is not under it, or Rejected if it is but does not name one."""
    r = rule("hub_repo", url=url, endpoint=endpoint)
    if r is None:
        return None
    return HubRepo(r["endpoint"], r["kind"], r["owner"], r["name"], r["revision"], r["directory"])


def hub_page(url: str, endpoint: str) -> bool:
    """Whether the file input `url` would be a Hub store if it ended in `/` (§1.5
    <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>): a web page
    that is rejected without being read."""
    return rule("hub_page", url=url, endpoint=endpoint)
