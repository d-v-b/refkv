// The ZIP64 end records, which every archive has, and large entries (spec §10.1
// <https://github.com/d-v-b/refkv/blob/main/spec/archive.md#101-structure> rules 4 and 7, §10.2
// <https://github.com/d-v-b/refkv/blob/main/spec/archive.md#102-zip64>).

import assert from "node:assert/strict";
import { test } from "node:test";
import { Archive, RefkvError } from "../src/archive.ts";
import {
  centralRecord,
  type Header,
  InvalidInputError,
  localHeader,
  writeVzip,
} from "../src/writer.ts";

const U32 = 0xffffffff;
const utf8 = (s: string) => new TextEncoder().encode(s);
const text = (b: Uint8Array | undefined) => new TextDecoder().decode(b);
const remote = utf8("0123456789abcdefghij");
const fetchRemote = async (_url: string, start: number, end: number) => ({
  data: remote.subarray(start, end),
  size: remote.length,
});

/** A small archive: "z" (STORED "hello"), "fine" and a reference "x". */
const small = () =>
  writeVzip({
    sources: [{ url: "blob.bin" }],
    entries: [
      { key: "z", bytes: utf8("hello") },
      { key: "fine", bytes: utf8("ok"), compress: true },
      { key: "x", ranges: [{ source: 0, offset: 3n, length: 2n }] },
    ],
  });

/** The offsets of the zip64 record, its locator and the end record. */
function tail(b: Uint8Array) {
  const eocd = b.length - 44;
  return { z: eocd - 76, loc: eocd - 20, eocd };
}

const view = (b: Uint8Array) => new DataView(b.buffer, b.byteOffset, b.byteLength);
const open = (b: Uint8Array) => Archive.open(b, "https://data.test/a.vzip", fetchRemote);
const isClass = (cls: string) => (e: unknown) => e instanceof RefkvError && e.errorClass === cls;

test("writes the zip64 end records in every archive, and reads them whatever the end record holds", async () => {
  const many = Array.from({ length: 70000 }, (_, i) => ({
    key: `c/${i}`,
    ranges: [{ source: 0, offset: BigInt(i % 10), length: 1n }],
  }));
  const cases: [Uint8Array, number, [number, number, number, number] | undefined][] = [
    [await writeVzip({ sources: [], entries: [] }), 1, undefined],
    [await small(), 4, undefined],
    [await small(), 4, [0, 0, 0, 0]],
    [await small(), 4, [7, 9, U32 - 15, 0x12345678]],
    [await writeVzip({ sources: [{ url: "blob.bin" }], entries: many }), 70001, undefined],
  ];
  for (const [bytes, count, eocdFields] of cases) {
    const v = view(bytes);
    const { z, loc, eocd } = tail(bytes);
    assert.equal(v.getUint32(z, true), 0x06064b50);
    assert.equal(v.getBigUint64(z + 4, true), 44n);
    assert.equal(v.getUint16(z + 14, true), 45, "version needed");
    assert.equal(v.getBigUint64(z + 24, true), BigInt(count));
    assert.equal(v.getBigUint64(z + 32, true), BigInt(count));
    assert.equal(v.getBigUint64(z + 48, true) + v.getBigUint64(z + 40, true), BigInt(z));
    assert.equal(v.getUint32(loc, true), 0x07064b50);
    assert.equal(v.getBigUint64(loc + 8, true), BigInt(z));
    assert.equal(v.getUint32(loc + 16, true), 1);
    assert.deepEqual(
      [v.getUint16(eocd + 8, true), v.getUint16(eocd + 10, true), v.getUint32(eocd + 12, true), v.getUint32(eocd + 16, true)],
      [0xffff, 0xffff, U32, U32],
    );
    if (eocdFields) {
      v.setUint16(eocd + 8, eocdFields[0], true);
      v.setUint16(eocd + 10, eocdFields[1], true);
      v.setUint32(eocd + 12, eocdFields[2], true);
      v.setUint32(eocd + 16, eocdFields[3], true);
    }
    const a = await open(bytes);
    assert.equal(a.keys().length, count - 1);
    if (count === 4) {
      assert.equal(text(await a.read("z")), "hello");
      assert.equal(text(await a.read("x")), "34");
    }
  }
});

