// Checks the service worker's phkv route in a real browser: the worker (js/dist/vzip-sw.js, with
// the phkv reader's wasm module beside it) serves a standalone phkv store, read in place from
// another origin, as plain HTTP.
//
// The store is rust/refkv-phkv/examples/wasm_fixture.rs's, written with a relative object URL;
// the page is on 127.0.0.1 (a secure context, which a service worker needs), and the store and
// its object on fixture.test, which Chromium alone resolves to this server, since the reader
// policy refuses loopback hosts (spec/refkv.md §6.7).
//
// Usage (after `just js::build`): node demo/e2e_phkv.mjs

import { execFileSync } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { createRequire } from "node:module";

// playwright is a development dependency of js/, beside this directory
const { chromium } = createRequire(new URL("../js/package.json", import.meta.url))("playwright");

const here = path.dirname(new URL(import.meta.url).pathname);
const dist = path.join(here, "../js/dist");
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "e2e-phkv-"));
const storePath = path.join(tmp, "fixture.phkv");
execFileSync(
  "cargo",
  ["run", "--quiet", "--example", "wasm_fixture", "--manifest-path", path.join(here, "../rust/refkv-phkv/Cargo.toml"), "--", storePath, "object"],
  { stdio: "inherit" },
);
const store = fs.readFileSync(storePath);
const object = Uint8Array.from({ length: 5000 }, (_, i) => i % 256);
const big = new Uint8Array(400_000);
for (let i = 0; i < 100_000; i++) new DataView(big.buffer).setUint32(4 * i, i % 97, true);
const sha = (b) => crypto.createHash("sha256").update(b).digest("hex");

const requests = [];
const PAGE = '<!doctype html><title>phkv e2e</title>';
const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://x");
  const cors = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Range, If-Match, If-Unmodified-Since",
    "Access-Control-Expose-Headers": "Content-Range, Content-Length, ETag, Last-Modified",
  };
  if (req.method === "OPTIONS") return res.writeHead(204, cors).end();
  let body;
  let type = "application/octet-stream";
  if (url.pathname === "/sw/page.html") [body, type] = [PAGE, "text/html"];
  else if (url.pathname.startsWith("/sw/") && fs.existsSync(path.join(dist, url.pathname.slice(4)))) {
    body = fs.readFileSync(path.join(dist, url.pathname.slice(4)));
    type = url.pathname.endsWith(".wasm") ? "application/wasm" : "text/javascript";
  } else if (url.pathname === "/data/fixture.phkv") body = store;
  else if (url.pathname === "/data/object") body = object;
  if (body === undefined) return res.writeHead(404).end("not found");
  if (url.pathname.startsWith("/data/")) requests.push(`${req.method} ${url.pathname} ${req.headers.range ?? ""}`.trim());
  body = Buffer.from(body);
  const range = req.headers.range?.match(/^bytes=(\d+)-(\d+)$/);
  const headers = { ...cors, "Content-Type": type, "Accept-Ranges": "bytes" };
  if (!range) headers["Content-Length"] = String(body.length);
  if (range) {
    const [a, b] = [Number(range[1]), Math.min(Number(range[2]), body.length - 1)];
    res.writeHead(206, { ...headers, "Content-Range": `bytes ${a}-${b}/${body.length}` });
    return res.end(body.subarray(a, b + 1));
  }
  res.writeHead(200, headers).end(req.method === "HEAD" ? undefined : body);
});
await new Promise((r) => server.listen(0, "127.0.0.1", r));
const port = server.address().port;
const base = `http://127.0.0.1:${port}`;
const storeUrl = `http://fixture.test:${port}/data/fixture.phkv`;
const id = Buffer.from(storeUrl).toString("base64url");

const browser = await chromium.launch({ args: ["--host-resolver-rules=MAP fixture.test 127.0.0.1"] });
const page = await (await browser.newContext()).newPage();
page.on("console", (m) => console.log("page:", m.text()));
await page.goto(`${base}/sw/page.html`);
await page.evaluate(async () => {
  await navigator.serviceWorker.register("vzip-sw.js");
  await navigator.serviceWorker.ready;
  if (!navigator.serviceWorker.controller) await new Promise((r) => navigator.serviceWorker.addEventListener("controllerchange", r, { once: true }));
});

// every fetch below is answered by the worker, from the store and its object
const fetched = await page.evaluate(async (id) => {
  const hex = async (b) => [...new Uint8Array(await crypto.subtle.digest("SHA-256", b))].map((x) => x.toString(16).padStart(2, "0")).join("");
  const out = {};
  const one = async (name, key, init) => {
    const r = await fetch(`/sw/vz/phkv/${id}/${key}`, init);
    const b = await r.arrayBuffer();
    out[name] = { status: r.status, worker: r.headers.get("X-Vzip-Worker"), range: r.headers.get("Content-Range"), length: b.byteLength, sha: await hex(b), text: b.byteLength < 100 ? new TextDecoder().decode(b) : undefined };
  };
  await one("meta", "a/zarr.json");
  await one("zstd", "a/zstd");
  await one("deflate", "a/deflate");
  await one("ref", "a/ref");
  await one("concat", "a/concat");
  await one("window", "a/plain", { headers: { Range: "bytes=10-49" } });
  await one("missing", "no/such/key");
  return out;
}, id);
console.log(JSON.stringify(fetched, null, 1));
console.log("requests to the data origin:", requests);

const want = {
  meta: (r) => r.status === 200 && r.worker === "1" && r.text === '{"zarr_format":3}',
  zstd: (r) => r.status === 200 && r.length === 400_000 && r.sha === sha(big),
  deflate: (r) => r.status === 200 && r.sha === sha(big),
  ref: (r) => r.status === 200 && r.sha === sha(object.slice(100, 400)),
  concat: (r) => r.status === 200 && r.length === 22,
  window: (r) => r.status === 206 && r.range === "bytes 10-49/*" && r.sha === sha(big.slice(10, 50)),
  missing: (r) => r.status === 404,
};
const failed = Object.entries(want).filter(([k, f]) => !f(fetched[k])).map(([k]) => k);
console.log(failed.length === 0 ? "phkv route in the service worker: OK" : `FAILED: ${failed.join(", ")}`);
await browser.close();
server.close();
fs.rmSync(tmp, { recursive: true });
process.exitCode = failed.length === 0 ? 0 : 1;
