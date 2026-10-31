// Service worker: serves vzip archives and zarrified TIFFs under
// `<scope>vz/` as plain HTTP (see server.ts). Archives are kept in memory
// and rebuilt if the worker restarts.

import { makeHandler } from "./server.ts";

declare const self: ServiceWorkerGlobalScope;

const prefix = new URL("vz/", self.registration.scope).href;
// Chrome fails a request its worker has not answered in 5 minutes, and zarrifying a large input
// from a slow server can take longer: a request waits a minute at most, and the page asks again
// (fetchWorker), each request keeping the worker alive while the build goes on.
const handle = makeHandler({ prefix, pendingAfter: 60_000 });

self.addEventListener("install", (event) => event.waitUntil(self.skipWaiting()));
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("message", (event) => {
  if (event.data === "claim") event.waitUntil(self.clients.claim());
});
self.addEventListener("fetch", (event) => {
  if (event.request.url.startsWith(prefix)) event.respondWith(handle(event.request));
});
