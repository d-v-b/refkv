"""Hub stores (spec/zarrify.md §1.5
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>): a Hugging Face
Hub repository at a revision, its revision resolved to a commit, its tree listed page by page,
and its files read at the commit. The responses are trimmed recordings of the Hub's (the
DeepSeek-V4.1-Flash-1017 model, with `limit=` to make it paginate). The browser implementation
has the same tests in js/test/hub_store.test.ts, and the CLI in rust/refkv-cli/src/store.rs.
"""

import io
import json
import os
import urllib.error
from email.message import Message
from typing import override

import pytest

from refkv.zarrify import Rejected, zarrify
from refkv.zarrify.store import (
    HUB,
    HubStore,
    ListingFailed,
    choose_profile,
    hub_endpoint,
    hub_page,
    hub_repo,
    open_store,
)

SHA = "1bae0e1088993b9b82de08f59de5be5a34e42170"
API = "https://huggingface.co/api/models/deepseek-ai/DeepSeek-V4.1-Flash-1017"
STORE = "https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash-1017/tree/main/"
REVISION = f"{API}/revision/main?expand%5B%5D=sha&expand%5B%5D=gated"
TREE = f"{API}/tree/{SHA}?recursive=true"
NEXT = (f"{API}/tree/{SHA}?expand=false&recursive=true&limit=3&cursor=ZXlKbWFXeGxYMjVoYldVaU9pSmxibU52WkdsdVp5OVNSVUZF"
        "VFVVdWJXUWlMQ0owY21WbFgyOXBaQ0k2SWpBNU1XSTJOekJsWTJKbU1UTXhZekE1TTJZMk1HRTNaV0kyT1RWaU9EazNNamRrWkRGa01ESWlmUT09OjEw")


def revision(sha: str = SHA, gated: object = False) -> bytes:
    return json.dumps({"_id": "6ad2e0265a59c8bc18707765", "id": "deepseek-ai/DeepSeek-V4.1-Flash-1017",
                       "sha": sha, "gated": gated}).encode()


def entry(path: str, size: int | None = None, kind: str = "file") -> dict:
    e: dict = {"type": kind, "oid": "a8a050df998304d9f6ab196df1d56a6a6b0cb438", "size": 0, "path": path}
    if size is not None:
        e["size"] = size
    return e


PAGE1 = [
    entry("encoding", kind="directory"),
    entry("encoding/tests", kind="directory"),
    entry(".gitattributes", 1594),
]
PAGE2 = [
    entry("config.json", 3311),
    # an LFS file: its size is the file's, not the pointer's
    {**entry("model-00001-of-00048.safetensors", 970533624),
     "lfs": {"oid": "5df8", "size": 970533624, "pointerSize": 131}, "xetHash": "08169c26"},
    entry("encoding/tests/test_input_1.json", 2),
]


class Response(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200, links: list[str] | None = None):
        super().__init__(body)
        self.status = status
        self.headers = Message()
        for link in links or []:
            self.headers["Link"] = link

    @override
    def __enter__(self):
        return self

    @override
    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


def opener(pages: dict):
    """A fake urlopen: request URL → body, (body, Link headers), or HTTP status."""
    def open_(req):
        v = pages[req.full_url]
        if isinstance(v, int):
            raise urllib.error.HTTPError(req.full_url, v, "x", Message(), None)
        if isinstance(v, tuple):
            return Response(v[0], links=v[1])
        return Response(v)
    return open_


def tree(entries: list) -> bytes:
    return json.dumps(entries).encode()


def paged(page1: bytes = tree(PAGE1), links: list[str] | None = None, page2: bytes | object = tree(PAGE2)) -> dict:
    pages: dict = {REVISION: revision(), TREE: (page1, [f'<{NEXT}>; rel="next"'] if links is None else links)}
    if page2 is not None:
        pages[NEXT] = page2
    return pages


def open_hub(pages: dict, url: str = STORE) -> HubStore:
    store = open_store(url, opener=opener(pages), hub=HUB)
    assert isinstance(store, HubStore)
    return store


