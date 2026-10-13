// Runs a store format of the core built for wasm32 (rust/refkv-ir, `just ir-wasm`) on local
// directories from Node, as the browser's host would on a listed store: the listing in, then
// each batch of object reads (one request per object, its spans) served from the files.
// Prints one JSON line per store: the output's entries (key and SHA-256 of the bytes) and
// references, or the rejection, and the batches, requests and bytes read.
// Exits 1 when a store is rejected (each rejection still printed).
// Usage: node rust/refkv-ir/wasm/store.mjs [--wasm <vzip_ir.wasm>] [--format zarr2|n5] <dir>...
import { readFileSync, readdirSync, statSync, openSync, readSync, closeSync } from "node:fs";
import { join } from "node:path";
import { createHash } from "node:crypto";

const args = process.argv.slice(2);
let wasmPath = new URL("../target/web/vzip_ir.wasm", import.meta.url);
let format = "zarr2";
for (;;) {
  if (args[0] === "--wasm") { wasmPath = args[1]; args.splice(0, 2); continue; }
  if (args[0] === "--format") { format = args[1]; args.splice(0, 2); continue; }
  break;
}
const { instance } = await WebAssembly.instantiate(readFileSync(wasmPath), {});
const w = instance.exports;
const enc = new TextEncoder();
const text = (n) => new TextDecoder().decode(new Uint8Array(w.memory.buffer, w.vz_out(), n));
const put = (bytes) => {
  const p = w.vz_alloc(bytes.length);
  new Uint8Array(w.memory.buffer, p, bytes.length).set(bytes);
  return [p, bytes.length];
};

function walk(root, rel = "", out = []) {
  for (const e of readdirSync(join(root, rel), { withFileTypes: true })) {
    const r = rel ? `${rel}/${e.name}` : e.name;
    if (e.isDirectory()) walk(root, r, out);
    else if (e.isFile()) out.push([r, statSync(join(root, r)).size]);
  }
  return out;
}

function decode(b) {
  let at = 0;
  const dv = new DataView(b.buffer, b.byteOffset, b.byteLength);
  const u32 = () => { at += 4; return dv.getUint32(at - 4, true); };
  const u64 = () => { at += 8; return Number(dv.getBigUint64(at - 8, true)); };
  const bytes = () => { const n = u32(); at += n; return b.slice(at - n, at); };
  const str = () => new TextDecoder().decode(bytes());
  at = 4;
  str(); str();
  for (let n = u32(); n > 0; n--) str();
  for (let n = u32(); n > 0; n--) bytes();
  const entries = {};
  for (let n = u32(); n > 0; n--) { const k = str(); entries[k] = createHash("sha256").update(bytes()).digest("hex"); }
  const refs = {};
  for (let n = u32(); n > 0; n--) {
    const k = str();
    const parts = [];
    for (let m = u32(); m > 0; m--) {
      const tag = b[at++];
      if (tag !== 3) throw new Error(`part kind ${tag} in a store's output`);
      parts.push([str(), u64(), u64()]);
    }
    refs[k] = parts;
  }
  return { entries, refs };
}

for (const dir of args) {
  const listing = walk(dir).sort((a, b) => Buffer.compare(Buffer.from(a[0]), Buffer.from(b[0])));
  const [fp, fn] = put(enc.encode(format));
  const [lp, ln] = put(enc.encode(JSON.stringify(listing)));
  const t0 = performance.now();
  const r = Number(w.vz_store_new(fp, fn, lp, ln, 6));
  if (r < 0) { console.log(JSON.stringify({ dir, rejected: text(-r) })); process.exitCode = 1; continue; }
  let batches = 0, requests = 0, bytes = 0, out = null;
  for (;;) {
    const n = Number(w.vz_store_poll(r));
    if (n < 0) { out = { rejected: text(-n) }; break; }
    if (n === 0) break;
    batches++;
    const dv = new DataView(w.memory.buffer, w.vz_out(), n - 1);
    const reqs = [];
    let at = 4;
    const u32 = () => { at += 4; return dv.getUint32(at - 4, true); };
    const u64 = () => { at += 8; return Number(dv.getBigUint64(at - 8, true)); };
    for (let c = dv.getUint32(0, true); c > 0; c--) {
      const obj = u32();
      const spans = [];
      for (let ns = u32(); ns > 0; ns--) spans.push([u64(), u64()]);
      reqs.push([obj, spans]);
    }
    reqs.forEach(([obj, spans], k) => {
      const total = spans.reduce((a, [s, e]) => a + e - s, 0);
      const buf = new Uint8Array(total);
      const fd = openSync(join(dir, listing[obj][0]), "r");
      let o = 0;
      for (const [s, e] of spans) { readSync(fd, buf, o, e - s, s); o += e - s; }
      closeSync(fd);
      const [p, m] = put(buf);
      const c = Number(w.vz_store_complete(r, k, p, m));
      if (c < 0) throw new Error(text(-c));
      requests++; bytes += total;
    });
  }
  if (out === null) {
    const [up, un] = put(enc.encode("https://example.org/s/"));
    const n = Number(w.vz_store_output(r, up, un, 1));
    out = n < 0 ? { rejected: text(-n) } : decode(new Uint8Array(w.memory.buffer, w.vz_out(), n).slice());
  }
  if (out.rejected !== undefined) process.exitCode = 1;
  console.log(JSON.stringify({ dir, ...out, batches, requests, bytes, ms: Math.round(performance.now() - t0) }));
}