async function brokenTail(mutate: (b: Uint8Array, t: ReturnType<typeof tail>) => Uint8Array | void) {
  const b = await small();
  return mutate(b, tail(b)) ?? b;
}

test("refuses to open an archive without a zip64 locator", async () => {
  const b = await brokenTail((b, { z, eocd }) => new Uint8Array([...b.subarray(0, z), ...b.subarray(eocd)]));
  await assert.rejects(open(b), isClass("archive"));
});

test("refuses to open an archive whose zip64 record has a wrong signature", async () => {
  const b = await brokenTail((b, { z }) => view(b).setUint32(z, 0x06064b51, true));
  await assert.rejects(open(b), isClass("archive"));
});

test("refuses to open an archive whose zip64 record's size field is not 44", async () => {
  const b = await brokenTail((b, { z }) => view(b).setBigUint64(z + 4, 45n, true));
  await assert.rejects(open(b), isClass("archive"));
});

test("refuses to open an archive whose zip64 record lies outside the file", async () => {
  const b = await brokenTail((b, { loc }) => view(b).setBigUint64(loc + 8, BigInt(b.length - 55), true));
  await assert.rejects(open(b), isClass("archive"));
});

const header = (size: number, offset: number): Header => ({
  name: utf8("k"),
  method: 0,
  crc: 0x1234,
  size,
  csize: size,
  extra: new Uint8Array(),
  offset,
});

const u64s = (...values: number[]) => {
  const out = new Uint8Array(8 * values.length);
  values.forEach((v, i) => view(out).setBigUint64(8 * i, BigInt(v), true));
  return out;
};

test("writes a large entry's sizes, and an offset that needs it, in ZIP64 fields", () => {
  const big = 2 ** 32 + 7;
  const far = 2 ** 33;
  const lfh = localHeader(header(big, 0));
  assert.equal(view(lfh).getUint16(4, true), 45);
  assert.deepEqual([view(lfh).getUint32(18, true), view(lfh).getUint32(22, true)], [U32, U32]);
  assert.deepEqual([view(lfh).getUint16(26, true), view(lfh).getUint16(28, true)], [1, 20]);
  assert.deepEqual([...lfh.subarray(31)], [1, 0, 16, 0, ...u64s(big, big)]);
  assert.equal(localHeader(header(5, far)).length, 31, "no extra field below 4 GiB");
  const cases: [number, number, Uint8Array][] = [
    [big, 0, u64s(big, big)],
    [big, far, u64s(big, big, far)],
    [U32, 0, u64s(U32, U32)],
    [5, far, u64s(far)],
    [5, 0, new Uint8Array()],
  ];
  for (const [size, offset, values] of cases) {
    const rec = centralRecord(header(size, offset));
    const block = values.length ? [1, 0, values.length, 0, ...values] : [];
    assert.deepEqual([...rec.subarray(47)], block, `${size} at ${offset}`);
    const large = size >= U32;
    assert.equal(view(rec).getUint32(20, true), large ? U32 : size);
    assert.equal(view(rec).getUint32(42, true), offset >= U32 ? U32 : offset);
  }
});

/**
 * A small archive whose central directory record for `key` has the size fields, offset
 * field, extra field and method that `mutate` returns; the zip64 record is moved after
 * the directory and the locator updated.
 */
