// Hub stores (spec/zarrify.md §1.5
// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>), as
// python/tests/test_zarrify_hub.py checks them for the Python host: a Hub store URL's
// repository, revision and directory, the revision and tree responses, the Link header's next
// link, and listing over HTTP with a stand-in fetch.

import assert from "node:assert/strict";
import { test } from "node:test";
import { ImageError } from "../src/zarrify/common.ts";
import { openHttpSource } from "../src/zarrify/ir/source.ts";
import {
  HUB,
  hubBase,
  hubEndpoint,
  hubPage,
  hubRepo,
  hubRevisionUrl,
  hubTreeUrl,
  nextLink,
  openHttpStore,
  StoreError,
  StoreReadError,
} from "../src/zarrify/store.ts";

const utf8 = new TextEncoder();
const SHA = "1bae0e1088993b9b82de08f59de5be5a34e42170";
const API = "https://huggingface.co/api/datasets/o/r";
const STORE = "https://huggingface.co/datasets/o/r/tree/main/a.zarr/";
const REVISION = `${API}/revision/main?expand%5B%5D=sha&expand%5B%5D=gated`;
const TREE = `${API}/tree/${SHA}/a.zarr?recursive=true`;
const PAGE2 = `${API}/tree/${SHA}/a.zarr?recursive=true&cursor=Mg`;
const BASE = `https://huggingface.co/datasets/o/r/resolve/${SHA}/a.zarr/`;

type Answer = number | { body: unknown; link?: string };

function fakeFetch(pages: Record<string, Answer>) {
  return async (url: string) => {
    const v = pages[url];
    if (v === undefined) throw new Error(`unexpected request ${url}`);
    if (typeof v === "number") return new Response("x", { status: v });
    const body = typeof v.body === "string" ? v.body : JSON.stringify(v.body);
    return new Response(body, { status: 200, headers: v.link ? { Link: v.link } : {} });
  };
}

const file = (path: string, size: unknown = 1) => ({ type: "file", oid: "x", size, path });
const dir = (path: string) => ({ type: "directory", oid: "x", size: 0, path });
const revision = (extra: object = {}) => ({ _id: "x", id: "o/r", sha: SHA, gated: false, ...extra });

/** The Hub store at STORE whose tree is `first` (and PAGE2), its revision `rev`. */
function hub(first: Answer, rest: Record<string, Answer> = {}, rev: Answer = { body: revision() }) {
  return openHttpStore(STORE, { fetch: fakeFetch({ [REVISION]: rev, [TREE]: first, ...rest }) });
}

