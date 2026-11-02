// The perfect hash of a phkv store (spec/bindings/phkv.md §18
// <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#18-perfect-hash>): PtrHash's
// query (parts, buckets, one byte of pilot per bucket) with XXH64 as the key hash and a slot table
// in place of PtrHash's remap. `slot` is the query the specification defines; `build` is the
// writer's way to find pilots for it, a port of rust/refkv-phkv/src/ptrhash.rs (the specification
// requires only that every key lands in a slot of its own).
//
// A 64-bit value is held as two uint32 halves in the hot paths: BigInt would make the pilot search
// (up to 256 tries per bucket) too slow for the hundreds of thousands of keys of a large image.

import { xxh64 } from "./xxh64.ts";

export interface Params {
  seed: bigint;
  parts: number;
  slots: number;
  buckets: number;
}

export const ALPHA = 0.98;
export const LAMBDA = 3;
export const PART_KEYS = 1 << 16;
export const MAX_SEEDS = 64;
/** Buckets placed most recently, which a bucket being placed must not evict. */
const RECENT = 16;
const TWO32 = 4294967296;

// the pilot multiplier 0x517CC1B727220A95 and MurmurHash3's finalizer constants, in halves
const PILOT_HI = 0x517cc1b7;
const PILOT_LO = 0x27220a95;
const C1_HI = 0xff51afd7;
const C1_LO = 0xed558ccd;
const C2_HI = 0xc4ceb9fe;
const C2_LO = 0x1a85ec53;

// the high word of the last `mul32`, and the halves of the last `fmix`
let H = 0;
let OH = 0;
let OL = 0;

/** The low word of the 64-bit product `a * b` of two uint32; its high word is left in `H`. */
function mul32(a: number, b: number): number {
  const a0 = a & 0xffff;
  const a1 = a >>> 16;
  const b0 = b & 0xffff;
  const b1 = b >>> 16;
  const mid = a0 * b1 + a1 * b0;
  const lo = a0 * b0 + (mid % 65536) * 65536;
  H = a1 * b1 + Math.floor(mid / 65536) + Math.floor(lo / TWO32);
  return lo >>> 0;
}

/** `(xh, xl) * (ch, cl)` modulo 2^64, into OH, OL. */
function mulWrap(xh: number, xl: number, ch: number, cl: number): void {
  const lo = mul32(xl, cl);
  OL = lo;
  OH = (H + Math.imul(xh, cl) + Math.imul(xl, ch)) >>> 0;
}

/** MurmurHash3's 64-bit finalizer of `(xh, xl)`, into OH, OL. */
function fmix(xh: number, xl: number): void {
  xl = (xl ^ (xh >>> 1)) >>> 0;
  mulWrap(xh, xl, C1_HI, C1_LO);
  xh = OH;
  xl = (OL ^ (xh >>> 1)) >>> 0;
  mulWrap(xh, xl, C2_HI, C2_LO);
  xh = OH;
  OL = (OL ^ (xh >>> 1)) >>> 0;
}

/** `mulhi((xh, xl), m)` for `m` below 2^32: `x` scaled from [0, 2^64) to [0, m). */
function mulhi(xh: number, xl: number, m: number): number {
  mul32(xl, m);
  const p1h = H;
  const p2l = mul32(xh, m);
  return H + (p2l + p1h >= TWO32 ? 1 : 0);
}

/** `(xh, xl) * m` modulo 2^64 for `m` below 2^32, into OH, OL. */
function mulSmallWrap(xh: number, xl: number, m: number): void {
  const lo = mul32(xl, m);
  const p1h = H;
  const p2l = mul32(xh, m);
  OL = lo;
  OH = (p1h + p2l) >>> 0;
}

// pilot × the multiplier, for each of the 256 pilots
const PILOT_HIS = new Uint32Array(256);
const PILOT_LOS = new Uint32Array(256);
for (let p = 0; p < 256; p++) {
  mulWrap(0, p, PILOT_HI, PILOT_LO);
  PILOT_HIS[p] = OH;
  PILOT_LOS[p] = OL;
}

