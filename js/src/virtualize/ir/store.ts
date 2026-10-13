// Virtualizing a store with the Rust core's store formats (rust/vzip-ir/src/store.rs;
// conventions §8.9 <https://github.com/d-v-b/vzip/blob/main/spec/conventions.md#89-stores>): the
// core parses the listing and the objects it reads, projects the convention's hierarchy and
// mirrors the store IR; this host lists the store (store.ts), gives the core the keys the
// listing ignores, performs its batches of object reads (one request per object, its spans, at
// most the store's concurrency at a time), and turns its output (`Out::encode`) into the
// writer's archive description: one url source per object some reference uses, its listed
// size pinned, in order of first use, then the data sources (spec/virtualize.md §1.4
// <https://github.com/d-v-b/vzip/blob/main/spec/virtualize.md#14-store-inputs>). The twin of
// `run_store` and `StoreIrOutput` in python/src/vzip/ir/store.py.

import type { Range, Source } from "../../protobuf.ts";
import type { ArchiveDesc, EntryDesc } from "../../writer.ts";
import { compareKeys, objectUrl, type Store, StoreError } from "../store.ts";
import { bytesEntry, decodeRaw, type RawOutput } from "./run.ts";
import { ir, type IrExports, out, put } from "./wasm.ts";

/** The core's store formats: a Zarr v2 store, a SAFE product's directory. */
export type StoreFormat = "zarr2" | "safe";
/** The profiles a store format's run decides between (its rejections name them): a Zarr v2
 * store is OME-Zarr's when its root declares OME-NGFF 0.4. */
export type StoreProfile = StoreFormat | "ome-zarr";

const utf8 = new TextEncoder();
const text = (b: Uint8Array) => new TextDecoder().decode(b);

function rejection(w: IrExports, r: number, format: StoreProfile): StoreError {
  return new StoreError(text(out(w, -r)), format);
}

/** Virtualizes the listed `store` with the core's store format `format`: the keys its listing
 * ignores given to the run (the recorded ignored keys, which the projection records, and the
 * empty `d/` keys, a SAFE directory's directories), its objects read at most
 * `options.concurrency` (default `store.concurrency`) at a time. A store the core refuses is a
 * StoreError naming the format (or, for one of the output's checks, the profile the run
 * decided: OME-Zarr's for a Zarr v2 store whose root declares OME-NGFF 0.4); an object that
 * cannot be read, its own error.
 * @param options.mirror false leaves `vzip_source`'s mirror out, and with it the source
 * metadata that records the listing's ignored keys: such an output keeps neither. */
export async function virtualizeStoreIr(
  format: StoreFormat,
  store: Store,
  options: { mirror?: boolean; concurrency?: number } = {},
): Promise<ArchiveDesc & { summary: Record<string, unknown> }> {
  const w = await ir();
  // documents read ahead (choosing the profile does): the run takes those read, and reads the
  // rest itself, so that at most `concurrency` reads are in flight
  await store.prefetch?.drain();
  const listing = [...store.objects].sort((a, b) => compareKeys(a[0], b[0]));
  const f = utf8.encode(format);
  const l = utf8.encode(JSON.stringify(listing));
  const concurrency = Math.max(1, options.concurrency ?? store.concurrency ?? 1);
  const made = Number(w.vz_store_new(put(w, f), f.length, put(w, l), l.length, concurrency));
  if (made < 0) throw rejection(w, made, format);
  const p = made;
  let live = true;
  try {
    const ig = utf8.encode(JSON.stringify([store.ignored ?? [], store.folders ?? []]));
    const g = Number(w.vz_store_ignore(p, put(w, ig), ig.length));
    if (g < 0) throw rejection(w, g, format);
    for (;;) {
      const n = Number(w.vz_store_poll(p));
      if (n < 0) throw rejection(w, n, format);
      if (n === 0) break;
      await perform(w, p, store, listing, decodeRequests(out(w, n - 1)), concurrency, format);
    }
    // the output's checks are the decided profile's (OME-Zarr's for a Zarr v2 store whose root
    // declares OME-NGFF 0.4), which its rejection names
    const said = text(out(w, Number(w.vz_store_profile(p))));
    const decided: StoreProfile = said === "ome-zarr" || said === format ? said : format;
    live = false; // vz_store_output consumes the run
    const u = utf8.encode(store.url);
    const n = Number(w.vz_store_output(p, put(w, u), u.length, options.mirror === false ? 0 : 1));
    if (n < 0) throw rejection(w, n, decided);
    const raw = decodeRaw(out(w, n));
    return { ...storeDesc(raw, new Map(listing)), summary: { ...raw.summary, listingRequests: store.requests } };
  } finally {
    if (live) w.vz_store_free(p);
  }
}