async function patched(
  key: string,
  mutate: (r: { size: number; csize: number; offset: number; extra: Uint8Array; method: number }) =>
    { size: number; csize: number; offset: number; extra: Uint8Array; method: number },
) {
  const b = await small();
  const v = view(b);
  const { z } = tail(b);
  const cdOffset = Number(v.getBigUint64(z + 48, true));
  let i = cdOffset;
  while (text(b.subarray(i + 46, i + 46 + v.getUint16(i + 28, true))) !== key) {
    i += 46 + v.getUint16(i + 28, true) + v.getUint16(i + 30, true);
  }
  const nameLen = v.getUint16(i + 28, true);
  const extraLen = v.getUint16(i + 30, true);
  const r = mutate({
    size: v.getUint32(i + 24, true),
    csize: v.getUint32(i + 20, true),
    offset: v.getUint32(i + 42, true),
    extra: b.slice(i + 46 + nameLen, i + 46 + nameLen + extraLen),
    method: v.getUint16(i + 10, true),
  });
  const head = b.slice(i, i + 46 + nameLen);
  const hv = view(head);
  hv.setUint16(10, r.method, true);
  hv.setUint32(20, r.csize, true);
  hv.setUint32(24, r.size, true);
  hv.setUint16(30, r.extra.length, true);
  hv.setUint32(42, r.offset, true);
  const out = new Uint8Array([
    ...b.subarray(0, i), ...head, ...r.extra, ...b.subarray(i + 46 + nameLen + extraLen),
  ]);
  const ov = view(out);
  const z2 = z + r.extra.length - extraLen;
  ov.setBigUint64(z2 + 40, ov.getBigUint64(z2 + 40, true) + BigInt(r.extra.length - extraLen), true);
  ov.setBigUint64(z2 + 56 + 8, BigInt(z2), true);
  const a = await open(out);
  assert.equal(text(await a.read("fine")), "ok", "other keys are unaffected");
  return a;
}

const z64 = (...values: number[]) => new Uint8Array([1, 0, 8 * values.length, 0, ...u64s(...values)]);

test("reads a large entry's sizes and offset from its record", async () => {
  const big = 2 ** 32 + 3;
  for (const far of [false, true]) {
    let offset = 0;
    const a = await patched("z", (r) => {
      offset = r.offset;
      return {
        ...r,
        size: U32,
        csize: U32,
        offset: far ? U32 : r.offset,
        extra: new Uint8Array([...(far ? z64(big, big, r.offset) : z64(big, big)), ...r.extra]),
      };
    });
    const e = a.lookup("z")!;
    assert.deepEqual([e.size, e.csize, e.bodyOffset], [big, big, offset + 30 + 1 + 20]);
    // the body this record describes is not in the file
    await assert.rejects(a.read("z", 0, 4), isClass("body"));
  }
});

const entryError = async (key: string, a: Archive) =>
  assert.throws(() => a.lookup(key), isClass("entry"));

test("an entry with exactly one size field all ones is an entry error", async () => {
  await entryError("z", await patched("z", (r) => ({ ...r, size: U32, extra: z64(U32, r.csize) })));
});

test("a large entry whose ZIP64 block is too short for its sizes is an entry error", async () => {
  await entryError("z", await patched("z", (r) => ({ ...r, size: U32, csize: U32, extra: z64(5) })));
});

test("a large entry whose ZIP64 block lacks the offset it needs is an entry error", async () => {
  await entryError("z", await patched("z", (r) => ({ ...r, size: U32, csize: U32, offset: U32, extra: z64(5, 5) })));
});

test("a large entry without a ZIP64 block is an entry error", async () => {
  await entryError("z", await patched("z", (r) => ({ ...r, size: U32, csize: U32 })));
});

test("a large reference entry is an entry error", async () => {
  await entryError("x", await patched("x", (r) => ({
    ...r, size: U32, csize: U32, extra: new Uint8Array([...z64(r.size, r.csize), ...r.extra]),
  })));
});

test("a large entry with method 8 is an entry error", async () => {
  await entryError("z", await patched("z", (r) => ({ ...r, size: U32, csize: U32, extra: z64(5, 7), method: 8 })));
});

test("the writer refuses to DEFLATE an entry of 4 GiB or more", async () => {
  const large = { length: 2 ** 32 } as Uint8Array; // the length of a large body, without its bytes
  await assert.rejects(
    writeVzip({ sources: [], entries: [{ key: "k", bytes: large, compress: true }] }),
    (e) => e instanceof InvalidInputError && /must be STORED/.test(e.message),
  );
});