const hi = (h: bigint) => Number(h >> 32n);
const lo = (h: bigint) => Number(h & 0xffffffffn);

/** The part of a hash, and its bucket's index among all buckets (the index of its pilot). */
function bucket(params: Params, hh: number, hl: number): number {
  const p = mulhi(hh, hl, params.parts);
  mulSmallWrap(hh, hl, params.parts);
  return p * params.buckets + mulhi(OH, OL, params.buckets);
}

/** The slot of a hash within its part, given the pilot of its bucket. */
function slotInPart(params: Params, hh: number, hl: number, pilot: number): number {
  fmix((hh ^ PILOT_HIS[pilot]) >>> 0, (hl ^ PILOT_LOS[pilot]) >>> 0);
  return mulhi(OH, OL, params.slots);
}

/** The slot of the key whose hash is `h`, among `params.parts * params.slots` (spec §18.3). */
export function slot(params: Params, pilots: Uint8Array, h: bigint): number {
  const hh = hi(h);
  const hl = lo(h);
  const p = mulhi(hh, hl, params.parts);
  const b = bucket(params, hh, hl);
  return p * params.slots + slotInPart(params, hh, hl, pilots[b]);
}

/** The XXH64 of a key's UTF-8 bytes, with the store's seed. */
export const hash = (key: Uint8Array, seed: bigint): bigint => xxh64(key, seed);

export interface Built {
  params: Params;
  pilots: Uint8Array;
  /** For each slot, the index of its key in the input, or -1. */
  slots: Int32Array;
}

/**
 * Finds a seed and pilots that place every key in a slot of its own: seeds 0, 1, 2, ... until one
 * works. Undefined if none of the first `MAX_SEEDS` does (in practice, if two keys are equal).
 */
export function build(keys: Uint8Array[]): Built | undefined {
  if (keys.length === 0) throw new Error("a store with no keys has no perfect hash");
  for (let seed = 0n; seed < BigInt(MAX_SEEDS); seed++) {
    const built = buildSeed(keys, seed);
    if (built !== undefined) return built;
  }
  return undefined;
}

function buildSeed(keys: Uint8Array[], seed: bigint): Built | undefined {
  const n = keys.length;
  const parts = Math.max(1, Math.ceil(n / PART_KEYS));
  const hs = new Uint32Array(n);
  const ls = new Uint32Array(n);
  const partOf = new Uint32Array(n);
  const counts = new Uint32Array(parts);
  for (let i = 0; i < n; i++) {
    const h = hash(keys[i], seed);
    hs[i] = hi(h);
    ls[i] = lo(h);
    partOf[i] = mulhi(hs[i], ls[i], parts);
    counts[partOf[i]]++;
  }
  let max = 0;
  for (const c of counts) max = Math.max(max, c);
  const params: Params = {
    seed,
    parts,
    slots: Math.max(1, Math.ceil(max / ALPHA)),
    buckets: Math.max(1, Math.ceil(max / LAMBDA)),
  };
  const members: number[][] = Array.from({ length: parts }, () => []);
  for (let i = 0; i < n; i++) members[partOf[i]].push(i);
  const pilots = new Uint8Array(parts * params.buckets);
  const slots = new Int32Array(parts * params.slots).fill(-1);
  for (let p = 0; p < parts; p++) {
    const placed = buildPart(params, hs, ls, members[p], p * params.buckets);
    if (placed === undefined) return undefined;
    pilots.set(placed.pilots, p * params.buckets);
    slots.set(placed.slots, p * params.slots);
  }
  return { params, pilots, slots };
}

