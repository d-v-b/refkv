// Virtualizing a store with the Rust core's store formats (rust/vzip-ir/src/store.rs;
// conventions §8.9 <https://github.com/d-v-b/vzip/blob/main/spec/conventions.md#89-stores>):
// the store is listed by this host (store.ts), the run in WebAssembly asks for batches of
// object reads (one request per object, its spans), and its output (`Out::encode`) becomes
// the writer's archive description, every object referenced through its own url source, its
// size pinned, in the order of the entries' keys (spec/virtualize.md §1.4
// <https://github.com/d-v-b/vzip/blob/main/spec/virtualize.md#14-store-inputs>). The twin of
// `run_store` in python/src/vzip/ir/store.py.

import type { Range, Source } from "../../protobuf.ts";
import type { ArchiveDesc } from "../../writer.ts";
import { compareKeys, objectUrl, type Store, StoreError } from "../store.ts";
import { decodeOutput } from "./run.ts";
import { ir, type IrExports, out, put } from "./wasm.ts";

/** The core's store formats. */
export type StoreFormat = "zarr2";

/** The store run exports of rust/vzip-ir/src/wasm.rs. */
interface StoreExports extends IrExports {
  vz_store_new(format: number, nf: number, listing: number, nl: number, concurrency: number): bigint;
  vz_store_ignore(p: number, json: number, n: number): bigint;
  vz_store_poll(p: number): bigint;
  vz_store_complete(p: number, k: number, data: number, n: number): bigint;
  vz_store_output(p: number, url: number, n: number, mirror: number): bigint;
  vz_store_free(p: number): void;
}

const utf8 = new TextEncoder();
const text = (b: Uint8Array) => new TextDecoder().decode(b);

function rejection(w: IrExports, r: number, format: StoreFormat): StoreError {
  return new StoreError(text(out(w, -r)), format);
}

/** The requests of a poll: each one's object (an index of the listing) and spans [start, end). */
function decodeRequests(b: Uint8Array): [number, [number, number][]][] {
  const v = new DataView(b.buffer, b.byteOffset, b.byteLength);
  let at = 0;
  const u32 = () => ((at += 4), v.getUint32(at - 4, true));
  const u64 = () => ((at += 8), Number(v.getBigUint64(at - 8, true)));
  const reqs: [number, [number, number][]][] = [];
  for (let k = u32(); k > 0; k--) {
    const object = u32();
    const spans: [number, number][] = [];
    for (let n = u32(); n > 0; n--) spans.push([u64(), u64()]);
    reqs.push([object, spans]);
  }
  return reqs;
}

/** The whole object `key`, from what the store read already when it has it (a document kept,
 * or read ahead, as choosing the profile does), else read now: no document is read twice. */
async function whole(store: Store, key: string): Promise<Uint8Array> {
  const kept = store.kept?.get(key);
  store.kept?.delete(key);
  const r = kept ?? store.prefetch?.take(key);
  if (r === undefined) return store.read(key);
  const s = await r;
  if (!s.ok) throw s.error;
  return s.value;
}

async function fetchSpans(store: Store, key: string, spans: [number, number][]): Promise<Uint8Array[]> {
  const size = store.objects.get(key)!;
  if (spans.length === 1 && spans[0][0] === 0 && spans[0][1] === size) return [await whole(store, key)];
  if (!store.readRange) {
    const all = await store.read(key); // a store without range reads: the object once
    return spans.map(([a, e]) => all.subarray(a, e));
  }
  return Promise.all(spans.map(([a, e]) => store.readRange!(key, a, e - a)));
}

/** Virtualizes the listed `store` with the core's store format `format`: its ignored keys
 * recorded, its objects read at most `store.concurrency` at a time. A store the core refuses
 * is a StoreError; an object that cannot be read, its own error. */
export async function virtualizeStoreIr(
  format: StoreFormat,
  store: Store,
  options: { mirror?: boolean } = {},
): Promise<ArchiveDesc & { summary: Record<string, unknown> }> {
  const w = (await ir()) as StoreExports;
  // documents read ahead (choosing the profile does): the run takes those read, and reads the
  // rest itself, so that at most `store.concurrency` reads are in flight
  await store.prefetch?.drain();
  const listing = [...store.objects].sort((a, b) => compareKeys(a[0], b[0]));
  const f = utf8.encode(format);
  const l = utf8.encode(JSON.stringify(listing));
  const concurrency = Math.max(1, store.concurrency ?? 1);
  const made = Number(w.vz_store_new(put(w, f), f.length, put(w, l), l.length, concurrency));
  if (made < 0) throw rejection(w, made, format);
  const p = made;
  let live = true;
  try {
    const ig = utf8.encode(JSON.stringify(store.ignored ?? []));
    const g = Number(w.vz_store_ignore(p, put(w, ig), ig.length));
    if (g < 0) throw rejection(w, g, format);
    for (;;) {
      const r = Number(w.vz_store_poll(p));
      if (r < 0) throw rejection(w, r, format);
      if (r === 0) break;
      const reqs = decodeRequests(out(w, r - 1));
      const got: (Uint8Array[] | undefined)[] = new Array(reqs.length);
      let next = 0;
      const worker = async () => {
        while (next < reqs.length) {
          const k = next++;
          got[k] = await fetchSpans(store, listing[reqs[k][0]][0], reqs[k][1]);
        }
      };
      await Promise.all(Array.from({ length: Math.min(concurrency, reqs.length) }, worker));
      for (let k = 0; k < reqs.length; k++) {
        const parts = got[k]!;
        const n = parts.reduce((s, x) => s + x.length, 0);
        const ptr = w.vz_alloc(n) >>> 0;
        const mem = new Uint8Array(w.memory.buffer, ptr, n);
        let at = 0;
        for (const x of parts) {
          mem.set(x, at);
          at += x.length;
        }
        const c = Number(w.vz_store_complete(p, k, ptr, n));
        if (c < 0) throw rejection(w, c, format);
      }
    }
    live = false; // vz_store_output consumes the run
    const u = utf8.encode(store.url);
    const n = Number(w.vz_store_output(p, put(w, u), u.length, options.mirror === false ? 0 : 1));
    if (n < 0) throw rejection(w, n, format);
    // each reference to an object is a url source of its own, in the order of the entries' keys
    const refs: [string, string, bigint, Range][] = [];
    const desc = decodeOutput(out(w, n), (entry, key, offset, length) => {
      const range: Range = { source: 0, offset, length };
      refs.push([entry, key, length, range]);
      return range;
    });
    if (desc.sources.length !== 1) throw new Error("a store run's output with data sources");
    const refRanges = new Set(refs.map((r) => r[3]));
    for (const e of desc.entries) {
      if ("ranges" in e && e.ranges.some((r) => !refRanges.has(r))) throw new Error(`${e.key}: a store run's output with a range of no object`);
    }
    refs.sort((a, b) => compareKeys(a[0], b[0]));
    const sources: Source[] = refs.map(([, key, , range], i) => {
      (range as { source: number }).source = i;
      return { url: objectUrl(store.url, key), size: BigInt(store.objects.get(key)!) };
    });
    desc.summary.listingRequests = store.requests;
    return { ...desc, sources };
  } finally {
    if (live) w.vz_store_free(p);
  }
}
