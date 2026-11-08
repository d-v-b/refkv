import fs from "node:fs";
import assert from "node:assert/strict";
import { test } from "node:test";
import { Archive, RefkvError } from "../src/archive.ts";
import { crc32c } from "../src/deflate.ts";
import { isPhkv, openArchive, PhkvArchive, writePhkv } from "../src/phkv.ts";
import { build, hash, slot } from "../src/ptrhash.ts";
import { type ArchiveDesc, InvalidInputError, writeVzip } from "../src/writer.ts";
import { xxh64 } from "../src/xxh64.ts";

const utf8 = (s: string) => new TextEncoder().encode(s);
const text = (b: Uint8Array | undefined) => new TextDecoder().decode(b);
const remote = utf8("0123456789abcdefghij");
const fetchRange = async (url: string, start: number, end: number) => {
  assert.equal(url, "https://data.test/dir/blob.bin");
  return { data: remote.subarray(start, end), size: remote.length };
};
const open = (desc: ArchiveDesc) => PhkvArchive.openStore(writePhkv(desc), "https://data.test/dir/store.phkv", fetchRange);

// The test vectors of spec/bindings/phkv.md Appendix C.

test("XXH64 matches the specification's vectors", () => {
  assert.equal(xxh64(utf8(""), 0n), 0xef46db3751d8e999n);
  assert.equal(xxh64(utf8("abc"), 0n), 0x44bc2cf5ad770999n);
  assert.equal(xxh64(utf8("zarr.json"), 1n), 0xb7c230675ab7ba48n);
  // keys of 32 bytes or more take the four-lane path; values from python-xxhash
  assert.equal(xxh64(utf8("0123456789abcdef0123456789abcdef0123456789"), 7n), 0x9cdb6129259b938en);
  assert.equal(xxh64(utf8("x".repeat(100)), 0n), 0x92f0de5a88a3c094n);
  assert.equal(xxh64(utf8("a/b/c/".repeat(9)), 3n), 0x94ba6e47a7f46721n);
});

test("the query matches the specification's vectors", () => {
  const params = { seed: 0n, parts: 1, slots: 6, buckets: 2 };
  const pilots = Uint8Array.of(1, 3);
  for (const [key, want, h] of [
    ["zarr.json", 2, 0xabe4eb424f90bc87n],
    ["a/zarr.json", 3, 0xd620761f49f4923cn],
    ["a/c/0", 4, 0xe9d3cd35d106a3ddn],
    ["a/c/1", 1, 0xaf20bbfedc2fc6a0n],
    ["b", 5, 0x78452aa11af39f9bn],
  ] as const) {
    assert.equal(hash(utf8(key), 0n), h, key);
    assert.equal(slot(params, pilots, h), want, key);
  }
});

test("the builder finds the parameters the reference writer finds for the vectors' keys", () => {
  const keys = ["zarr.json", "a/zarr.json", "a/c/0", "a/c/1", "b"].map(utf8);
  const built = build(keys)!;
  assert.deepEqual(built.params, { seed: 0n, parts: 1, slots: 6, buckets: 2 });
  assert.deepEqual([...built.pilots], [1, 3]);
  assert.deepEqual([...built.slots], [-1, 3, 0, 1, 2, 4]);
});

test("the builder places every key in a slot of its own, at every size", () => {
  for (const n of [1, 2, 3, 10, 100, 1000, 10_000, 70_000]) {
    const keys = Array.from({ length: n }, (_, i) =>
      utf8(`arr/c/${Math.floor(i / 10000)}/${Math.floor(i / 100) % 100}/${i % 100}`));
    const built = build(keys)!;
    assert.ok(built, `${n} keys`);
    const seen = new Set<number>();
    keys.forEach((k, i) => {
      const s = slot(built.params, built.pilots, hash(k, built.params.seed));
      assert.ok(!seen.has(s), `two keys in slot ${s}`);
      seen.add(s);
      assert.equal(built.slots[s], i);
    });
  }
});

test("the builder fails for equal keys", () => {
  assert.equal(build([utf8("a"), utf8("a")]), undefined);
});

test("reads back what the writer wrote", async () => {
  const big = new Uint8Array(10_000).map((_, i) => i % 251);
  const a = await open({
    sources: [{ url: "blob.bin" }, { key: "hdr" }, { data: utf8("DATA") }],
    entries: [
      { key: "plain", bytes: utf8("hello") },
      { key: "big", bytes: big },
      { key: "hdr", bytes: utf8("HEADER") },
      { key: "url", ranges: [{ source: 0, offset: 10n, length: 5n }] },
      {
        key: "concat",
        ranges: [
          { source: 1, offset: 0n, length: 3n },
          { data: utf8("-") },
          { source: 2, offset: 1n, length: 2n },
          { source: 0, offset: 0n, length: 2n },
        ],
      },
      { key: "empty", ranges: [] },
      { key: "ключ/键", bytes: utf8("unicode") },
    ],
  });
  assert.equal(text(await a.read("plain")), "hello");
  assert.deepEqual(await a.read("big"), big);
  assert.deepEqual(await a.read("big", 5000, 5010), big.subarray(5000, 5010));
  assert.equal(text(await a.read("url")), "abcde");
  assert.equal(text(await a.read("concat")), "HEA-AT01");
  assert.equal(text(await a.read("concat", 2, 6)), "A-AT");
  assert.equal(text(await a.read("empty")), "");
  assert.equal(text(await a.read("ключ/键")), "unicode");
  assert.equal(text(await a.read("hdr")), "HEADER", "a standalone store reserves no names");
  assert.equal(await a.read("absent"), undefined);
  assert.equal(await a.read(""), undefined);
  assert.deepEqual(a.keys(), ["big", "concat", "empty", "hdr", "plain", "url", "ключ/键"]);
  assert.equal(a.size(a.lookup("big")!), 10_000);
});

