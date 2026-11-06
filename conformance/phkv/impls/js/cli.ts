// Conformance harness CLI (conformance/phkv/HARNESS.md) for the JavaScript phkv reader and writer
// (js/src/phkv.ts). It reads only the `http` and `https` sources, so a description with a `file:`
// source is for the runner's `--no-file-sources`.
//
//   node cli.ts [--allow-private-hosts] read <store> <queries.json>
//   node cli.ts write <description.json> <out>

import fs from "node:fs";
import { pathToFileURL } from "node:url";
import { RefkvError, compareUtf8, type Policy } from "../../../../js/src/archive.ts";
import { readHttpRange } from "../../../../js/src/http.ts";
import { PhkvArchive, writePhkv } from "../../../../js/src/phkv.ts";
import type { Range, Source } from "../../../../js/src/protobuf.ts";
import { InvalidInputError, type EntryDesc } from "../../../../js/src/writer.ts";
import { decodeJsonBytes, isNum, isObj, JsonError, parseJson, type JValue } from "../../../impls/typescript/src/json.ts";

class QueryError extends Error {}

type Query =
  | { op: "classify" | "get_raw"; key: string }
  | { op: "get"; key: string; range: { start: number; end: number } | { offset: number } | { suffix: number } | undefined }
  | { op: "list"; prefix: string };

function nonNegInt(v: JValue | undefined, what: string): number {
  if (!isNum(v) || v.int === null || v.int < 0n || v.int >= 2n ** 53n) {
    throw new QueryError(`${what} must be a non-negative integer below 2^53`);
  }
  return Number(v.int);
}

function wellFormed(s: string): boolean {
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if (c >= 0xd800 && c <= 0xdbff) {
      const d = s.charCodeAt(i + 1);
      if (!(d >= 0xdc00 && d <= 0xdfff)) return false;
      i++;
    } else if (c >= 0xdc00 && c <= 0xdfff) return false;
  }
  return true;
}

function parseQuery(q: JValue, i: number): Query {
  if (!isObj(q)) throw new QueryError(`query ${i} is not an object`);
  const op = q.op;
  if (op !== "classify" && op !== "get" && op !== "get_raw" && op !== "list") throw new QueryError(`query ${i}: unknown op`);
  if (op !== "get" && "range" in q) throw new QueryError(`query ${i}: range on ${op}`);
  if (op === "list") {
    if (typeof q.prefix !== "string" || !wellFormed(q.prefix)) throw new QueryError(`query ${i}: bad prefix`);
    return { op, prefix: q.prefix };
  }
  if (typeof q.key !== "string" || !wellFormed(q.key)) throw new QueryError(`query ${i}: bad key`);
  if (op !== "get") return { op, key: q.key };
  if (!("range" in q)) return { op, key: q.key, range: undefined };
  const r = q.range;
  if (!isObj(r)) throw new QueryError(`query ${i}: range must be an object`);
  const has = (k: string) => k in r;
  const forms = [has("start") || has("end"), has("offset"), has("suffix")].filter(Boolean).length;
  if (forms !== 1) throw new QueryError(`query ${i}: range must have exactly one form`);
  if (has("start") || has("end")) {
    if (!has("start") || !has("end")) throw new QueryError(`query ${i}: partial range`);
    return { op, key: q.key, range: { start: nonNegInt(r.start, "start"), end: nonNegInt(r.end, "end") } };
  }
  if (has("offset")) return { op, key: q.key, range: { offset: nonNegInt(r.offset, "offset") } };
  return { op, key: q.key, range: { suffix: nonNegInt(r.suffix, "suffix") } };
}

/** JSON in ASCII only, so that a reader that decodes the output with a locale's encoding gets it right. */
const asciiJson = (v: unknown): string =>
  JSON.stringify(v).replace(/[\u007f-\uffff]/g, (c) => `\\u${c.charCodeAt(0).toString(16).padStart(4, "0")}`);

const hex = (b: Uint8Array | undefined) => (b === undefined ? null : Buffer.from(b).toString("hex"));

