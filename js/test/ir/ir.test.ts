// The browser's host of the Rust core (src/zarrify/ir): its output against the
// frozen TypeScript zarrifiers (conformance/reference), and its http source,
// the twin of HttpTransport in python/src/refkv/ir/planner.py.

import assert from "node:assert/strict";
import fs from "node:fs";
import http from "node:http";
import type { AddressInfo } from "node:net";
import { after, before, test } from "node:test";
import { isDeepStrictEqual } from "node:util";
import { virtualizeImage as referenceImage } from "../../../conformance/zarrify/reference/ts/index.ts";
import { RefkvError } from "../../src/archive.ts";
import { HttpResolutionError } from "../../src/http.ts";
import { blockReader, ImageError } from "../../src/zarrify/common.ts";
import { irFormat, zarrifyImage, zarrifySource } from "../../src/zarrify/index.ts";
import { REGISTRY } from "../../src/zarrify/profiles.ts";
import { zarrifyIr } from "../../src/zarrify/ir/run.ts";
import { bytesSource, OPEN, openHttpSource } from "../../src/zarrify/ir/source.ts";
import { IMS_REVISION, REVISION as FROZEN_REVISION } from "../../../conformance/zarrify/reference/ts/revision.ts";
import { hierarchy } from "./compare.ts";

const FIXTURES = new URL("../../../fixtures/", import.meta.url);
const fixture = (path: string) => new Uint8Array(fs.readFileSync(new URL(path, FIXTURES)));
const ETAG = '"fixture-1"';

// ---- a local server of the fixtures: single ranges, and several ranges as
// multipart/byteranges ("multipart") or as a 200 of the whole file ("whole")

type Mode = "multipart" | "whole" | "endless" | "error";
const state = { mode: "multipart" as Mode, requests: 0, multi: 0, closedEarly: 0 };
let server: http.Server;
let base = "";

before(async () => {
  server = http.createServer((req, res) => {
    state.requests++;
    if (state.mode === "error") {
      res.writeHead(503, { "Retry-After": "0", "Content-Length": "0" }).end();
      return;
    }
    const body = fixture(decodeURIComponent(new URL(req.url!, "http://x").pathname.slice(1)));
    const spans = (req.headers.range ?? "").replace(/^bytes=/, "").split(",").map((s) => {
      const [a, b] = s.split("-").map(Number);
      return [a, Math.min(b, body.length - 1)];
    });
    if (spans.length > 1) {
      state.multi++;
      if (state.mode === "endless") {
        // a 200 that never ends: the reader must cancel it, not read it
        res.writeHead(200, { ETag: ETAG });
        const timer = setInterval(() => res.write(new Uint8Array(1 << 16)), 1);
        res.on("close", () => {
          clearInterval(timer);
          state.closedEarly++;
        });
        return;
      }
      if (state.mode === "whole") {
        res.writeHead(200, { ETag: ETAG, "Content-Length": String(body.length) }).end(body);
        return;
      }
      const boundary = "vzipboundary";
      const parts: Uint8Array[] = [];
      const enc = new TextEncoder();
      for (const [a, b] of spans) {
        parts.push(enc.encode(`\r\n--${boundary}\r\nContent-Type: application/octet-stream\r\n` +
          `Content-Range: bytes ${a}-${b}/${body.length}\r\n\r\n`), body.subarray(a, b + 1));
      }
      parts.push(enc.encode(`\r\n--${boundary}--\r\n`));
      res.writeHead(206, { ETag: ETAG, "Content-Type": `multipart/byteranges; boundary=${boundary}` });
      res.end(Buffer.concat(parts));
      return;
    }
    const [[a, b]] = spans;
    if (a >= body.length) {
      res.writeHead(416, { "Content-Range": `bytes */${body.length}` }).end();
      return;
    }
    res.writeHead(206, { ETag: ETAG, "Content-Range": `bytes ${a}-${b}/${body.length}`, "Content-Length": String(b - a + 1) });
    res.end(body.subarray(a, b + 1));
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}/`;
});
after(() => {
  server.closeAllConnections();
  server.close();
});

// The fixtures whose layout the profile changed after the freeze, which the frozen reference does not
// give (conformance/zarrify/revised.json, compare.py's REVISED).
const REVISED = new Set<string>(JSON.parse(fs.readFileSync(new URL("../../../conformance/zarrify/revised.json",
  import.meta.url), "utf8")).fixtures);
const IR_FIXTURES = ["tiff", "nd2", "czi", "nifti", "ims"].flatMap((dir) =>
  fs.readdirSync(new URL(dir, FIXTURES), { withFileTypes: true })
    .filter((e) => e.isFile() && !e.name.endsWith(".npz") && !REVISED.has(e.name))
    .map((e) => e.name).sort().map((n) => `${dir}/${n}`));

test("the Rust core gives the frozen zarrifiers' hierarchy, and rejects the same files", async () => {
  const problems: string[] = [];
  for (const path of IR_FIXTURES) {
    const bytes = fixture(path);
    const url = `https://data.test/${path}`;
    const read = async (o: number, n: number) => bytes.subarray(o, o + n);
    const [ref, got] = await Promise.allSettled([
      referenceImage(url, blockReader(read, bytes.length), bytes.length),
      zarrifyImage(url, read, bytes.length),
    ]);
    if (got.status === "fulfilled") {
      // the sniffed profile, or the one its reading chose (an Imaris file is sniffed as HDF5)
      const sniffed = await irFormat(bytes.subarray(0, 552));
      const chosenBy = REGISTRY.find((e) => e.name === got.value.format)?.chosen_by;
      assert.ok(got.value.format === sniffed || chosenBy === sniffed, `${path}: ${got.value.format}, sniffed ${sniffed}`);
    }
    if (ref.status === "rejected" || got.status === "rejected") {
      if (got.status === "rejected" && !(got.reason instanceof ImageError)) problems.push(`${path}: ${got.reason}`);
      if (ref.status !== got.status) problems.push(`${path}: the reference ${ref.status === "rejected" ? "rejects" : "accepts"} it, the core does not`);
      continue;
    }
    const [a, b] = [hierarchy(ref.value, path.startsWith("ims/") ? IMS_REVISION : FROZEN_REVISION), hierarchy(got.value)];
    for (const key of new Set([...a.keys(), ...b.keys()])) {
      if (!isDeepStrictEqual(a.get(key), b.get(key))) problems.push(`${path}: ${key} differs`);
    }
    assert.deepEqual(got.value.sources[0], { url, size: BigInt(bytes.length) });
  }
  assert.deepEqual(problems, []);
});

