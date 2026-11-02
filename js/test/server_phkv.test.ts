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