async function cmdRead(storePath: string, queriesPath: string, policy: Policy): Promise<number> {
  let queries: Query[];
  try {
    const v = parseJson(decodeJsonBytes(fs.readFileSync(queriesPath)));
    if (!Array.isArray(v)) throw new QueryError("queries file is not a JSON array");
    queries = v.map(parseQuery);
  } catch (e) {
    process.stderr.write(`invalid queries file: ${(e as Error).message}\n`);
    return 2;
  }
  let archive: PhkvArchive;
  try {
    const bytes = new Uint8Array(fs.readFileSync(storePath));
    archive = await PhkvArchive.openStore(
      bytes,
      pathToFileURL(fs.realpathSync(storePath)).href,
      (url, s, e, pins, o) => readHttpRange(url, s, e, pins, undefined, o),
      policy,
    );
  } catch (e) {
    if (e instanceof RefkvError && e.errorClass !== "archive") throw e;
    process.stdout.write(asciiJson({ open: { ok: false, class: "archive", error: (e as Error).message }, results: [] }) + "\n");
    return 0;
  }
  const results: unknown[] = [];
  for (const q of queries) {
    try {
      switch (q.op) {
        case "classify": {
          const e = archive.lookup(q.key);
          results.push({ ok: true, kind: e === undefined ? "missing" : e.reference ? "reference" : "bytes" });
          break;
        }
        case "get": {
          const r = q.range;
          if (r && "start" in r && r.start > r.end) throw new RefkvError("request", "start after end");
          const entry = archive.lookup(q.key);
          let value: Uint8Array | undefined;
          if (entry !== undefined) {
            const size = archive.size(entry);
            let a = 0;
            let b = size;
            if (r && "suffix" in r) a = Math.max(size - r.suffix, 0);
            else if (r && "offset" in r) a = Math.min(r.offset, size);
            else if (r) {
              a = Math.min(r.start, size);
              b = Math.min(r.end, size);
            }
            value = await archive.read(q.key, a, b);
          }
          results.push({ ok: true, value: hex(value) });
          break;
        }
        case "get_raw": {
          const e = archive.lookup(q.key);
          if (e === undefined) results.push({ ok: true, value: null });
          else if (e.reference) results.push({ ok: true, value: hex(e.reference.payload) });
          else results.push({ ok: true, value: hex(archive.bytes.subarray(e.bodyOffset, e.bodyOffset + e.csize)) });
          break;
        }
        case "list":
          results.push({ ok: true, keys: archive.keys().filter((k) => k.startsWith(q.prefix)).sort(compareUtf8) });
          break;
      }
    } catch (e) {
      if (e instanceof RefkvError) results.push({ ok: false, class: e.errorClass, error: e.message });
      else results.push({ ok: false, class: "internal", error: String((e as Error).stack ?? e) });
    }
  }
  process.stdout.write(asciiJson({ open: { ok: true }, results }) + "\n");
  return 0;
}

// ---------------------------------------------------------------- write

const bad = (m: string): never => {
  throw new InvalidInputError(m);
};

function hexBytes(v: JValue | undefined, what: string): Uint8Array {
  if (typeof v !== "string" || !/^(?:[0-9a-f]{2})*$/.test(v)) bad(`${what} must be a lowercase even-length hex string`);
  return new Uint8Array(Buffer.from(v as string, "hex"));
}

function int(v: JValue | undefined, what: string, allowNegative = false): bigint {
  if (!isNum(v) || v.int === null) bad(`${what} must be a JSON integer`);
  const n = (v as { int: bigint }).int;
  if (!allowNegative && n < 0n) bad(`${what} must not be negative`);
  return n;
}

function bool(v: JValue | undefined, what: string): boolean {
  if (typeof v !== "boolean") bad(`${what} must be a boolean`);
  return v as boolean;
}

function rejectNulls(v: JValue, path: string): void {
  if (v === null && path !== "") bad(`null is not allowed at ${path}`);
  if (Array.isArray(v)) v.forEach((x, i) => rejectNulls(x, `${path}[${i}]`));
  else if (isObj(v)) for (const [k, x] of Object.entries(v)) rejectNulls(x, path === "" ? k : `${path}.${k}`);
}

