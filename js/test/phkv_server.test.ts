// The phkv route of the service worker's handler (src/server.ts): a standalone store served as plain
// HTTP, read in place. The store is rust/refkv-phkv/examples/wasm_fixture.rs's (`just js::phkv-wasm`).

import assert from "node:assert/strict";
import fs from "node:fs/promises";
import { test } from "node:test";
import { HttpResolutionError } from "../src/http.ts";
import { encodeId, makeHandler } from "../src/server.ts";

const PREFIX = "https://viewer.test/vz/";
const STORE = "https://data.test/store.phkv";
const OBJECT = "https://data.test/object";
const WEB = new URL("../../rust/refkv-phkv/target/web/", import.meta.url);

const fixture = await fs.readFile(new URL("fixture.phkv", WEB)).catch((e: Error) => {
  throw new Error(`cannot read the phkv fixture (build it with \`just js::phkv-wasm\`): ${e.message}`);
});
const big = new Uint8Array(400_000);
for (let i = 0; i < 100_000; i++) new DataView(big.buffer).setUint32(4 * i, i % 97, true);
const object = Uint8Array.from({ length: 5000 }, (_, i) => i % 256);

function handler(options: { policy?: Parameters<typeof makeHandler>[0]["policy"]; reads?: string[] } = {}) {
  const reads = options.reads ?? [];
  return makeHandler({
    prefix: PREFIX,
    ...(options.policy ? { policy: options.policy } : {}),
    openPhkvFile: async (url) => {
      if (url.endsWith("missing.phkv")) throw new HttpResolutionError(`${url}: HTTP 404`);
      const bytes = url.endsWith("junk.phkv") ? new Uint8Array(5000).fill(7) : new Uint8Array(fixture);
      return { size: bytes.length, read: async (o, n) => bytes.slice(o, o + n) };
    },
    fetchRange: async (url, start, end, pins) => {
      reads.push(`${url} ${start}-${end}${pins.size !== undefined ? ` size=${pins.size}` : ""}`);
      assert.equal(url, OBJECT);
      return { data: object.slice(start, end), size: object.length };
    },
  });
}

const route = (key: string, store = STORE) => `${PREFIX}phkv/${encodeId(store)}/${key}`;
const get = (h: ReturnType<typeof handler>, key: string, init?: RequestInit, store?: string) => h(new Request(route(key, store), init));
const bytes = async (r: Response) => new Uint8Array(await r.arrayBuffer());

test("serves a phkv store's values as plain HTTP", async () => {
  const h = handler();
  const meta = await get(h, "a/zarr.json");
  assert.equal(meta.status, 200);
  assert.equal(meta.headers.get("Content-Type"), "application/json");
  assert.equal(meta.headers.get("X-Vzip-Worker"), "1");
  assert.deepEqual(await meta.json(), { zarr_format: 3 });
  for (const key of ["a/plain", "a/deflate", "a/zstd"]) {
    const r = await get(h, key);
    assert.equal(r.status, 200, key);
    assert.equal(r.headers.get("Content-Length"), "400000");
    assert.deepEqual(await bytes(r), big, key);
  }
  assert.equal((await get(h, "many/02999")).status, 200);
});

test("resolves references with a source's bytes, under the pins and the policy", async () => {
  const reads: string[] = [];
  const h = handler({ reads });
  assert.deepEqual(await bytes(await get(h, "a/ref")), object.slice(100, 400));
  assert.deepEqual(reads, [`${OBJECT} 100-400`]);
  assert.deepEqual(await bytes(await get(h, "a/via-key")), big.slice(4, 12));
  // a policy that does not allow the source's URL refuses the read
  const refused = handler({ policy: { prefixes: ["https://elsewhere.test/"] } });
  const r = await get(refused, "a/ref");
  assert.equal(r.status, 502);
  assert.match(await r.text(), /not under an allowed URL prefix/);
});

test("serves ranges: from a start, as a suffix, and past the end", async () => {
  const h = handler();
  const window = await get(h, "a/plain", { headers: { Range: "bytes=10-49" } });
  assert.equal(window.status, 206);
  assert.equal(window.headers.get("Content-Range"), "bytes 10-49/*");
  assert.equal(window.headers.get("Content-Length"), "40");
  assert.deepEqual(await bytes(window), big.slice(10, 50));
  const open = await get(h, "a/ref", { headers: { Range: "bytes=290-" } });
  assert.equal(open.headers.get("Content-Range"), "bytes 290-299/*");
  assert.deepEqual(await bytes(open), object.slice(390, 400));
  const suffix = await get(h, "a/zstd", { headers: { Range: "bytes=-7" } });
  assert.equal(suffix.status, 206);
  assert.equal(suffix.headers.get("Content-Range"), "bytes 399993-399999/400000");
  assert.deepEqual(await bytes(suffix), big.slice(399_993));
  const past = await get(h, "a/ref", { headers: { Range: "bytes=300-310" } });
  assert.equal(past.status, 416);
  assert.equal((await get(h, "a/plain", { headers: { Range: "bytes=5-4" } })).status, 416);
});

test("HEAD gives the size without a body", async () => {
  const r = await get(handler(), "a/deflate", { method: "HEAD" });
  assert.equal(r.status, 200);
  assert.equal(r.headers.get("Content-Length"), "400000");
  assert.equal((await r.arrayBuffer()).byteLength, 0);
});

test("reports a missing key, a store that cannot be opened and one that is no store", async () => {
  const h = handler();
  assert.equal((await get(h, "no/such/key")).status, 404);
  assert.equal((await get(h, "")).status, 404);
  assert.equal((await get(h, "a/")).status, 404);
  const gone = await get(h, "a/plain", undefined, "https://data.test/missing.phkv");
  assert.equal(gone.status, 502);
  const junk = await get(h, "a/plain", undefined, "https://data.test/junk.phkv");
  assert.equal(junk.status, 422);
  assert.match(await junk.text(), /not a phkv store/);
  assert.equal((await h(new Request(route("a/plain"), { method: "POST" }))).status, 405);
});

test("opens a store once, for any number of requests", async () => {
  let opens = 0;
  const h = makeHandler({
    prefix: PREFIX,
    openPhkvFile: async () => {
      opens++;
      return { size: fixture.length, read: async (o, n) => new Uint8Array(fixture).slice(o, o + n) };
    },
  });
  const responses = await Promise.all(Array.from({ length: 20 }, (_, i) => get(h, `many/${String(i).padStart(5, "0")}`)));
  assert.deepEqual(responses.map((r) => r.status), Array(20).fill(200));
  assert.equal(opens, 1);
});
