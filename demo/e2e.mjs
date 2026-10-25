// End-to-end check in headless Chromium: the demo page zarrifies a TIFF in
// its service worker, Neuroglancer (same origin) opens the resulting Zarr URL
// as plain zarr3 over HTTP, and the archive is downloaded. The archive is then
// hosted on another origin, with CORS, as a user would share it: the demo
// opens its URL, and its Neuroglancer link, which names the archive
// (`<archive url>|vzip:<path>|zarr3:`), opens in a browser that runs no
// service worker.
//
// Usage (after `node js/build.mjs` and building the Neuroglancer fork; see
// serve.mjs):
//   node demo/e2e.mjs <tiff url> <out dir> [wait ms]
// With BASE=<url> (e.g. the GitHub Pages site), tests that deployment instead
// of serving the local build.

import { spawn } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { createRequire } from "node:module";

// playwright is a development dependency of js/, beside this directory
const { chromium } = createRequire(new URL("../js/package.json", import.meta.url))("playwright");

const here = path.dirname(new URL(import.meta.url).pathname);

const [tiffUrl, outDir, waitMs = "20000"] = process.argv.slice(2);
fs.mkdirSync(outDir, { recursive: true });

const server = process.env.BASE ? undefined : spawn("node", [path.join(here, "serve.mjs"), "0"]);
const base = process.env.BASE ?? await new Promise((resolve) =>
  server.stdout.on("data", (d) => resolve(String(d).trim())),
);

const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 900, height: 700 }, acceptDownloads: true });
const log = [];
context.on("request", (r) => {
  const u = r.url();
  if (!u.startsWith(base) || u.includes("/vz/")) log.push(`${r.method()} ${u.replace(base, "")} ${r.headers()["range"] ?? ""}`);
});
const errors = [];
context.on("weberror", (e) => errors.push(String(e.error())));

const page = await context.newPage();
page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
await page.goto(`${base}?url=${encodeURIComponent(tiffUrl)}`);
await page.waitForSelector("#result:not([hidden])", { timeout: 60000 });
console.log("demo:", await page.textContent("#status"));
console.log("zarr url:", await page.textContent("#zarr-url"));
console.log("levels:", (await page.$$eval("#levels tr", (rows) => rows.map((r) => r.textContent))).join(" | "));
await page.screenshot({ path: path.join(outDir, "demo.png"), fullPage: true });

const download = page.waitForEvent("download");
await page.click("#download");
const file = path.join(outDir, (await download).suggestedFilename());
await (await download).saveAs(file);
console.log("downloaded:", file, fs.statSync(file).size, "bytes");

const viewer = await context.newPage();
viewer.on("console", (m) => m.type() === "error" && errors.push(m.text()));
await viewer.goto(await page.getAttribute("#open-ng", "href"));
await viewer.waitForTimeout(Number(waitMs));
await viewer.screenshot({ path: path.join(outDir, "neuroglancer.png") });
const controlled = await viewer.evaluate(() => navigator.serviceWorker.controller !== null);
console.log("neuroglancer page controlled by the service worker:", controlled);

