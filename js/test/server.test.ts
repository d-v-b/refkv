import assert from "node:assert/strict";
import fs from "node:fs";
import { test } from "node:test";
import { HttpResolutionError } from "../src/http.ts";
import { ARCHIVE_KEY, encodeId, makeHandler, type SavedArchives } from "../src/server.ts";

const PREFIX = "https://viewer.test/vz/";
const FIXTURES = new URL("../../fixtures/tiff/", import.meta.url);
const remote = (name: string) => `https://data.test/${name}`;
const local = (url: string) => new Uint8Array(fs.readFileSync(new URL(url.slice("https://data.test/".length), FIXTURES)));

function handler(fetched: string[] = []) {
  return makeHandler({
    prefix: PREFIX,
    openFile: async (url) => {
      if (url.endsWith("missing.tif")) throw new HttpResolutionError(`${url}: HTTP 404`);
      const bytes = local(url);
      return { size: bytes.length, read: async (o, n) => bytes.subarray(o, o + n) };
    },
    fetchRange: async (url, start, end) => {
      fetched.push(`${start}-${end}`);
      if (url.endsWith("gone.tif")) throw new HttpResolutionError(`${url}: HTTP 404`);
      const body = local(url);
      return { data: body.subarray(start, end), size: body.length }; // the size pin is checked
    },
    fetchArchive: async () => {
      const r = await makeHandler({
        prefix: PREFIX,
        openFile: async (url) => {
          const bytes = local(url);
          return { size: bytes.length, read: async (o, n) => bytes.subarray(o, o + n) };
        },
      })(new Request(`${PREFIX}tiff/${encodeId(remote("float32_uncompressed_be.ome.tif"))}/${ARCHIVE_KEY}`));
      return new Uint8Array(await r.arrayBuffer());
    },
  });
}

const get = (h: ReturnType<typeof handler>, path: string, init?: RequestInit) =>
  h(new Request(PREFIX + path, init));
const tiff = (name: string) => `tiff/${encodeId(remote(name))}/`;

test("serves zarrified TIFFs and archives as plain HTTP", async () => {
  const fetched: string[] = [];
  const h = handler(fetched);
  const base = tiff("float32_uncompressed_be.ome.tif");

  const group = await get(h, `${base}zarr.json`);
  assert.equal(group.status, 200);
  assert.equal(group.headers.get("Content-Type"), "application/json");
  assert.equal((await group.json()).attributes.ome.version, "0.5");
  assert.equal(fetched.length, 0, "metadata needs no reads of tile data");

  // An uncompressed float32 tile is 32 * 32 * 4 bytes, read straight from the TIFF.
  const chunk = await get(h, `${base}0/c/1/2`);
  assert.equal(chunk.status, 200);
  assert.equal(chunk.headers.get("Content-Length"), "4096");
  assert.equal((await chunk.arrayBuffer()).byteLength, 4096);
  assert.equal(fetched.length, 1);

  const part = await get(h, `${base}0/c/1/2`, { headers: { Range: "bytes=10-19" } });
  assert.equal(part.status, 206);
  assert.equal(part.headers.get("Content-Range"), "bytes 10-19/4096");
  assert.deepEqual(new Uint8Array(await part.arrayBuffer()), new Uint8Array(await (await get(h, `${base}0/c/1/2`)).arrayBuffer()).subarray(10, 20));

  const suffix = await get(h, `${base}0/c/1/2`, { headers: { Range: "bytes=-6" } });
  assert.equal(suffix.headers.get("Content-Range"), "bytes 4090-4095/4096");

  const head = await get(h, `${base}0/c/1/2`, { method: "HEAD" });
  assert.equal(head.status, 200);
  assert.equal(head.headers.get("Content-Length"), "4096");
  assert.equal((await head.arrayBuffer()).byteLength, 0);

  assert.equal((await get(h, `${base}0/c/9/9`)).status, 404, "absent chunk");
  assert.equal((await get(h, `${base}__vz__/sources`)).status, 404, "hidden key");
  assert.equal((await get(h, base)).status, 404, "directory");

  const download = await get(h, `${base}${ARCHIVE_KEY}`);
  assert.equal(download.headers.get("Content-Type"), "application/zip");
  assert.match(download.headers.get("Content-Disposition")!, /filename="float32_uncompressed_be.vzip"/);

  // The downloaded archive, served through the archive route, gives the same values.
  const viaArchive = await get(h, `archive/${encodeId("https://data.test/x.vzip")}/0/c/1/2`);
  assert.deepEqual(await viaArchive.arrayBuffer(), await (await get(h, `${base}0/c/1/2`)).arrayBuffer());
});

test("rejects an id that is not base64url", async () => {
  assert.equal((await get(handler(), "tiff/not*base64/zarr.json")).status, 400);
});

test("rejects methods other than GET and HEAD", async () => {
  const r = await get(handler(), `${tiff("svs_like_int16.tif")}zarr.json`, { method: "PUT", body: "x" });
  assert.equal(r.status, 405);
});