test("parses Hub store URLs", () => {
  const cases: [string, string, object | undefined][] = [
    ["https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash-1017/tree/main/", HUB,
      { endpoint: HUB, kind: "models", owner: "deepseek-ai", name: "DeepSeek-V4.1-Flash-1017", revision: "main", directory: "" }],
    ["HTTPS://HuggingFace.co/datasets/o/r/tree/refs%2Fpr%2F1/a%20b/%C3%A9/", HUB,
      { endpoint: HUB, kind: "datasets", owner: "o", name: "r", revision: "refs/pr/1", directory: "a b/é/" }],
    ["http://127.0.0.1:9/hub/f/fixtures/tree/main/zarr2/s/", "http://127.0.0.1:9/hub",
      { endpoint: "http://127.0.0.1:9/hub", kind: "models", owner: "f", name: "fixtures", revision: "main", directory: "zarr2/s/" }],
    // not under the endpoint: an S3-style store
    ["https://s3.amazonaws.com/b/", HUB, undefined],
    ["http://127.0.0.1:9/hubx/f/r/tree/main/", "http://127.0.0.1:9/hub", undefined],
  ];
  for (const [url, endpoint, want] of cases) assert.deepEqual(hubRepo(url, endpoint), want, url);
  const repo = hubRepo("https://huggingface.co/datasets/o/r/tree/refs%2Fpr%2F1/a%20b/c/", HUB)!;
  assert.equal(hubRevisionUrl(repo), `${API}/revision/refs%2Fpr%2F1?expand%5B%5D=sha&expand%5B%5D=gated`);
  assert.equal(hubTreeUrl(repo, SHA), `${API}/tree/${SHA}/a%20b/c?recursive=true`);
  assert.equal(hubBase(repo, SHA), `https://huggingface.co/datasets/o/r/resolve/${SHA}/a%20b/c/`);
  const model = hubRepo("https://huggingface.co/o/r/tree/v1/", HUB)!;
  assert.equal(hubTreeUrl(model, SHA), `https://huggingface.co/api/models/o/r/tree/${SHA}?recursive=true`);
  assert.equal(hubBase(model, SHA), `https://huggingface.co/o/r/resolve/${SHA}/`);
  assert.equal(hubEndpoint(), HUB);
  assert.equal(hubEndpoint("http://127.0.0.1:9/hub/"), "http://127.0.0.1:9/hub");
  assert.equal(hubPage("https://huggingface.co/o/r/tree/main", HUB), true);
  assert.equal(hubPage("https://huggingface.co/o/r/tree/main/", HUB), false);
  assert.equal(hubPage("https://huggingface.co/o/r/resolve/main/x.bin", HUB), false);
  assert.equal(hubPage("https://huggingface.co/o/r/tree/main?x", HUB), false);
  // the next link, of several, in either case, resolved against the URL that answered
  assert.equal(nextLink(null, TREE, HUB), undefined);
  assert.equal(nextLink('<https://huggingface.co/x>; rel="prev"', TREE, HUB), undefined);
  assert.equal(nextLink(`<https://huggingface.co/x>; rel=prev, <?cursor=Mg> ; REL = "first Next"`, TREE, HUB),
    `${API}/tree/${SHA}/a.zarr?cursor=Mg`);
});

test("lists a Hub store over HTTP", async () => {
  // two pages, with directories (which list nothing), sizes 0 and beyond 2^32, a file's size
  // written 5.0, and ignored keys
  const store = await hub(
    { body: [dir("a.zarr/x"), file("a.zarr/.zgroup", 18), file("a.zarr/x/0", 2 ** 40)], link: `<${PAGE2}>; rel="next"` },
    { [PAGE2]: { body: `[{"type": "file", "path": "a.zarr/x/1", "size": 5.0, "lfs": {}}, ${JSON.stringify(file("a.zarr/x/2", 0))}, ${JSON.stringify(file("a.zarr/x/./y", 3))}]` } },
  );
  assert.deepEqual([...store.objects], [[".zgroup", 18], ["x/0", 2 ** 40], ["x/1", 5], ["x/2", 0]]);
  assert.deepEqual(store.ignored, ["x/./y"]);
  assert.equal(store.url, BASE);
  assert.deepEqual([store.requests, store.listed], [3, 5]);
});

test("reads a Hub store's objects at its commit", async () => {
  const pages: Record<string, Answer> = {
    [REVISION]: { body: revision() },
    [TREE]: { body: [file("a.zarr/.zgroup", 2)] },
    [`${BASE}.zgroup`]: { body: "{}" },
  };
  const store = await openHttpStore(STORE, { fetch: fakeFetch(pages) });
  assert.deepEqual(await store.read(".zgroup"), utf8.encode("{}"));
});

const rejections: [string, string, Record<string, Answer>][] = [
  ["a URL that names no revision", "https://huggingface.co/o/r/", {}],
  ["a Space", "https://huggingface.co/spaces/o/r/tree/main/", {}],
  ["a bad repository name", "https://huggingface.co/o/r%20s/tree/main/", {}],
  ["an empty directory segment", "https://huggingface.co/o/r/tree/main/a//", {}],
  ["a dot segment", "https://huggingface.co/o/r/tree/main/%2E%2E/", {}],
  ["a revision that is not UTF-8", "https://huggingface.co/o/r/tree/%FF/", {}],
];
for (const [name, url, pages] of rejections) {
  test(`Hub store URL: ${name}`, async () => {
    await assert.rejects(openHttpStore(url, { fetch: fakeFetch(pages) }), StoreError);
  });
}

