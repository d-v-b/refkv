// Runs the parsers built for wasm32 (rust/refkv-ir, `just ir-wasm`) on local files
// from Node: the host reads the ranges each step asks for and feeds them. Prints
// one JSON line per file: the IR's element and run counts, the checker's verdict,
// its digest and the facts (or the rejection), and the rounds, ranges and bytes read.
// Usage: node rust/refkv-ir/wasm/run.mjs [--wasm <refkv_ir.wasm>] <file>...
import { openSync, readSync, fstatSync, readFileSync, closeSync } from "node:fs";

const args = process.argv.slice(2);
let wasmPath = new URL("../target/web/refkv_ir.wasm", import.meta.url);
if (args[0] === "--wasm") { wasmPath = args[1]; args.splice(0, 2); }
const { instance } = await WebAssembly.instantiate(readFileSync(wasmPath), {});
const w = instance.exports;
const text = (n) => new TextDecoder().decode(new Uint8Array(w.memory.buffer, w.vz_out(), n));

// The format's name, by the file's first bytes (`vz_sniff`, the core's `Format::sniff`); a file
// of no format is tried as CZI, whose parser rejects it.
function format(fd) {
  const head = new Uint8Array(552);
  const n = readSync(fd, head, 0, 552, 0);
  const r = Number(w.vz_sniff(put(head.subarray(0, n)), n));
  return r > 0 ? text(r) : "czi";
}

function put(bytes) {
  const ptr = w.vz_alloc(bytes.length);
  new Uint8Array(w.memory.buffer, ptr, bytes.length).set(bytes);
  return ptr;
}

for (const file of args) {
  const fd = openSync(file, "r");
  const size = fstatSync(fd).size;
  const t0 = performance.now();
  const name = new TextEncoder().encode(format(fd));
  const p = w.vz_new(put(name), name.length, BigInt(size));
  if (p === 0) throw new Error(`${file}: no parser for ${format(fd)}`);
  let rounds = 0, ranges = 0, bytes = 0, out = null;
  for (;;) {
    const r = Number(w.vz_step(p));
    if (r < 0) { out = { rejected: text(-r) }; break; }
    if (r === 0) break;
    const n = r - 1;
    rounds++;
    const view = new DataView(w.memory.buffer, w.vz_out(), n);
    const batch = [];
    for (let k = 0; k < n; k += 16) batch.push([view.getBigUint64(k, true), view.getBigUint64(k + 8, true)]);
    for (const [o, len] of batch) {
      const m = Number(len);
      const ptr = w.vz_alloc(m);
      readSync(fd, new Uint8Array(w.memory.buffer, ptr, m), 0, m, Number(o));
      w.vz_feed(p, o, ptr, m);
      ranges++; bytes += m;
    }
  }
  if (out === null) {
    const n = Number(w.vz_finish(p));
    out = n < 0 ? { rejected: text(-n) } : JSON.parse(text(n));
  }
  closeSync(fd);
  console.log(JSON.stringify({ file, ...out, rounds, ranges, bytes, ms: Math.round(performance.now() - t0) }));
}