/** The requests of a poll: each one's object (an index into the listing) and spans [start, end). */
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

/** Performs a poll's requests, at most `concurrency` at a time; every request has settled
 * when this returns or throws. */
async function perform(w: IrExports, p: number, store: Store, listing: [string, number][],
                       reqs: [number, [number, number][]][], concurrency: number, format: StoreFormat): Promise<void> {
  let next = 0;
  let failure: { error: unknown } | undefined;
  const worker = async () => {
    while (failure === undefined && next < reqs.length) {
      const k = next++;
      try {
        const [object, spans] = reqs[k];
        const [key, size] = listing[object];
        const parts = spans.length === 1 && spans[0][0] === 0 && spans[0][1] === size
          ? [await whole(store, key)]
          : await Promise.all(spans.map(([a, b]) => readRange(store, key, a, b - a)));
        if (failure !== undefined) return;
        const n = parts.reduce((s, x) => s + x.length, 0);
        const ptr = w.vz_alloc(n) >>> 0;
        const mem = new Uint8Array(w.memory.buffer, ptr, n);
        let at = 0;
        for (const x of parts) {
          mem.set(x, at);
          at += x.length;
        }
        const r = Number(w.vz_store_complete(p, k, ptr, n));
        if (r < 0) throw rejection(w, r, format);
      } catch (e) {
        failure ??= { error: e };
      }
    }
  };
  await Promise.allSettled(Array.from({ length: Math.max(1, Math.min(concurrency, reqs.length)) }, worker));
  if (failure !== undefined) throw failure.error;
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

async function readRange(store: Store, key: string, offset: number, length: number): Promise<Uint8Array> {
  if (length === 0) return new Uint8Array();
  if (store.readRange) return store.readRange(key, offset, length);
  return (await store.read(key)).subarray(offset, offset + length);
}

/** A store output as the writer's archive description: one url source per object some
 * reference uses, in order of first use (the references in order of their keys, each
 * one's ranges in order), its listed size pinned, then the data sources, also in order of
 * first use (spec/virtualize.md §1.4
 * <https://github.com/d-v-b/vzip/blob/main/spec/virtualize.md#14-store-inputs>; a Zarr v2
 * store's objects are each one reference's, so this is one source per reference, in order of
 * the entries' keys). */
export function storeDesc(raw: RawOutput, sizes: Map<string, number>): ArchiveDesc {
  const refs = [...raw.refs].sort((a, b) => compareKeys(a[0], b[0]));
  const objects = new Map<string, number>();
  for (const [, parts] of refs) {
    for (const x of parts) if (x.kind === 3 && !objects.has(x.key)) objects.set(x.key, objects.size);
  }
  const data = new Map<number, number>();
  const entries: EntryDesc[] = raw.entries.map(([key, value]) => bytesEntry(key, value, raw.lazy));
  for (const [key, parts] of refs) {
    const ranges: Range[] = parts.map((x) => {
      if (x.kind === 3) return { source: objects.get(x.key)!, offset: x.offset, length: x.length };
      if (x.kind === 2) return { data: x.data };
      if (x.kind === 1) {
        if (!data.has(x.source)) data.set(x.source, objects.size + data.size);
        return { source: data.get(x.source)!, offset: x.offset, length: x.length };
      }
      throw new Error("a reference to source 0 in a store's output");
    });
    entries.push({ key, ranges });
  }
  const sources: Source[] = [...objects.keys()].map((k) => {
    const size = sizes.get(k);
    return { url: objectUrl(raw.url, k), ...(size !== undefined ? { size: BigInt(size) } : {}) };
  });
  for (const i of data.keys()) sources.push({ data: raw.data[i - 1] });
  return { sources, entries };
}