/** `page_size` and `inline_max` are hints (HARNESS.md): this writer's pages and inlining are its own. */
function parseDescription(v: JValue): { sources: Source[]; entries: EntryDesc[] } {
  if (!isObj(v)) bad("description must be an object");
  const d = v as { [k: string]: JValue };
  rejectNulls(d, "");
  if ("page_size" in d && int(d.page_size, "page_size") < 1n) bad("page_size must be at least 1");
  if ("inline_max" in d) int(d.inline_max, "inline_max");
  const sources: Source[] = [];
  if ("sources" in d) {
    if (!Array.isArray(d.sources)) bad("sources must be an array");
    for (const [i, s] of (d.sources as JValue[]).entries()) {
      if (!isObj(s)) bad(`source ${i} must be an object`);
      const o = s as { [k: string]: JValue };
      const kinds = ["url", "key", "data"].filter((k) => k in o);
      if (kinds.length !== 1) bad(`source ${i} must have exactly one of url, key, data`);
      const pins = ["size", "etag", "modified_not_after"].filter((k) => k in o);
      if (kinds[0] !== "url" && pins.length > 0) bad(`source ${i}: pins are only allowed on url sources`);
      if (kinds[0] === "url") {
        if (typeof o.url !== "string") bad(`source ${i}: url must be a string`);
        const src: Source = { url: o.url as string };
        if ("size" in o) src.size = int(o.size, `source ${i} size`);
        if ("etag" in o) {
          if (typeof o.etag !== "string") bad(`source ${i}: etag must be a string`);
          src.etag = o.etag as string;
        }
        if ("modified_not_after" in o) src.modifiedNotAfter = int(o.modified_not_after, `source ${i} modified_not_after`, true);
        sources.push(src);
      } else if (kinds[0] === "key") {
        if (typeof o.key !== "string") bad(`source ${i}: key must be a string`);
        sources.push({ key: o.key as string });
      } else sources.push({ data: hexBytes(o.data, `source ${i} data`) });
    }
  }
  const entries: EntryDesc[] = [];
  if ("entries" in d) {
    if (!Array.isArray(d.entries)) bad("entries must be an array");
    for (const [i, e] of (d.entries as JValue[]).entries()) {
      if (!isObj(e)) bad(`entry ${i} must be an object`);
      const o = e as { [k: string]: JValue };
      if (typeof o.key !== "string") bad(`entry ${i}: key must be a string`);
      const compress = "compress" in o ? bool(o.compress, `entry ${i} compress`) : false;
      const hasB = "bytes" in o;
      const hasR = "ranges" in o;
      if (hasB === hasR) bad(`entry ${i}: needs exactly one of bytes, ranges`);
      if (hasB) entries.push({ key: o.key as string, bytes: hexBytes(o.bytes, `entry ${i} bytes`), compress });
      else {
        if (compress) bad(`entry ${i}: a reference is never compressed`);
        if (!Array.isArray(o.ranges)) bad(`entry ${i}: ranges must be an array`);
        const ranges = (o.ranges as JValue[]).map((r, j): Range => {
          if (!isObj(r)) bad(`entry ${i} range ${j} must be an object`);
          const ro = r as { [k: string]: JValue };
          const src = ["source", "offset", "length"].some((k) => k in ro);
          if ("data" in ro) {
            if (src) bad(`entry ${i} range ${j}: mixes data with source/offset/length`);
            return { data: hexBytes(ro.data, `entry ${i} range ${j} data`) };
          }
          return {
            source: "source" in ro ? Number(int(ro.source, "source")) : 0,
            offset: "offset" in ro ? int(ro.offset, "offset") : 0n,
            length: "length" in ro ? int(ro.length, "length") : 0n,
          };
        });
        entries.push({ key: o.key as string, ranges });
      }
    }
  }
  return { sources, entries };
}

function cmdWrite(descPath: string, outPath: string): number {
  try {
    const v = parseJson(decodeJsonBytes(fs.readFileSync(descPath)));
    const bytes = writePhkv(parseDescription(v));
    fs.writeFileSync(outPath, bytes);
    return 0;
  } catch (e) {
    if (e instanceof InvalidInputError || e instanceof JsonError || e instanceof TypeError) {
      process.stderr.write(`invalid description: ${e.message}\n`);
      return 1;
    }
    process.stderr.write(`error: ${(e as Error).stack ?? e}\n`);
    return 1;
  }
}

async function main(argv: string[]): Promise<number> {
  let policy: Policy = {};
  if (argv[0] === "--allow-private-hosts") {
    policy = { allowPrivateHosts: true }; // unsafe: asked for by name
    argv = argv.slice(1);
  }
  const [cmd, a, b] = argv;
  if (cmd === "read" && a !== undefined && b !== undefined && argv.length === 3) return cmdRead(a, b, policy);
  if (cmd === "write" && a !== undefined && b !== undefined && argv.length === 3) return cmdWrite(a, b);
  process.stderr.write("usage: cli.ts [--allow-private-hosts] read <store> <queries.json> | write <description.json> <out>\n");
  return 2;
}

main(process.argv.slice(2)).then(
  (code) => {
    process.exitCode = code;
  },
  (e) => {
    process.stderr.write(`crash: ${(e as Error).stack ?? e}\n`);
    process.exitCode = 70;
  },
);