test("rejects an unsatisfiable range", async () => {
  const r = await get(handler(), `${tiff("svs_like_int16.tif")}zarr.json`, { headers: { Range: "bytes=99999-" } });
  assert.equal(r.status, 416);
  assert.match(r.headers.get("Content-Range")!, /^bytes \*\/\d+$/);
});

test("reports an unsupported TIFF", async () => {
  const r = await get(handler(), `${tiff("unsupported_predictor_float.tif")}zarr.json`);
  assert.equal(r.status, 422);
  assert.match(await r.text(), /unsupported predictor 3/);
});

test("reports a TIFF that cannot be opened", async () => {
  assert.equal((await get(handler(), `${tiff("missing.tif")}zarr.json`)).status, 502);
});

test("reports a tile that cannot be fetched", async () => {
  fs.copyFileSync(new URL("svs_like_int16.tif", FIXTURES), new URL("gone.tif", FIXTURES));
  try {
    const r = await get(handler(), `${tiff("gone.tif")}0/c/0/0`);
    assert.equal(r.status, 502);
    assert.match(await r.text(), /refkv resolution error/);
  } finally {
    fs.rmSync(new URL("gone.tif", FIXTURES));
  }
});

test("ignores paths outside the routes", async () => {
  assert.equal((await get(handler(), "other/zarr.json")).status, 404);
});

test("serves zarrified stores, and reports rejected ones", async () => {
  // A store whose listing (spec/zarrify.md §1.5
  // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>) and objects
  // come from the N5, Zarr v2 and SAFE fixtures, in the bucket "b".
  const roots = ["n5", "zarr2", "safe"].map((f) => new URL(`../../fixtures/${f}/`, import.meta.url));
  const rootOf = (rel: string) => roots.find((r) => fs.existsSync(new URL(rel.split("/")[0] + "/", r))) ?? roots[0];
  const root = roots[0];
  const file = (url: string) => {
    const rel = decodeURIComponent(new URL(url).pathname.slice("/b/".length));
    return new URL(rel, rootOf(rel));
  };
  const fetchStore = async (url: string) => {
    const u = new URL(url);
    const prefix = u.searchParams.get("prefix")!;
    const dir = new URL(prefix, rootOf(prefix));
    if (!fs.existsSync(dir)) return new Response("no such bucket", { status: 404 });
    const keys: string[] = [];
    const walk = (d: URL, rel: string) => {
      for (const e of fs.readdirSync(d, { withFileTypes: true })) {
        if (e.isDirectory()) walk(new URL(`${e.name}/`, d), `${rel}${e.name}/`);
        else keys.push(`<Contents><Key>${prefix}${rel}${e.name}</Key><Size>${fs.statSync(new URL(e.name, d)).size}</Size></Contents>`);
      }
    };
    walk(dir, "");
    return new Response(`<ListBucketResult><IsTruncated>false</IsTruncated>${keys.join("")}</ListBucketResult>`);
  };
  const h = makeHandler({
    prefix: PREFIX,
    fetchStore: async (url, init) => {
      if (new URL(url).search) return fetchStore(url);
      return new Response(fs.readFileSync(file(url)), { status: init ? 206 : 200 });
    },
    fetchRange: async (url, start, end) => {
      const body = new Uint8Array(fs.readFileSync(file(url)));
      return { data: body.subarray(start, end), size: body.length }; // the size pin is checked
    },
  });
  const base = `image/${encodeId("https://data.test/b/n5_cosem/")}/`;
  const group = await get(h, `${base}em/fibsem-uint8/zarr.json`);
  assert.equal(group.status, 200);
  assert.equal((await group.json()).attributes.ome.multiscales[0].datasets.length, 3);
  // A chunk is the whole block object.
  const chunk = await get(h, `${base}em/fibsem-uint8/s0/0/0/0`);
  assert.equal(chunk.status, 200);
  assert.equal((await chunk.arrayBuffer()).byteLength, fs.statSync(new URL("n5_cosem/em/fibsem-uint8/s0/0/0/0", root)).size);
  const rejected = await get(h, `image/${encodeId("https://data.test/b/n5_reject_compression_lz4/")}/zarr.json`);
  assert.equal(rejected.status, 422);
  assert.match(await rejected.text(), /^N5: .*lz4/);
  // the Rust core's Zarr v2 store format names itself in its rejections
  const z = await get(h, `image/${encodeId("https://data.test/b/zarr2_reject_order/")}/zarr.json`);
  assert.equal(z.status, 422);
  assert.match(await z.text(), /^Zarr v2: .*order A is not C or F/);
  // a SAFE directory's rejection is bare, as a SAFE zip file's is
  const s = await get(h, `image/${encodeId("https://data.test/b/safe_reject_bad_cs_code/")}/zarr.json`);
  assert.equal(s.status, 422);
  const st = await s.text();
  assert.ok(st.length > 0 && !/^(SAFE|store): /.test(st), st);
  const missing = await get(h, `image/${encodeId("https://data.test/b/no_such_store/")}/zarr.json`);
  assert.equal(missing.status, 422);
  assert.match(await missing.text(), /^store: the store has no listing/);
});

