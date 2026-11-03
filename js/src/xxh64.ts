// XXH64 (https://github.com/Cyan4973/xxHash/blob/dev/doc/xxhash_spec.md), the key hash of a phkv
// store's perfect hash (spec/bindings/phkv.md §16.1
// <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#161-notation>). BigInt
// arithmetic: it hashes each key once per seed, so it is not the hot loop (ptrhash.ts is).

const MASK = (1n << 64n) - 1n;
const P1 = 0x9e3779b185ebca87n;
const P2 = 0xc2b2ae3d27d4eb4fn;
const P3 = 0x165667b19e3779f9n;
const P4 = 0x85ebca77c2b2ae63n;
const P5 = 0x27d4eb2f165667c5n;

const rotl = (x: bigint, r: bigint) => ((x << r) | (x >> (64n - r))) & MASK;
const round = (acc: bigint, input: bigint) => (rotl((acc + input * P2) & MASK, 31n) * P1) & MASK;
const merge = (h: bigint, v: bigint) => (((h ^ round(0n, v)) * P1) & MASK) + P4 & MASK;

/** The XXH64 of `data` with `seed`, as an unsigned 64-bit integer. */
export function xxh64(data: Uint8Array, seed = 0n): bigint {
  const view = new DataView(data.buffer, data.byteOffset, data.byteLength);
  const n = data.length;
  let at = 0;
  let h: bigint;
  if (n >= 32) {
    let v1 = (seed + P1 + P2) & MASK;
    let v2 = (seed + P2) & MASK;
    let v3 = seed & MASK;
    let v4 = (seed - P1) & MASK;
    for (; at + 32 <= n; at += 32) {
      v1 = round(v1, view.getBigUint64(at, true));
      v2 = round(v2, view.getBigUint64(at + 8, true));
      v3 = round(v3, view.getBigUint64(at + 16, true));
      v4 = round(v4, view.getBigUint64(at + 24, true));
    }
    h = (rotl(v1, 1n) + rotl(v2, 7n) + rotl(v3, 12n) + rotl(v4, 18n)) & MASK;
    h = merge(h, v1);
    h = merge(h, v2);
    h = merge(h, v3);
    h = merge(h, v4);
  } else {
    h = (seed + P5) & MASK;
  }
  h = (h + BigInt(n)) & MASK;
  for (; at + 8 <= n; at += 8) {
    h ^= round(0n, view.getBigUint64(at, true));
    h = ((rotl(h, 27n) * P1) & MASK) + P4 & MASK;
  }
  if (at + 4 <= n) {
    h ^= (BigInt(view.getUint32(at, true)) * P1) & MASK;
    h = ((rotl(h, 23n) * P2) & MASK) + P3 & MASK;
    at += 4;
  }
  for (; at < n; at++) {
    h ^= (BigInt(data[at]) * P5) & MASK;
    h = (rotl(h, 11n) * P1) & MASK;
  }
  h ^= h >> 33n;
  h = (h * P2) & MASK;
  h ^= h >> 29n;
  h = (h * P3) & MASK;
  h ^= h >> 32n;
  return h;
}
