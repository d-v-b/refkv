// Virtualizes local image files and directories (as stores) with the browser
// code, under Node, and prints every output's zarr.json documents:
//   node js/conformance/documents.ts < inputs.json
// The input is a JSON array of [file or directory, source url] pairs (for a
// directory, the url is the store URL it is served from, ending in "/"). The
// output is a JSON array with, for each input in order, {"format", "docs"}
// (docs maps each zarr.json key to its document) or {"rejected": message}.
// Used by python/tests/test_virtualize_conventions.py.

import fs from "node:fs";
import { blockReader } from "../src/virtualize/common.ts";
import { virtualizeImage, virtualizeStore } from "../src/virtualize/index.ts";
import { readerSource } from "../src/virtualize/ir/source.ts";
import { keepIntegers } from "../src/virtualize/store.ts";
import { directoryStore } from "./directory_store.ts";

const REJECTIONS = /^(TiffError|Nd2Error|LVError|DicomError|NiftiError|ImsError|ImageError|StoreError|N5Error|Zarr2Error|OmeZarrError|SafeError|CziError)$/;
const inputs: [string, string][] = JSON.parse(fs.readFileSync(0, "utf8"));
const decoder = new TextDecoder();
// JSON.rawJSON (Node 21+; not yet in TypeScript's lib).
const RawJSON = JSON as unknown as { rawJSON(text: string): object };
/** Integers exact (`keepIntegers`), and a number written with a fraction or an exponent kept as
 * written, so that a binary64 value of 2^53 or more (which JSON.stringify writes as an integer
 * literal) reads as that binary64 value, not as an integer. */
function exact(key: string, value: unknown, context?: { source?: string }): unknown {
  const kept = keepIntegers(key, value, context);
  return typeof kept === "number" && Number.isFinite(kept) && Math.abs(kept) > Number.MAX_SAFE_INTEGER
    ? RawJSON.rawJSON(context!.source!)
    : kept;
}
const results: unknown[] = [];
for (const [path, url] of inputs) {
  try {
    let virtual;
    if (fs.statSync(path).isDirectory()) {
      virtual = await virtualizeStore(directoryStore(path, url));
    } else {
      const bytes = new Uint8Array(fs.readFileSync(path));
      // a VSI file's tile files lie beside it: opened from the directory as the run asks
      const open = async (key: string) => {
        const p = `${path.slice(0, path.lastIndexOf("/") + 1)}${key}`;
        if (!fs.existsSync(p)) return undefined;
        const b = new Uint8Array(fs.readFileSync(p));
        return readerSource(async (o, n) => b.subarray(o, o + n), b.length);
      };
      virtual = await virtualizeImage(url, blockReader(async (o, n) => bytes.subarray(o, o + n), bytes.length), bytes.length, undefined, { open });
    }
    const docs: { [k: string]: unknown } = {};
    for (const e of virtual.entries) {
      if (e.key.endsWith("zarr.json") && "bytes" in e) docs[e.key] = JSON.parse(decoder.decode(e.bytes), exact as never); // numbers exact
    }
    results.push({ format: virtual.format, docs });
  } catch (e) {
    if (!(e instanceof Error) || !REJECTIONS.test(e.constructor.name)) throw e;
    results.push({ rejected: `${e.constructor.name}: ${e.message}` });
  }
}
process.stdout.write(JSON.stringify(results));
