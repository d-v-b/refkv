// The page's side of the service worker (client.ts).

import assert from "node:assert/strict";
import { test } from "node:test";

import { fetchWorker } from "../src/client.ts";
import { PENDING_HEADER } from "../src/server.ts";

test("fetchWorker asks again while the worker answers pending, and returns any other answer", async () => {
  const pending = () => new Response("still zarrifying", { status: 503, headers: { [PENDING_HEADER]: "1" } });
  const answers = [pending(), pending(), new Response("busy", { status: 503 })];
  const asked: string[] = [];
  const real = globalThis.fetch;
  globalThis.fetch = async (url) => {
    asked.push(String(url));
    return answers[asked.length - 1];
  };
  try {
    const r = await fetchWorker("https://viewer.test/vz/a/zarr.json");
    assert.equal(r.status, 503);
    assert.equal(await r.text(), "busy");
    assert.deepEqual(asked, Array(3).fill("https://viewer.test/vz/a/zarr.json"));
  } finally {
    globalThis.fetch = real;
  }
});