test("Hub: a repository that does not exist, or a private one (401)", async () => {
  await assert.rejects(hub(0, {}, 401), StoreError);
});

test("Hub: a revision it does not have (404)", async () => {
  await assert.rejects(hub(0, {}, 404), StoreError);
});

test("Hub: a directory it does not have (404)", async () => {
  await assert.rejects(hub(404), StoreError);
});

test("Hub: a gated repository", async () => {
  await assert.rejects(hub(0, {}, { body: revision({ gated: "manual" }) }), StoreError);
});

test("Hub: a revision that is not JSON", async () => {
  await assert.rejects(hub(0, {}, { body: "<html>" }), StoreError);
});

test("Hub: a revision whose sha is not a commit", async () => {
  await assert.rejects(hub(0, {}, { body: revision({ sha: SHA.toUpperCase() }) }), StoreError);
});

test("Hub: a tree that is not an array", async () => {
  await assert.rejects(hub({ body: { error: "x" } }), StoreError);
});

test("Hub: a tree entry of another type", async () => {
  await assert.rejects(hub({ body: [{ type: "submodule", path: "a.zarr/m" }] }), StoreError);
});

test("Hub: a tree entry outside the directory", async () => {
  await assert.rejects(hub({ body: [file("b/.zgroup")] }), StoreError);
});

test("Hub: a tree entry that is the directory itself", async () => {
  await assert.rejects(hub({ body: [dir("a.zarr/")] }), StoreError);
});

test("Hub: a path with a lone surrogate", async () => {
  await assert.rejects(hub({ body: '[{"type": "file", "path": "a.zarr/\\ud800", "size": 1}]' }), StoreError);
});

test("Hub: a file without a size", async () => {
  await assert.rejects(hub({ body: [{ type: "file", path: "a.zarr/x" }] }), StoreError);
});

test("Hub: a size that is not an integer", async () => {
  await assert.rejects(hub({ body: [file("a.zarr/x", 1.5)] }), StoreError);
});

test("Hub: a size beyond 2^53 - 1", async () => {
  await assert.rejects(hub({ body: [file("a.zarr/x", 2 ** 53)] }), StoreError);
});

test("Hub: a key listed twice", async () => {
  await assert.rejects(hub({ body: [file("a.zarr/x")], link: `<${PAGE2}>; rel="next"` }, { [PAGE2]: { body: [file("a.zarr/x")] } }),
    StoreError);
});

test("Hub: a Link header that is not links", async () => {
  await assert.rejects(hub({ body: [file("a.zarr/x")], link: `${PAGE2}; rel="next"` }), StoreError);
});

test("Hub: two next links", async () => {
  await assert.rejects(hub({ body: [file("a.zarr/x")], link: `<${PAGE2}>; rel="next", <${PAGE2}&x>; rel="next"` }), StoreError);
});

test("Hub: a next link off the endpoint", async () => {
  await assert.rejects(hub({ body: [file("a.zarr/x")], link: `<https://example.org/tree>; rel="next"` }), StoreError);
});

test("Hub: a next link requested already", async () => {
  await assert.rejects(hub({ body: [file("a.zarr/x")], link: `<${TREE}>; rel="next"` }), StoreError);
});

test("Hub: an empty page with a next link", async () => {
  await assert.rejects(hub({ body: [], link: `<${PAGE2}>; rel="next"` }), StoreError);
});

test("Hub: a listing that answers 503 fails", async () => {
  await assert.rejects(hub(503), StoreReadError);
});

test("Hub: a repository's page is rejected unread", async () => {
  await assert.rejects(openHttpSource("https://huggingface.co/o/r/tree/main"), ImageError);
});