const zarrRequests = log.filter((l) => l.split(" ")[1].startsWith("vz/"));
const tiffRequests = log.filter((l) => !l.split(" ")[1].startsWith("vz/"));
console.log(`requests to the Zarr URL: ${zarrRequests.length}, e.g.`);
for (const l of zarrRequests.slice(0, 6)) console.log("  " + l.slice(0, 160));
console.log(`requests to the TIFF (from the service worker): ${tiffRequests.length}`);
for (const l of tiffRequests.slice(0, 4)) console.log("  " + l.slice(0, 200));
// The archive, hosted on another origin: a server that allows cross-origin
// range reads but, like most, exposes no header beyond the safelisted ones.
// It listens on a loopback address, which a deployed (public) demo may not
// read (Private Network Access), so this step runs against the local build
// only.
async function checkSharedArchive() {
  const archive = fs.readFileSync(file);
  let hostReads = 0;
  const host = http.createServer((req, res) => {
    const cors = { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "Range" };
    if (req.method === "OPTIONS") return res.writeHead(204, cors).end();
    const range = req.headers.range?.match(/^bytes=(\d+)-(\d*)$/);
    if (range) {
      hostReads++;
      const [a, b] = [Number(range[1]), range[2] === "" ? archive.length - 1 : Math.min(Number(range[2]), archive.length - 1)];
      res.writeHead(206, { ...cors, "Content-Range": `bytes ${a}-${b}/${archive.length}` });
      return res.end(archive.subarray(a, b + 1));
    }
    res.writeHead(200, { ...cors, "Content-Length": String(archive.length) }).end(req.method === "HEAD" ? undefined : archive);
  });
  await new Promise((resolve) => host.listen(0, "127.0.0.1", resolve));
  try {
    const archiveUrl = `http://127.0.0.1:${host.address().port}/${path.basename(file)}`;
    await page.goto(`${base}?url=${encodeURIComponent(archiveUrl)}`);
    await page.waitForFunction(() => {
      const s = document.getElementById("status");
      return s.classList.contains("error") || s.textContent.startsWith("Ready");
    }, null, { timeout: 60000 });
    console.log("demo, hosted archive:", await page.textContent("#status"));
    console.log("neuroglancer source:", await page.textContent("#zarr-url"));
    await page.screenshot({ path: path.join(outDir, "demo-archive.png"), fullPage: true });
    const shared = await browser.newContext({ viewport: { width: 900, height: 700 }, serviceWorkers: "block" });
    const sharedErrors = [];
    shared.on("weberror", (e) => sharedErrors.push(String(e.error())));
    const anywhere = await shared.newPage();
    anywhere.on("console", (m) =>
      (m.type() === "error" || /Error retrieving chunk/.test(m.text())) && sharedErrors.push(m.text()));
    const readsBefore = hostReads;
    await anywhere.goto(await page.getAttribute("#open-ng", "href"));
    await anywhere.waitForTimeout(Number(waitMs));
    await anywhere.screenshot({ path: path.join(outDir, "neuroglancer-archive.png") });
    const sources = await anywhere.evaluate(() =>
      window.viewer.state.toJSON().layers.flatMap((l) => [l.source].flat().map((s) => s.url ?? s)));
    console.log("shared link's sources (service workers blocked):", sources.join(" "));
    const named = sources.length > 0 && sources.every((s) => s.startsWith(`${archiveUrl}|vzip:`) && s.endsWith("|zarr3:"));
    console.log("sources name the hosted archive:", named);
    // Each data source loaded, or the message it failed with (shown in the
    // layer panel, not on the console).
    const states = await anywhere.evaluate(() =>
      window.viewer.layerManager.managedLayers.flatMap((l) => l.layer?.dataSources ?? []).map((d) =>
        d.loadState === undefined ? "loading" : d.loadState.error === undefined ? "loaded" : `error: ${d.loadState.error.message}`));
    console.log("shared link's data sources:", states.join(" | "));
    const reads = hostReads - readsBefore;
    console.log("range reads of the hosted archive by Neuroglancer:", reads);
    console.log(`shared link's errors (${sharedErrors.length}):`);
    for (const e of sharedErrors.slice(0, 10)) console.log("  " + e.slice(0, 300));
    await shared.close();
    return named && reads > 0 && states.length > 0 && states.every((s) => s === "loaded") && sharedErrors.length === 0;
  } finally {
    host.close();
  }
}
const sharedOk = process.env.BASE ? true : await checkSharedArchive();
if (process.env.BASE) console.log("hosted archive: skipped (BASE is set)");

console.log(`errors (${errors.length}):`);
for (const e of errors.slice(0, 10)) console.log("  " + e.slice(0, 300));

await browser.close();
server?.kill();
process.exitCode = sharedOk ? 0 : 1;
