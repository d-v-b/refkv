import assert from "node:assert/strict";
import { test } from "node:test";
import { writePhkv } from "../src/phkv.ts";
import { ARCHIVE_KEY, encodeId, makeHandler, PHKV_ARCHIVE_KEY, WORKER_HEADER } from "../src/server.ts";
import { type ArchiveDesc, writeVzip } from "../src/writer.ts";

const PREFIX = "https://viewer.test/vz/";
const utf8 = (s: string) => new TextEncoder().encode(s);
const remote = utf8("0123456789abcdefghijklmnopqrstuvwxyz");

const desc: ArchiveDesc = {
  sources: [{ url: "https://data.test/blob.bin", size: BigInt(remote.length) }],
  entries: [
    { key: "zarr.json", bytes: utf8('{"zarr_format":3,"node_type":"group"}') },
    { key: "a/c/0", ranges: [{ source: 0, offset: 10n, length: 10n }] },
    { key: "a/c/1", ranges: [{ source: 0, offset: 0n, length: 5n }, { data: utf8("--") }, { source: 0, offset: 30n, length: 4n }] },
  ],
};

/** A handler that serves the archive `bytes` for any `archive/` URL, and counts the source reads. */
function handlerFor(bytes: Uint8Array, reads: string[] = []) {
  return makeHandler({
    prefix: PREFIX,
    fetchArchive: async () => bytes,
    fetchRange: async (url, start, end) => {
      reads.push(`${url} ${start}-${end}`);
      return { data: remote.subarray(start, end), size: remote.length };
    },
  });
}

const ID = encodeId("https://store.test/example.phkv");
const get = (h: ReturnType<typeof handlerFor>, path: string, init: RequestInit = {}) =>
  h(new Request(`${PREFIX}archive/${ID}/${path}`, init));
const text = async (r: Response) => new TextDecoder().decode(await r.arrayBuffer());

test("a phkv store is served as plain HTTP, and so is a vzip archive of the same input", async () => {
  for (const [name, bytes] of [["phkv", writePhkv(desc)], ["vzip", await writeVzip(desc)]] as const) {
    const reads: string[] = [];
    const h = handlerFor(bytes, reads);
    const meta = await get(h, "zarr.json");
    assert.equal(meta.status, 200, name);
    assert.equal(meta.headers.get(WORKER_HEADER), "1");
    assert.equal(meta.headers.get("Content-Type"), "application/json");
    assert.equal(await text(meta), '{"zarr_format":3,"node_type":"group"}');
    assert.equal(await text(await get(h, "a/c/0")), "abcdefghij", name);
    assert.equal(await text(await get(h, "a/c/1")), "01234--uvwx", name);
    const part = await get(h, "a/c/1", { headers: { Range: "bytes=3-6" } });
    assert.equal(part.status, 206, name);
    assert.equal(part.headers.get("Content-Range"), "bytes 3-6/11");
    assert.equal(await text(part), "34--", name);
    assert.equal((await get(h, "a/c/9")).status, 404, name);
    assert.equal((await get(h, "a/c/1", { headers: { Range: "bytes=50-" } })).status, 416, name);
    const head = await get(h, "a/c/0", { method: "HEAD" });
    assert.equal(head.headers.get("Content-Length"), "10", name);
    assert.ok(reads.length > 0 && reads.every((r) => r.startsWith("https://data.test/blob.bin ")), name);
  }
});

test("a phkv handler answers its own store for the download key", async () => {
  const store = writePhkv(desc);
  const h = handlerFor(store);
  const r = await get(h, PHKV_ARCHIVE_KEY);
  assert.equal(r.status, 200);
  assert.deepEqual(new Uint8Array(await r.arrayBuffer()), store);
  assert.match(r.headers.get("Content-Disposition") ?? "", /example\.phkv/);
  // the vzip key serves the same bytes: the archive is whatever it is
  assert.equal((await get(h, ARCHIVE_KEY)).status, 200);
});

test("a file that is neither a phkv store nor a vzip archive is a server error, not a crash", async () => {
  const r = await get(handlerFor(utf8("not an archive at all, really, no")), "zarr.json");
  assert.equal(r.status, 500);
  assert.match(await text(r), /not a vzip archive|archive/);
});

// A real file through the Rust core (needs refkv_ir.wasm: `just js::wasm`): the phkv handler's
// zarrified store holds the keys and values the vzip handler's does.
test("zarrifying a TIFF gives a phkv store with the vzip archive's keys and values", async () => {
  const fs = await import("node:fs");
  const file = new Uint8Array(fs.readFileSync(new URL("../../fixtures/tiff/float32_uncompressed_be.ome.tif", import.meta.url)));
  const url = "https://data.test/float32_uncompressed_be.ome.tif";
  const make = (format: "vzip" | "phkv") =>
    makeHandler({
      prefix: PREFIX,
      format,
      openFile: async () => ({ size: file.length, read: async (o, n) => file.subarray(o, o + n) }),
      fetchRange: async (_u, start, end) => ({ data: file.subarray(start, end), size: file.length }),
    });
  const base = `${PREFIX}tiff/${encodeId(url)}/`;
  const [phkv, vzip] = [make("phkv"), make("vzip")];
  const store = new Uint8Array(await (await phkv(new Request(`${base}${PHKV_ARCHIVE_KEY}`))).arrayBuffer());
  const { PhkvArchive } = await import("../src/phkv.ts");
  const keys = (await PhkvArchive.openStore(store, url)).keys();
  assert.ok(keys.length > 2);
  assert.ok(keys.includes("zarr.json"));
  for (const k of keys) {
    const [a, b] = await Promise.all([phkv(new Request(base + k)), vzip(new Request(base + k))]);
    assert.equal(a.status, 200, k);
    assert.deepEqual(new Uint8Array(await a.arrayBuffer()), new Uint8Array(await b.arrayBuffer()), k);
  }
});