test("lists the keys of a store with many pages, in UTF-8 order", async () => {
  const entries = Array.from({ length: 3000 }, (_, i) => ({ key: `k/${i}`, bytes: utf8(`v${i}`) }));
  const a = await open({ sources: [], entries });
  const want = entries.map((e) => e.key).sort((x, y) => Buffer.compare(Buffer.from(x), Buffer.from(y)));
  assert.deepEqual(a.keys(), want);
  for (const i of [0, 1, 1500, 2999]) assert.equal(text(await a.read(`k/${i}`)), `v${i}`);
  assert.equal(await a.read("k/3000"), undefined);
});

test("an empty store opens and has no keys", async () => {
  const a = await open({ sources: [], entries: [] });
  assert.deepEqual(a.keys(), []);
  assert.equal(await a.read("x"), undefined);
});

test("serves the same values as a vzip archive of the same input", async () => {
  const desc: ArchiveDesc = {
    sources: [{ url: "blob.bin", size: 20n }],
    entries: [
      { key: "zarr.json", bytes: utf8('{"zarr_format":3}') },
      ...Array.from({ length: 200 }, (_, i) => ({
        key: `c/${i}`,
        ranges: [{ source: 0, offset: BigInt(i % 15), length: 5n }],
      })),
    ],
  };
  const vzip = await Archive.open(await writeVzip(desc), "https://data.test/dir/a.vzip", fetchRange);
  const phkv = await open(desc);
  assert.deepEqual(phkv.keys(), vzip.keys());
  for (const k of vzip.keys()) assert.deepEqual(await phkv.read(k), await vzip.read(k), k);
});

test("openArchive tells a phkv store from a vzip archive", async () => {
  const desc: ArchiveDesc = { sources: [], entries: [{ key: "a", bytes: utf8("1") }] };
  const store = writePhkv(desc);
  const zip = await writeVzip(desc);
  assert.ok(isPhkv(store));
  assert.ok(!isPhkv(zip));
  assert.ok((await openArchive(store, "https://x/")) instanceof PhkvArchive);
  assert.ok(!((await openArchive(zip, "https://x/")) instanceof PhkvArchive));
  await assert.rejects(openArchive(utf8("neither of them, really"), "https://x/"), (e) =>
    e instanceof RefkvError && e.errorClass === "archive");
});

test("a standalone store may carry bytes after its superblock, at a probe distance", () => {
  const store = writePhkv({ sources: [], entries: [{ key: "a", bytes: utf8("1") }] });
  assert.ok(!isPhkv(Uint8Array.from([...store, 1, 2, 3])), "three stray bytes are not a probe distance");
});

test("rejects a store that is damaged", async () => {
  const good = writePhkv({ sources: [], entries: [{ key: "a", bytes: utf8("1") }, { key: "b", bytes: utf8("2") }] });
  const open_ = (b: Uint8Array) => PhkvArchive.openStore(b, "https://x/");
  const archiveError = (e: unknown) => e instanceof RefkvError && e.errorClass === "archive";
  // the directory's crc32c
  const dirOffset = Number(new DataView(good.buffer).getBigUint64(good.length - 64 + 24, true));
  const badDir = good.slice();
  badDir[dirOffset + 10] ^= 1;
  await assert.rejects(open_(badDir), archiveError);
  // the superblock's
  const badSuper = good.slice();
  badSuper[good.length - 64 + 16] ^= 1;
  await assert.rejects(open_(badSuper), archiveError);
  // a version it does not know (with the superblock's crc32c made good)
  const v1 = good.slice();
  v1[good.length - 64 + 4] = 1;
  new DataView(v1.buffer).setUint32(v1.length - 4, crc32c(v1.subarray(v1.length - 64, v1.length - 4)), true);
  await assert.rejects(open_(v1), /unsupported phkv version 1/);
});

test("a value whose bytes changed fails its crc32c", async () => {
  const bytes = writePhkv({ sources: [], entries: [{ key: "a", bytes: new Uint8Array(5000).fill(7) }] });
  bytes[100] ^= 1; // inside the stored value, which comes first
  const a = await PhkvArchive.openStore(bytes, "https://x/");
  await assert.rejects(a.read("a"), (e) => e instanceof RefkvError && e.errorClass === "body");
});

