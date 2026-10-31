// Service worker: serves vzip archives and zarrified inputs under `<scope>vz/` as plain HTTP
// (see server.ts). The archives it zarrifies are saved in Cache Storage, so that a restarted
// worker (Chrome stops one idle for 30 s) serves them without zarrifying again.
//
// A saved archive is served until a read finds that its source changed: the archive pins each
// source's size, and its ETag where the server showed one, and these are checked as values are
// read (a server that hides the ETag cross-origin leaves only the size). Such a read answers 502
// and drops the archive (`saved` in server.ts), and the next request zarrifies the input again.
// Nothing else expires a saved archive, but the cache is named for the worker's build
// (`__REFKV_BUILD__`, the refkv commit): a worker of a new build deletes the older builds' caches
// when it activates, as its zarrify rules may differ. Each build keeps the MAX_ARCHIVES archives
// saved last, at most MAX_BYTES in all.

import { encodeId, makeHandler, type SavedArchives } from "./server.ts";

declare const self: ServiceWorkerGlobalScope;
declare const __REFKV_BUILD__: string;

const CACHE_PREFIX = "refkv-archives-";
const CACHE = CACHE_PREFIX + __REFKV_BUILD__;
const MAX_ARCHIVES = 16;
const MAX_BYTES = 2 ** 30;

/** The synthetic URL an archive is saved under (never fetched). */
const savedUrl = (name: string) => new URL(`refkv-archives/${encodeId(name)}`, self.registration.scope).href;

const saved: SavedArchives = {
  async get(name) {
    const r = await (await caches.open(CACHE)).match(savedUrl(name));
    return r && new Uint8Array(await r.arrayBuffer());
  },
  async put(name, bytes) {
    const cache = await caches.open(CACHE);
    await cache.put(
      savedUrl(name),
      new Response(bytes as BodyInit, { headers: { "Content-Length": String(bytes.length) } }),
    );
    // A cache lists its requests in the order they were put, so the newest is last.
    const keys = [...(await cache.keys())].reverse();
    let total = 0;
    for (const [i, key] of keys.entries()) {
      total += Number((await cache.match(key))?.headers.get("Content-Length") ?? 0);
      if (i >= MAX_ARCHIVES || total > MAX_BYTES) await cache.delete(key);
    }
  },
  async delete(name) {
    await (await caches.open(CACHE)).delete(savedUrl(name));
  },
};

const prefix = new URL("vz/", self.registration.scope).href;
const handle = makeHandler({ prefix, saved });

async function dropOlderBuilds() {
  for (const name of await caches.keys()) {
    if (name.startsWith(CACHE_PREFIX) && name !== CACHE) await caches.delete(name);
  }
}

self.addEventListener("install", (event) => event.waitUntil(self.skipWaiting()));
self.addEventListener("activate", (event) => event.waitUntil(Promise.all([self.clients.claim(), dropOlderBuilds()])));
self.addEventListener("message", (event) => {
  if (event.data === "claim") event.waitUntil(self.clients.claim());
});
self.addEventListener("fetch", (event) => {
  if (event.request.url.startsWith(prefix)) event.respondWith(handle(event.request));
});
