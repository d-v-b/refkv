// The phkv reader (rust/refkv-phkv) built for wasm32 and driven by js/src/phkv.ts, over a standalone
// store that rust/refkv-phkv/examples/wasm_fixture.rs writes (`just js::phkv-wasm` builds both).

import assert from "node:assert/strict";
import fs from "node:fs/promises";
import { test } from "node:test";
import { PhkvError, PhkvStore, type PhkvIo, type PhkvSource } from "../src/phkv.ts";

const WEB = new URL("../../rust/refkv-phkv/target/web/", import.meta.url);

async function load(): Promise<[WebAssembly.Module, Uint8Array]> {
  try {
    const wasm = await fs.readFile(process.env.REFKV_PHKV_WASM || new URL("refkv_phkv.wasm", WEB));
    const store = await fs.readFile(new URL("fixture.phkv", WEB));
    return [await WebAssembly.compile(wasm as Uint8Array<ArrayBuffer>), store];
  } catch (e) {
    throw new Error(`cannot read the phkv wasm module or its fixture (build them with \`just js::phkv-wasm\`): ${(e as Error).message}`);
  }
}

// the values of the fixture, by the formulas of wasm_fixture.rs
const big = new Uint8Array(400_000);
new DataView(big.buffer).setUint32(0, 0, true);
for (let i = 0; i < 100_000; i++) new DataView(big.buffer).setUint32(4 * i, i % 97, true);
const object = Uint8Array.from({ length: 5000 }, (_, i) => i % 256);
const concat = (...p: Uint8Array[]) => Uint8Array.from(p.flatMap((a) => [...a]));

/** An io over the fixture's bytes and the object, that counts its reads. */
function fakeIo(file: Uint8Array) {
  const log = { file: [] as [number, number][], source: [] as [string, number, number][] };
  const io: PhkvIo = {
    size: file.length,
    async read(offset, length) {
      log.file.push([offset, length]);
      return file.slice(offset, offset + length);
    },
    async readSource(source: PhkvSource, offset, length) {
      log.source.push([source.url ?? "", offset, length]);
      assert.equal(source.url, "https://data.test/object");
      return object.slice(offset, offset + length);
    },
  };
  return { io, log };
}

test("opens the store and counts its keys", async () => {
  const [module, file] = await load();
  const { io, log } = fakeIo(file);
  const store = await PhkvStore.open(module, io);
  assert.equal(store.keyCount, 3007);
  // the tail holds a store this small whole: one read
  assert.equal(log.file.length, 1);
  store.close();
});

test("reads bytes values of every codec", async () => {
  const [module, file] = await load();
  const store = await PhkvStore.open(module, fakeIo(file).io);
  assert.deepEqual(await store.get("a/zarr.json"), new TextEncoder().encode('{"zarr_format":3}'));
  for (const key of ["a/plain", "a/deflate", "a/zstd"]) assert.deepEqual(await store.get(key), big, key);
  assert.deepEqual(await store.get("many/01234"), new TextEncoder().encode("value 1234"));
  assert.equal(await store.get("no/such/key"), undefined);
  assert.equal(await store.get("many/9999"), undefined);
});

test("resolves references, through a source, literal bytes and another key", async () => {
  const [module, file] = await load();
  const { io, log } = fakeIo(file);
  const store = await PhkvStore.open(module, io);
  assert.deepEqual(await store.get("a/ref"), object.slice(100, 400));
  assert.deepEqual(await store.get("a/concat"), concat(object.slice(0, 10), new TextEncoder().encode("--"), object.slice(4990)));
  assert.deepEqual(await store.get("a/via-key"), big.slice(4, 12));
  // only the first two touched the object, each for the bytes it names
  assert.deepEqual(log.source, [
    ["https://data.test/object", 100, 300],
    ["https://data.test/object", 0, 10],
    ["https://data.test/object", 4990, 10],
  ]);
  assert.deepEqual(await store.source(0), { index: 0, kind: "url", url: "https://data.test/object" });
});

test("reads windows of values", async () => {
  const [module, file] = await load();
  const store = await PhkvStore.open(module, fakeIo(file).io);
  assert.deepEqual(await store.get("a/plain", { start: 10, end: 50 }), big.slice(10, 50));
  assert.deepEqual(await store.get("a/zstd", { start: 399_990 }), big.slice(399_990));
  assert.deepEqual(await store.get("a/ref", { suffix: 7 }), object.slice(393, 400));
  assert.deepEqual(await store.get("a/ref", { start: 5, end: 5 }), new Uint8Array(0));
});

test("lists keys by prefix, in order", async () => {
  const [module, file] = await load();
  const store = await PhkvStore.open(module, fakeIo(file).io);
  assert.deepEqual(await store.list("a/"), ["a/concat", "a/deflate", "a/plain", "a/ref", "a/via-key", "a/zarr.json", "a/zstd"]);
  assert.deepEqual((await store.list("many/0299")).length, 10);
  assert.equal((await store.list("")).length, 3007);
  assert.deepEqual(await store.list("zzz"), []);
});

test("reads again from memory, and overlapping reads of one store agree", async () => {
  const [module, file] = await load();
  const { io, log } = fakeIo(file);
  const store = await PhkvStore.open(module, io);
  await store.get("a/ref");
  const before = [log.file.length, log.source.length];
  assert.deepEqual(await store.get("a/ref"), object.slice(100, 400));
  assert.deepEqual([log.file.length, log.source.length], before, "a key read before needs no read");
  // many gets at once: each answer is its own
  const keys = Array.from({ length: 60 }, (_, i) => `many/${String(i * 37).padStart(5, "0")}`);
  const got = await Promise.all(keys.map((k) => store.get(k)));
  got.forEach((v, i) => assert.deepEqual(v, new TextEncoder().encode(`value ${i * 37}`)));
});

test("a file that is no store fails with an archive error", async () => {
  const [module] = await load();
  const junk = new Uint8Array(5000).fill(7);
  await assert.rejects(PhkvStore.open(module, fakeIo(junk).io), (e: unknown) => e instanceof PhkvError && e.errorClass === "archive" && /not a phkv store/.test(e.message));
});