test("an http source gives the in-memory output, with its pins and checksums", async () => {
  // files past the first 64 KiB (the request that opens the source), and one within them
  const files = ["tiff/tczyx_uint16_deflate.ome.tif", "tiff/jpeg_ycbcr.tif", "nd2/nd2_source_frame_times.nd2", "czi/czi_many_attachments.czi"];
  const packs = new Set<string>();
  for (const mode of ["multipart", "whole"] as const) {
    for (const path of files) {
      state.mode = mode;
      const url = base + path;
      const bytes = fixture(path);
      const expected = await zarrifyImage(url, async (o, n) => bytes.subarray(o, o + n), bytes.length, undefined, { checksums: true });
      // small files are read whole by default: plan their batches, to send multi-range requests
      const source = await openHttpSource(url, { policy: { allowPrivateHosts: true }, planner: { wholeBelow: 0 } });
      const got = await zarrifySource(url, source, { checksums: true });
      assert.equal(source.size, bytes.length);
      packs.add(`${mode} ${(got.summary as { planner: { packs: unknown } }).planner.packs}`);
      assert.deepEqual(got.entries, expected.entries, `${mode} ${path}`);
      assert.deepEqual(got.sources, [{ ...expected.sources[0], etag: ETAG }, ...expected.sources.slice(1)]);
    }
  }
  // multi-range requests were sent where the server packs ranges, and not after it refused
  assert.ok(packs.has("multipart true") && packs.has("whole false") && !packs.has("multipart false"), [...packs].join());
});

test("refuses a private host without allowPrivateHosts, before any request", async () => {
  const before = state.requests;
  await assert.rejects(openHttpSource(base + "tiff/jpeg_gray.tif"),
    (e) => e instanceof RefkvError && e.errorClass === "resolution" && /loopback/.test(e.message));
  assert.equal(state.requests, before);
});

test("cancels a multi-range answer that is not multipart, and reads single ranges", async () => {
  state.mode = "endless";
  state.multi = 0;
  state.closedEarly = 0;
  const path = "tiff/tczyx_uint16_deflate.ome.tif";
  const bytes = fixture(path);
  const expected = await zarrifyIr("tiff", base + path, bytesSource(bytes));
  const source = await openHttpSource(base + path, { policy: { allowPrivateHosts: true }, planner: { wholeBelow: 0 } });
  const got = await zarrifyIr("tiff", base + path, source);
  assert.equal(state.multi, 1, "one multi-range probe");
  // the endless body was cancelled (its connection closed), else the run would not end
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(state.closedEarly, 1);
  assert.equal((got.summary as { planner: { packs: unknown } }).planner.packs, false);
  assert.deepEqual(got.entries, expected.entries);
});

