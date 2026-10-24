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

// The format's name (`Format::from_name` in src/run.rs), by the file's first bytes.
function format(fd) {
  const head = new Uint8Array(348);
  readSync(fd, head, 0, 348, 0);
  const s = String.fromCharCode(...head.subarray(0, 16));
  if (String.fromCharCode(...head.subarray(128, 132)) === "DICM") return "dicom";
  if (head[0] === 0xda && head[1] === 0xce && head[2] === 0xbe && head[3] === 0x0a) return "nd2";
  if (s.startsWith("II*\0") || s.startsWith("MM\0*") || s.startsWith("II+\0") || s.startsWith("MM\0+")) return "tiff";
  if (String.fromCharCode(...head.subarray(344, 348)) === "n+1\0" || s.slice(4, 12) === "n+2\0\r\n\x1a\n") return "nifti";
  if (s.startsWith("\x89HDF\r\n\x1a\n")) return "ims";
  if (s.startsWith("PK\x03\x04")) return "safe"; // a SAFE product's zip form
  const box = (head[0] << 24 | head[1] << 16 | head[2] << 8 | head[3]) >>> 0;
  if ((box >= 8 || box === 1) && ["ftyp", "moov", "mdat", "wide", "free", "skip"].includes(s.slice(4, 8))) return "mp4";
  if (String.fromCharCode(...head.subarray(208, 211)) === "MAP") return "mrc";
  if (s.startsWith("ID3") || (head[0] === 0xff && (head[1] & 0xe0) === 0xe0 && (head[1] & 0x18) !== 0x08 && (head[1] & 0x06) !== 0
      && (head[2] & 0x0c) !== 0x0c)) return "mp3";
  if (s.startsWith("SIMPLE  =") && head[29] === 0x54) return "fits";
  return "czi";
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