def test_hub_urls():
    def parts(url: str) -> tuple | None:
        r = hub_repo(url, HUB)
        return None if r is None else (r.kind, r.owner, r.name, r.revision, r.directory, r.revision_url(),
                                       r.tree_url(SHA), r.base(SHA))
    assert parts(STORE) == ("models", "deepseek-ai", "DeepSeek-V4.1-Flash-1017", "main", "", REVISION, TREE,
                            f"https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash-1017/resolve/{SHA}/")
    assert parts("HTTPS://HuggingFace.CO/datasets/o/r.v-2_x/tree/refs%2Fpr%2F1/a%20b/%C3%A9/") == (
        "datasets", "o", "r.v-2_x", "refs/pr/1", "a b/é/",
        "https://huggingface.co/api/datasets/o/r.v-2_x/revision/refs%2Fpr%2F1?expand%5B%5D=sha&expand%5B%5D=gated",
        f"https://huggingface.co/api/datasets/o/r.v-2_x/tree/{SHA}/a%20b/%C3%A9?recursive=true",
        f"https://huggingface.co/datasets/o/r.v-2_x/resolve/{SHA}/a%20b/%C3%A9/")
    # another host, or a path beside the endpoint's: not a Hub store, but an S3-style one
    assert parts("https://s3.amazonaws.com/b/tree/main/") is None
    assert hub_repo("http://127.0.0.1:9/hubx/o/r/tree/main/", "http://127.0.0.1:9/hub") is None
    # an endpoint with a path (a test server's)
    r = hub_repo("http://127.0.0.1:9/hub/o/r/tree/main/d/", "http://127.0.0.1:9/hub")
    assert r is not None
    assert r.base(SHA) == f"http://127.0.0.1:9/hub/o/r/resolve/{SHA}/d/"
    assert hub_endpoint("https://hf-mirror.com/") == "https://hf-mirror.com"
    assert hub_page("https://huggingface.co/o/r/tree/main", HUB)
    assert hub_page("https://huggingface.co/o/r/tree/main/dir", HUB)
    assert not hub_page("https://huggingface.co/o/r/resolve/main/config.json", HUB)
    assert not hub_page("https://huggingface.co/o/r/tree/main?x", HUB)


def test_lists_a_hub_store():
    pages = paged()
    base = f"https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash-1017/resolve/{SHA}/"
    pages[f"{base}config.json"] = b"{}" + b" " * 3309
    store = open_hub(pages)
    assert store.commit == SHA
    assert store.url == store.location == f"https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash-1017/resolve/{SHA}/"
    assert store.objects == {".gitattributes": 1594, "config.json": 3311,
                             "model-00001-of-00048.safetensors": 970533624, "encoding/tests/test_input_1.json": 2}
    assert (store.requests, store.listed, store.ignored) == (3, 4, [])
    assert store.read("config.json").startswith(b"{}")
    # no profile takes a repository of safetensors files (yet)
    with pytest.raises(Rejected, match=r"not an N5, Zarr v2 or SAFE store"):
        choose_profile(store)


def test_lists_a_hub_directory_of_a_dataset():
    sha = "715e1970c7099cae22fe672020acb6b706121385"
    api = "https://huggingface.co/api/datasets/KokosDev/mur-sst-ml-benchmark-Zarr"
    pages = {
        f"{api}/revision/{sha}?expand%5B%5D=sha&expand%5B%5D=gated": revision(sha),
        f"{api}/tree/{sha}/pacific_sst.zarr?recursive=true": tree([
            entry("pacific_sst.zarr/lat", kind="directory"), entry("pacific_sst.zarr/.zgroup", 24),
            entry("pacific_sst.zarr/lat/.zarray", 300), entry("pacific_sst.zarr/lat/0", 4800)]),
    }
    store = open_hub(pages, f"https://huggingface.co/datasets/KokosDev/mur-sst-ml-benchmark-Zarr/tree/{sha}/pacific_sst.zarr/")
    assert store.objects == {".zgroup": 24, "lat/.zarray": 300, "lat/0": 4800}
    assert store.url == f"https://huggingface.co/datasets/KokosDev/mur-sst-ml-benchmark-Zarr/resolve/{sha}/pacific_sst.zarr/"
    assert choose_profile(store) == "zarr2"


def test_names_a_hub_store_by_its_url_when_renamed():
    store = open_store(STORE, "https://data.example/s/", opener=opener(paged()), hub=HUB)
    assert store.url == "https://data.example/s/"


@pytest.mark.parametrize("url", [
    "https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash-1017/",  # no revision
    "https://huggingface.co/o/r/blob/main/",  # not a tree
    "https://huggingface.co/spaces/o/r/tree/main/",  # a Space
    "https://huggingface.co/o/r/tree//",  # an empty revision
    "https://huggingface.co/o/r/tree/main/a//",  # an empty segment of the directory
    "https://huggingface.co/o/../tree/main/",  # a name ..
    "https://huggingface.co/o/r~x/tree/main/",  # not a name
])
def test_rejects_a_url_that_is_not_a_hub_tree(url):
    with pytest.raises(Rejected):
        hub_repo(url, HUB)


def test_rejects_a_revision_or_directory_that_is_not_utf8():
    with pytest.raises(Rejected, match="UTF-8"):
        hub_repo("https://huggingface.co/o/r/tree/%FF/", HUB)


def test_rejects_a_dot_segment_of_the_directory():
    with pytest.raises(Rejected, match="segment"):
        hub_repo("https://huggingface.co/o/r/tree/main/a/%2E%2E/", HUB)