/** A max-heap of buckets: the largest first, among equals the lowest index. */
class Queue {
  private a: number[] = [];
  private static key(size: number, index: number) {
    return size * 2 ** 24 + (2 ** 24 - 1 - index);
  }
  push(size: number, index: number) {
    const a = this.a;
    let i = a.length;
    a.push(Queue.key(size, index));
    while (i > 0) {
      const parent = (i - 1) >> 1;
      if (a[parent] >= a[i]) break;
      [a[parent], a[i]] = [a[i], a[parent]];
      i = parent;
    }
  }
  pop(): number | undefined {
    const a = this.a;
    if (a.length === 0) return undefined;
    const top = a[0];
    const last = a.pop()!;
    if (a.length > 0) {
      a[0] = last;
      let i = 0;
      for (;;) {
        const l = 2 * i + 1;
        const r = l + 1;
        let m = i;
        if (l < a.length && a[l] > a[m]) m = l;
        if (r < a.length && a[r] > a[m]) m = r;
        if (m === i) break;
        [a[m], a[i]] = [a[i], a[m]];
        i = m;
      }
    }
    return 2 ** 24 - 1 - (top % 2 ** 24);
  }
}

/**
 * Places one part's keys: buckets largest first, each with the first pilot that puts its keys in
 * free slots; when no pilot does, the pilot that evicts the fewest keys (counting a bucket of size
 * s as s²), whose evicted buckets go back in the queue.
 */
function buildPart(
  params: Params,
  hs: Uint32Array,
  ls: Uint32Array,
  members: number[],
  baseBucket: number,
): { pilots: Uint8Array; slots: Int32Array } | undefined {
  const nb = params.buckets;
  const ns = params.slots;
  if (nb >= 2 ** 24) throw new Error("a part has more buckets than the queue holds");
  const owners: number[][] = Array.from({ length: nb }, () => []);
  for (const i of members) owners[bucket(params, hs[i], ls[i]) - baseBucket].push(i);
  const queue = new Queue();
  for (let b = 0; b < nb; b++) if (owners[b].length > 0) queue.push(owners[b].length, b);
  const taken = new Int32Array(ns).fill(-1); // the bucket in each slot
  const pilots = new Uint8Array(nb);
  const recent = new Int32Array(RECENT).fill(-1);
  let recentAt = 0;
  let evictions = 0;
  const limit = 64 * members.length + 4096;
  const cand: number[] = [];

  const candidates = (b: number, pilot: number) => {
    cand.length = 0;
    for (const i of owners[b]) cand.push(slotInPart(params, hs[i], ls[i], pilot));
  };
  const hasDuplicate = () => {
    for (let x = 1; x < cand.length; x++) for (let y = 0; y < x; y++) if (cand[x] === cand[y]) return true;
    return false;
  };
  const collisions = () => [...new Set(cand.filter((s) => taken[s] !== -1).map((s) => taken[s]))];

  for (let b = queue.pop(); b !== undefined; b = queue.pop()) {
    let best: { cost: number; pilot: number } | undefined;
    let placed = false;
    for (let pilot = 0; pilot < 256; pilot++) {
      candidates(b, pilot);
      if (hasDuplicate()) continue;
      const hit = collisions();
      if (hit.length === 0) {
        for (const s of cand) taken[s] = b;
        pilots[b] = pilot;
        placed = true;
        break;
      }
      if (hit.some((o) => recent.includes(o))) continue;
      const cost = hit.reduce((c, o) => c + owners[o].length ** 2, 0);
      if (best === undefined || cost < best.cost) best = { cost, pilot };
    }
    if (!placed) {
      if (best === undefined) return undefined;
      candidates(b, best.pilot);
      for (const o of collisions()) {
        for (const i of owners[o]) taken[slotInPart(params, hs[i], ls[i], pilots[o])] = -1;
        queue.push(owners[o].length, o);
        evictions++;
      }
      if (evictions > limit) return undefined;
      for (const s of cand) taken[s] = b;
      pilots[b] = best.pilot;
    }
    recent[recentAt] = b;
    recentAt = (recentAt + 1) % RECENT;
  }
  const slots = new Int32Array(ns).fill(-1);
  for (let b = 0; b < nb; b++) {
    for (const i of owners[b]) slots[slotInPart(params, hs[i], ls[i], pilots[b])] = i;
  }
  return { pilots, slots };
}