/** Saved archives in a Map, as Cache Storage keeps them for the service worker. */
function memoryStore(): SavedArchives & { map: Map<string, Uint8Array> } {
  const map = new Map<string, Uint8Array>();
  return {
    map,
    get: async (name) => map.get(name),
    put: async (name, bytes) => void map.set(name, bytes),
    delete: async (name) => void map.delete(name),
  };
}

/** A handler that saves archives in `saved`, reading data.test/x.tif from the fixture `state.file`. */
function savingHandler(saved: SavedArchives, state: { file: string; opens: number }) {
  const bytes = () => local(remote(state.file));
  return makeHandler({
    prefix: PREFIX,
    saved,
    openFile: async () => {
      state.opens++;
      const b = bytes();
      return { size: b.length, read: async (o, n) => b.subarray(o, o + n) };
    },
    fetchRange: async (_url, start, end) => {
      const b = bytes();
      return { data: b.subarray(start, end), size: b.length }; // the size pin is checked
    },
  });
}

const x = tiff("x.tif");
const X_NAME = `tiff/${remote("x.tif")}`;

test("serves a saved archive without zarrifying again", async () => {
  const saved = memoryStore();
  const state = { file: "float32_uncompressed_be.ome.tif", opens: 0 };
  const first = savingHandler(saved, state);
  const chunk = await (await get(first, `${x}0/c/1/2`)).arrayBuffer();
  assert.equal(state.opens, 1);
  assert.deepEqual([...saved.map.keys()], [X_NAME]);
  assert.deepEqual(new Uint8Array(await (await get(first, `${x}${ARCHIVE_KEY}`)).arrayBuffer()), saved.map.get(X_NAME));
  // a new handler, as a restarted worker has, serves the saved archive
  const second = savingHandler(saved, state);
  const again = await get(second, `${x}0/c/1/2`);
  assert.equal(again.status, 200);
  assert.deepEqual(await again.arrayBuffer(), chunk);
  assert.equal((await (await get(second, `${x}zarr.json`)).json()).attributes.ome.version, "0.5");
  assert.equal(state.opens, 1, "the input is not zarrified again");
});

test("drops a saved archive whose source changed, and zarrifies it again", async () => {
  const saved = memoryStore();
  const state = { file: "float32_uncompressed_be.ome.tif", opens: 0 };
  assert.equal((await get(savingHandler(saved, state), `${x}zarr.json`)).status, 200);
  state.file = "svs_like_int16.tif"; // a file of another size at the same URL
  const h = savingHandler(saved, state);
  const stale = await get(h, `${x}0/c/1/2`);
  assert.equal(stale.status, 502);
  assert.match(await stale.text(), /the archive pins \d+: the source changed/);
  assert.equal(saved.map.has(X_NAME), false);
  assert.equal(state.opens, 1);
  const fresh = await get(h, `${x}zarr.json`);
  assert.equal(fresh.status, 200);
  assert.equal(state.opens, 2);
  assert.equal(saved.map.has(X_NAME), true);
});

test("drops a saved archive that cannot be opened, and zarrifies it again", async () => {
  const saved = memoryStore();
  saved.map.set(X_NAME, new Uint8Array([1, 2, 3]));
  const state = { file: "float32_uncompressed_be.ome.tif", opens: 0 };
  assert.equal((await get(savingHandler(saved, state), `${x}zarr.json`)).status, 200);
  assert.equal(state.opens, 1);
  assert.ok(saved.map.get(X_NAME)!.length > 3, "the new archive replaces it");
});

test("zarrifies when the saved archives cannot be read", async () => {
  const saved = { ...memoryStore(), get: () => { throw new Error("no cache"); } };
  const state = { file: "float32_uncompressed_be.ome.tif", opens: 0 };
  assert.equal((await get(savingHandler(saved, state), `${x}zarr.json`)).status, 200);
  assert.equal(state.opens, 1);
});

test("serves an archive that cannot be saved", async () => {
  const saved = { ...memoryStore(), put: async () => { throw new Error("quota exceeded"); } };
  const state = { file: "float32_uncompressed_be.ome.tif", opens: 0 };
  const h = savingHandler(saved, state);
  assert.equal((await get(h, `${x}zarr.json`)).status, 200);
  assert.equal((await get(h, `${x}0/c/1/2`)).status, 200);
  assert.equal(saved.map.size, 0);
  assert.equal((await get(savingHandler(saved, state), `${x}zarr.json`)).status, 200);
  assert.equal(state.opens, 2, "nothing was saved, so a new handler zarrifies again");
});