def test_rejects_a_missing_or_private_repository():
    with pytest.raises(Rejected, match="HTTP 401"):
        open_hub({REVISION: 401})


def test_rejects_a_missing_revision():
    with pytest.raises(Rejected, match="HTTP 404"):
        open_hub({REVISION: 404})


def test_rejects_a_missing_directory():
    with pytest.raises(Rejected, match="HTTP 404"):
        open_hub({**paged(), TREE: 404})


def test_rejects_a_gated_repository():
    with pytest.raises(Rejected, match="gated"):
        open_hub({**paged(), REVISION: revision(gated="manual")})


def test_rejects_a_revision_without_a_commit():
    with pytest.raises(Rejected, match="sha"):
        open_hub({**paged(), REVISION: revision(sha="1bae0e1")})


def test_rejects_a_revision_that_is_not_json():
    with pytest.raises(Rejected, match="JSON"):
        open_hub({**paged(), REVISION: b"<!doctype html>"})


def test_rejects_a_tree_that_is_not_json():
    with pytest.raises(Rejected, match="JSON"):
        open_hub({**paged(), TREE: b'[{"type": "file",'})


def test_rejects_a_tree_that_is_not_an_array():
    with pytest.raises(Rejected, match="array"):
        open_hub({**paged(), TREE: b'{"error": "x"}'})


def test_rejects_an_entry_of_another_type():
    with pytest.raises(Rejected, match="type"):
        open_hub({**paged(), TREE: tree([entry("x", 1, kind="submodule")])})


def test_rejects_an_entry_outside_the_directory():
    sha_tree = f"https://huggingface.co/api/models/o/r/tree/{SHA}/d?recursive=true"
    pages = {"https://huggingface.co/api/models/o/r/revision/main?expand%5B%5D=sha&expand%5B%5D=gated": revision(),
             sha_tree: tree([entry("e/x", 1)])}
    with pytest.raises(Rejected, match="not under"):
        open_hub(pages, "https://huggingface.co/o/r/tree/main/d/")


def test_rejects_a_file_without_a_size():
    with pytest.raises(Rejected, match="size"):
        open_hub({**paged(), TREE: tree([{"type": "file", "path": "x"}])})


def test_rejects_a_size_that_is_not_an_integer():
    with pytest.raises(Rejected, match="size"):
        open_hub({**paged(), TREE: tree([entry("x", 2**53)])})


def test_rejects_a_path_listed_twice():
    with pytest.raises(Rejected, match="twice"):
        open_hub(paged(page2=tree([entry(".gitattributes", 1594)])))


def test_rejects_a_link_header_that_is_not_links():
    with pytest.raises(Rejected, match="Link"):
        open_hub(paged(links=[f'{NEXT}; rel="next"']))


def test_rejects_two_next_links():
    with pytest.raises(Rejected, match="more than one next link"):
        open_hub(paged(links=[f'<{NEXT}>; rel="next"', f"<{NEXT}&x=1>; rel=next"]))


def test_rejects_a_next_link_off_the_endpoint():
    with pytest.raises(Rejected, match="not on"):
        open_hub(paged(links=['<https://evil.test/api/tree>; rel="next"']))


def test_rejects_a_next_link_requested_already():
    with pytest.raises(Rejected, match="requested already"):
        open_hub(paged(page2=(tree(PAGE2), [f'<{NEXT}>; rel="next"'])))


def test_rejects_an_empty_page_with_a_next_link():
    with pytest.raises(Rejected, match="lists nothing"):
        open_hub(paged(page1=tree([])))


def test_reads_next_links_among_others():
    # several links, parameters in any case and quoting, the next link relative to the page
    links = ['<https://huggingface.co/api/x>; rel="prev"; title="a,b;c"',
             f' <{NEXT[len("https://huggingface.co"):]}> ;REL = "first Next" , <x>;rel=last']
    assert open_hub(paged(links=links)).listed == 4


def test_listing_fails_on_server_errors(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)
    with pytest.raises(ListingFailed):
        open_hub({**paged(), TREE: 503})


def test_rejects_a_hub_page_as_a_file_without_reading_it(monkeypatch):
    monkeypatch.delenv("HF_ENDPOINT", raising=False)
    with pytest.raises(Rejected, match="end it in /"):
        zarrify("https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash-1017/tree/main")


@pytest.mark.skipif(not os.environ.get("REFKV_LIVE_HUB"), reason="reads huggingface.co: set REFKV_LIVE_HUB=1")
def test_lists_deepseek_v4_1_flash_live():
    store = open_store(f"https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash-1017/tree/{SHA}/", hub=HUB)
    assert isinstance(store, HubStore)
    assert len(store.objects) == 83
    assert sum(k.endswith(".safetensors") for k in store.objects) == 48