test("reads a small source whole: the request that opens it, and one for the rest", async () => {
  state.mode = "multipart";
  for (const path of ["tiff/jpeg_gray.tif", "tiff/tczyx_uint16_deflate.ome.tif", "czi/czi_many_attachments.czi"]) {
    const size = fixture(path).length;
    const source = await openHttpSource(base + path, { policy: { allowPrivateHosts: true } });
    const before = state.requests;
    const got = await zarrifyIr(path.startsWith("czi") ? "czi" : "tiff", base + path, source);
    const expected = await zarrifyIr(path.startsWith("czi") ? "czi" : "tiff", base + path, bytesSource(fixture(path)));
    assert.deepEqual(got.entries, expected.entries, path);
    assert.ok(size <= 1 << 20, path);
    assert.equal(state.requests - before, size <= 1 << 16 ? 0 : 1, path);
  }
});

test("retries a server error, then fails", async () => {
  state.mode = "error";
  const before = state.requests;
  await assert.rejects(
    openHttpSource(base + "tiff/jpeg_gray.tif", { policy: { allowPrivateHosts: true }, attempts: 3, baseDelay: 0.001 }),
    (e) => e instanceof HttpResolutionError && /HTTP 503 after 3 attempts/.test(e.message),
  );
  assert.equal(state.requests - before, 3);
});

/** Runs `f` with `respond` as fetch, under Node or, with `browser`, as in a page
 * (`inBrowser` in http.ts: a `document`). */
async function withFetch(respond: (url: string, init: RequestInit) => Promise<Response>, browser: boolean, f: () => Promise<void>): Promise<void> {
  const g = globalThis as { fetch: typeof fetch; document?: unknown };
  const saved = g.fetch;
  g.fetch = respond as typeof fetch;
  if (browser) g.document = {};
  try {
    await f();
  } finally {
    g.fetch = saved;
    delete g.document;
  }
}

const NO_RETRY_DELAY = { policy: { allowPrivateHosts: true }, attempts: 2, baseDelay: 0 };

test("a request that gets no response fails with fetch's message, and names CORS as a cause in a browser", async () => {
  const url = "https://data.test/x.tif";
  let calls = 0;
  const failing = async (): Promise<Response> => {
    calls++;
    throw new TypeError("Failed to fetch");
  };
  await withFetch(failing, false, () => assert.rejects(openHttpSource(url, NO_RETRY_DELAY),
    (e) => e instanceof HttpResolutionError && e.message === `${url}: Failed to fetch`));
  assert.equal(calls, 2);
  await withFetch(failing, true, () => assert.rejects(openHttpSource(url, NO_RETRY_DELAY),
    (e) => e instanceof HttpResolutionError &&
      e.message.startsWith(`${url}: Failed to fetch. A browser reports this for a server that does not allow cross-origin reads`)));
});

test("a body cut short after a response does not name CORS as a cause", async () => {
  const url = "https://data.test/x.tif";
  const size = 2 * OPEN;
  // the opening read answers; a later range's body breaks off after its headers
  const cutShort = async (_u: string, init: RequestInit) => {
    const [a, b] = (init.headers as Record<string, string>).Range.slice(6).split("-").map(Number);
    const headers = { "Content-Range": `bytes ${a}-${b}/${size}` };
    if (a === 0) return new Response(new Uint8Array(b + 1), { status: 206, headers });
    return new Response(new ReadableStream({ pull: (c) => c.error(new TypeError("terminated")) }), { status: 206, headers });
  };
  await withFetch(cutShort, true, async () => {
    const source = await openHttpSource(url, NO_RETRY_DELAY);
    await assert.rejects(source.read(OPEN, 16), (e) => e instanceof HttpResolutionError && e.message === `${url}: terminated`);
  });
});

test("a file the core refuses is an ImageError", async () => {
  const bytes = fixture("nd2/nd2_reject_magic.nd2");
  await assert.rejects(zarrifyImage("https://data.test/x.nd2", async (o, n) => bytes.subarray(o, o + n), bytes.length),
    (e) => e instanceof ImageError && e.message.length > 0);
});
