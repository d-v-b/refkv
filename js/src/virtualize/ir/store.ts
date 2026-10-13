// Virtualizing a store with the Rust core (rust/vzip-ir, `src/store.rs`; conventions §8.9
// <https://github.com/d-v-b/vzip/blob/main/spec/conventions.md#89-stores>): the core parses the
// listing and the objects it reads, projects the convention's hierarchy and mirrors the store
// IR; this host gives it the listing (store.ts's), performs its batches of object reads (one
// request per object, its spans, at most the store's concurrency at a time), and turns its
// output into the writer's archive description. The twin of `virtualize_listed` and
// `StoreIrOutput` in python/src/vzip/ir/store.py.

import type { Range, Source } from "../../protobuf.ts";
import type { ArchiveDesc, EntryDesc } from "../../writer.ts";
import { compareKeys, objectUrl, type Store } from "../store.ts";
import { bytesEntry, decodeRaw, rejection, type RawOutput } from "./run.ts";
import { ir, type IrExports, out, put } from "./wasm.ts";

const utf8 = new TextEncoder();

/** The store formats of the core a host runs. */
export type IrStoreFormat = "safe";

/** Virtualizes the listed `store` as `format`. A store the core refuses is an ImageError;
 * an object that cannot be read, its own error. */
export async function virtualizeIrStore(
  format: IrStoreFormat,
  store: Store,
  options: { mirror?: boolean; concurrency?: number } = {},
): Promise<ArchiveDesc & { summary: Record<string, unknown> }> {
  const w = await ir();
  const listing = [...store.objects].sort((a, b) => compareKeys(a[0], b[0]));
  const concurrency = Math.max(1, options.concurrency ?? Math.min(store.concurrency ?? 6, 6));
  const f = utf8.encode(format);
  const l = utf8.encode(JSON.stringify(listing));
  const made = Number(w.vz_store_new(put(w, f), f.length, put(w, l), l.length, concurrency));
  if (made < 0) throw rejection(w, made);
  const p = made;
  let live = true;
  try {
    const ig = utf8.encode(JSON.stringify([store.folders ?? [], store.ignored ?? []]));
    const r = Number(w.vz_store_ignored(p, put(w, ig), ig.length));
    if (r < 0) throw rejection(w, r);
    for (;;) {
      const n = Number(w.vz_store_poll(p));
      if (n < 0) throw rejection(w, n);
      if (n === 0) break;
      await perform(w, p, store, listing, decodeRequests(out(w, n - 1)), concurrency);
    }
    live = false; // vz_store_output consumes the run
    const u = utf8.encode(store.url);
    const n = Number(w.vz_store_output(p, put(w, u), u.length, options.mirror === false ? 0 : 1));
    if (n < 0) throw rejection(w, n);
    const raw = decodeRaw(out(w, n));
    return { ...storeDesc(raw, new Map(listing)), summary: { ...raw.summary, listingRequests: store.requests } };
  } finally {
    if (live) w.vz_store_free(p);
  }
}

/** The requests of a poll: each one's object (an index into the listing) and spans. */
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
                       reqs: [number, [number, number][]][], concurrency: number): Promise<void> {
  let next = 0;
  let failure: { error: unknown } | undefined;
  const worker = async () => {
    while (failure === undefined && next < reqs.length) {
      const k = next++;
      try {
        const [object, spans] = reqs[k];
        const [key, size] = listing[object];
        const parts = spans.length === 1 && spans[0][0] === 0 && spans[0][1] === size
          ? [await store.read(key)]
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
        if (r < 0) throw rejection(w, r);
      } catch (e) {
        failure ??= { error: e };
      }
    }
  };
  await Promise.allSettled(Array.from({ length: Math.max(1, Math.min(concurrency, reqs.length)) }, worker));
  if (failure !== undefined) throw failure.error;
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
 * <https://github.com/d-v-b/vzip/blob/main/spec/virtualize.md#14-store-inputs>). */
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