test("a record whose slot points outside RECORDS is an entry error", async () => {
  const bytes = writePhkv({ sources: [], entries: [{ key: "a", bytes: utf8("1") }] });
  const a = await PhkvArchive.openStore(bytes, "https://x/");
  // the one slot with a record: point it past the end
  const slots = (a as unknown as { slots: Uint8Array }).slots;
  for (let s = 0; s < slots.length; s += 12) {
    if (new DataView(slots.buffer, slots.byteOffset + s).getUint32(8, true) !== 0) slots[s + 1] = 0xff;
  }
  assert.throws(() => a.lookup("a"), (e) => e instanceof RefkvError && e.errorClass === "entry");
});

// spec §7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#7-writing> and §23.1
// <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#231-requirements>
test("the writer rejects what the spec's requirements reject", () => {
  const rejects = (desc: ArchiveDesc) => assert.throws(() => writePhkv(desc), InvalidInputError);
  rejects({ sources: [], entries: [{ key: "", bytes: utf8("x") }] });
  rejects({ sources: [], entries: [{ key: "a", bytes: utf8("x") }, { key: "a", bytes: utf8("y") }] });
  rejects({ sources: [{ key: "gone" }], entries: [] });
  rejects({ sources: [{ url: "u" }], entries: [{ key: "a", ranges: [{ source: 3, offset: 0n, length: 1n }] }] });
});

test("a reference with a payload too big for vzip is a phkv record", async () => {
  // 3000 ranges: a Concat payload of well over vzip's 65,519 bytes
  const ranges = Array.from({ length: 6000 }, (_, i) => ({ source: 0, offset: BigInt(i % 10), length: 1n, crc32c: 0xffffffff }));
  const desc: ArchiveDesc = { sources: [{ url: "blob.bin" }], entries: [{ key: "many", ranges }] };
  await assert.rejects(writeVzip(desc), InvalidInputError);
  const a = await open(desc);
  assert.equal(a.size(a.lookup("many")!), 6000);
});

test("reads a store the Rust reference writer wrote", async () => {
  const bytes = new Uint8Array(fs.readFileSync(new URL("../../fixtures/phkv/rust-deflate.phkv", import.meta.url)));
  const blob = new Uint8Array(100_000).map((_, i) => (i * 7) & 255);
  const a = await PhkvArchive.openStore(bytes, "https://data.test/dir/s.phkv", async (url, start, end) => {
    assert.equal(url, "https://data.test/dir/blob.bin");
    return { data: blob.subarray(start, end), size: blob.length };
  });
  const keys = a.keys();
  assert.equal(keys.length, 44);
  assert.deepEqual(keys, [...keys].sort((x, y) => Buffer.compare(Buffer.from(x), Buffer.from(y))));
  assert.equal(a.revision, 11);
  assert.equal(text(await a.read("zarr.json")), '{"zarr_format":3}');
  assert.deepEqual(await a.read("big"), new Uint8Array(20_000).map((_, i) => i % 253));
  assert.equal(text(await a.read("ключ/键")), "unicode");
  assert.deepEqual(await a.read("concat"), Uint8Array.from([...utf8("5678-"), blob[1], blob[2]]));
  for (const i of [0, 1, 39]) {
    assert.deepEqual(await a.read(`a/c/${i >> 6}/${i & 63}`), blob.subarray(30 * i, 30 * i + 25), String(i));
  }
});

test("a store whose sections are Zstandard is refused, and says why", async () => {
  const store = writePhkv({ sources: [], entries: [{ key: "a", bytes: utf8("1") }] });
  const dirOffset = Number(new DataView(store.buffer).getBigUint64(store.length - 64 + 24, true));
  const dirLength = Number(new DataView(store.buffer).getBigUint64(store.length - 64 + 32, true));
  store[dirOffset + dirLength - 40 + 4] = 2; // the last section's codec: SOURCES, which may be compressed
  const view = new DataView(store.buffer);
  view.setUint32(store.length - 64 + 56, crc32c(store.subarray(dirOffset, dirOffset + dirLength)), true);
  view.setUint32(store.length - 4, crc32c(store.subarray(store.length - 64, store.length - 4)), true);
  await assert.rejects(PhkvArchive.openStore(store, "https://x/"), (e) =>
    e instanceof RefkvError && e.errorClass === "archive" && /Zstandard/.test(e.message));
});

test("refuses vzip's format-entry names as key sources only in a vzip archive: a phkv store reserves no names", async () => {
  const names = (a: Archive) => (a as unknown as { namesFormatEntry: (key: string) => boolean }).namesFormatEntry.bind(a);
  const phkv = await PhkvArchive.openStore(writePhkv({ sources: [], entries: [] }), "https://x/");
  const vzip = await Archive.open(await writeVzip({ sources: [], entries: [] }), "https://x/");
  assert.deepEqual([names(phkv)("__vz__/sources"), names(phkv)("__vz__/index")], [false, false]);
  assert.deepEqual([names(vzip)("__vz__/sources"), names(vzip)("__vz__/index"), names(vzip)("k")], [true, true, false]);
});
