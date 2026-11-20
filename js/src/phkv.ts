// A phkv store, standalone profile, in memory (spec/bindings/phkv.md
// <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md>, Part 3, a draft): a writer
// (§23 <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#23-writing-a-phkv-store>) and a reader, `PhkvArchive`, which finds a key through the perfect hash in one hop (§19.5 <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#195-lookup>)
// and resolves references as `Archive` does (§6.3 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#63-resolving-a-reference>).
//
// What this implements of the format: codec 0 (none) for sections and values the writer writes, and
// DEFLATE (codec 1) for those it reads. Not implemented: the hybrid profile, Zstandard (codec 2:
// an entry that uses it reads as an entry error), PROPERTY sections (skipped), and `list(prefix)`
// through the page index (`keys()` reads the pages whole). It is a port of rust/refkv-phkv.

import { Archive, type Entry, type Policy, type RangeFetcher, DEFAULT_POLICY, RefkvError, validatePolicy } from "./archive.ts";
import { crc32c, inflateRaw } from "./deflate.ts";
import { readHttpRange } from "./http.ts";
import { build, hash, slot, type Params } from "./ptrhash.ts";
import { decodeTable, encodeSourceTable } from "./protobuf.ts";
import { type ArchiveDesc, CONCAT_ID, RANGE_ID, referenceBlock, SPEC_REVISION, validate } from "./writer.ts";

export const MAGIC = "PHKV";
export const SUPERBLOCK_SIZE = 64;
const DIR_ENTRY_SIZE = 40;
const SLOT_SIZE = 12;
const SLOT_LENGTH_MAX = 0xffffffff;
const MAX_PAYLOAD = 1 << 24;
/** Bytes values this short or shorter are inline in their records. */
export const INLINE_MAX = 4096;
/** About how many bytes of records a page holds. */
export const PAGE_SIZE = 4096;

export const SECTION = { SOURCES: 1, RECORDS: 2, PAGES: 3, PTRHASH: 4, SLOTS: 5, PROPERTY: 6, METADATA: 7 } as const;
const KIND_BYTES = 0;
const KIND_RANGE = 1;
const KIND_CONCAT = 2;
const REC_INLINE = 1;
const REC_CRC32C = 2;
const REC_HIDDEN = 4;

const utf8 = new TextEncoder();
const strictUtf8 = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });

/** The probe distances of a standalone store (spec §17.2 <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#172-finding-the-superblock>): 0, and 2^k for k from 10 to 30. */
const probeDistances = (): number[] => [0, ...Array.from({ length: 21 }, (_, k) => 2 ** (10 + k))];

// ---- writing

class Bytes {
  private chunks: Uint8Array[] = [];
  length = 0;
  push(b: Uint8Array) {
    this.chunks.push(b);
    this.length += b.length;
  }
  /** The bytes of `values` as a varint each, which is how a record's fields come. */
  varint(v: number) {
    const out: number[] = [];
    do {
      let b = v % 128;
      v = Math.floor(v / 128);
      if (v !== 0) b |= 0x80;
      out.push(b);
    } while (v !== 0);
    this.push(Uint8Array.from(out));
  }
  u8(v: number) {
    this.push(Uint8Array.of(v));
  }
  u32(v: number) {
    const b = new Uint8Array(4);
    new DataView(b.buffer).setUint32(0, v, true);
    this.push(b);
  }
  u64(v: number | bigint) {
    const b = new Uint8Array(8);
    new DataView(b.buffer).setBigUint64(0, BigInt(v), true);
    this.push(b);
  }
  align8() {
    const pad = (8 - (this.length % 8)) % 8;
    if (pad) this.push(new Uint8Array(pad));
  }
  concat(): Uint8Array {
    const out = new Uint8Array(this.length);
    let at = 0;
    for (const c of this.chunks) {
      out.set(c, at);
      at += c.length;
    }
    return out;
  }
}

const compareBytes = (a: Uint8Array, b: Uint8Array): number => {
  const n = Math.min(a.length, b.length);
  for (let i = 0; i < n; i++) if (a[i] !== b[i]) return a[i] - b[i];
  return a.length - b.length;
};

interface Section {
  type: number;
  offset: number;
  length: number;
  crc: number;
}

/**
 * Writes `desc` as a standalone phkv store, in the recommended layout (spec §23.2 <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#232-recommended-layout-informative>): the bytes values
 * that are not inline, the records, the slot table, the page index, the perfect hash, the source
 * table, the section directory and the superblock (`distance` 0). Every section is stored
 * (codec 0), and every bytes value too. Throws InvalidInputError for input that spec §7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#7-writing> or §23.1 <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#231-requirements>
 * rejects. `desc.mirror` and `compress` are vzip's and are ignored.
 */
export function writePhkv(desc: ArchiveDesc): Uint8Array {
  validate(desc, MAX_PAYLOAD);
  const entries = desc.entries
    .map((e) => ({ e, key: utf8.encode(e.key) }))
    .sort((a, b) => compareBytes(a.key, b.key));
  const out = new Bytes();

  // the bytes values that are not inline, in key order
  const stored = new Map<number, number>();
  entries.forEach(({ e }, rank) => {
    if ("bytes" in e && e.bytes.length > INLINE_MAX) {
      stored.set(rank, out.length);
      out.push(e.bytes);
    }
  });

  // RECORDS, and the pages over them
  const recordsStart = out.length;
  const offsets: number[] = [];
  const lengths: number[] = [];
  const pages: { firstKey: Uint8Array; offset: number; length: number }[] = [];
  let pageStart = 0;
  entries.forEach(({ e, key }, rank) => {
    const at = out.length;
    if (rank === 0 || at - pageStart >= PAGE_SIZE) {
      if (pages.length > 0) pages[pages.length - 1].length = at - pages[pages.length - 1].offset;
      pages.push({ firstKey: key, offset: at, length: 0 });
      pageStart = at;
    }
    offsets.push(at - recordsStart);
    const rec = new Bytes();
    rec.varint(key.length);
    if ("bytes" in e) {
      const offset = stored.get(rank);
      rec.u8(KIND_BYTES);
      rec.u8((offset === undefined ? REC_INLINE : 0) | REC_CRC32C);
      rec.varint(rank);
      rec.u8(0); // codec: none
      if (offset !== undefined) rec.varint(offset);
      rec.varint(e.bytes.length);
      rec.varint(e.bytes.length);
      rec.u32(crc32c(e.bytes));
      rec.push(key);
      if (offset === undefined) rec.push(e.bytes);
    } else {
      const { id, payload } = referenceBlock(e.ranges);
      rec.u8(id === RANGE_ID ? KIND_RANGE : KIND_CONCAT);
      rec.u8(0);
      rec.varint(rank);
      rec.varint(payload.length);
      rec.push(key);
      rec.push(payload);
    }
    lengths.push(rec.length);
    out.push(rec.concat());
  });
  if (pages.length > 0) pages[pages.length - 1].length = out.length - pages[pages.length - 1].offset;
  const sections: Section[] = [];
  const add = (type: number, body: Uint8Array) => {
    sections.push({ type, offset: out.length, length: body.length, crc: crc32c(body) });
    out.push(body);
  };
  const records = out.concat().subarray(recordsStart);
  if (entries.length > 0) {
    sections.push({ type: SECTION.RECORDS, offset: recordsStart, length: records.length, crc: crc32c(records) });

    // the perfect hash, and SLOTS
    const built = build(entries.map(({ key }) => key));
    if (built === undefined) throw new Error("no seed places every key (duplicate keys?)");
    out.align8();
    const slots = new Uint8Array(built.slots.length * SLOT_SIZE);
    const view = new DataView(slots.buffer);
    built.slots.forEach((i, s) => {
      if (i < 0) return;
      view.setBigUint64(s * SLOT_SIZE, BigInt(offsets[i]), true);
      view.setUint32(s * SLOT_SIZE + 8, lengths[i] < SLOT_LENGTH_MAX ? lengths[i] : SLOT_LENGTH_MAX, true);
    });
    add(SECTION.SLOTS, slots);

    const pageIndex = new Bytes();
    for (const p of pages) {
      pageIndex.varint(p.firstKey.length);
      pageIndex.push(p.firstKey);
      pageIndex.varint(p.offset);
      pageIndex.varint(p.length);
    }
    add(SECTION.PAGES, pageIndex.concat());

    const ptrhash = new Bytes();
    ptrhash.u8(1); // XXH64
    ptrhash.push(new Uint8Array(7));
    ptrhash.u64(built.params.seed);
    ptrhash.u64(built.params.parts);
    ptrhash.u64(built.params.slots);
    ptrhash.u64(built.params.buckets);
    ptrhash.push(built.pilots);
    add(SECTION.PTRHASH, ptrhash.concat());
  }
  add(SECTION.SOURCES, encodeSourceTable(desc.sources, SPEC_REVISION));

  // the section directory, then the superblock
  const dir = new Bytes();
  for (const s of sections) {
    dir.u32(s.type);
    dir.u8(0); // codec: none
    dir.u8(0);
    dir.push(new Uint8Array(2));
    dir.u64(s.offset);
    dir.u64(s.length);
    dir.u64(s.length);
    dir.u32(s.crc);
    dir.push(new Uint8Array(4));
  }
  const directory = dir.concat();
  const directoryOffset = out.length;
  out.push(directory);
  const sb = new Bytes();
  sb.push(utf8.encode(MAGIC));
  sb.push(new Uint8Array(4)); // version 0, flags 0
  sb.u64(0); // distance
  sb.u64(entries.length);
  sb.u64(directoryOffset);
  sb.u64(directory.length);
  sb.u64(0); // region_offset
  sb.u64(directoryOffset + directory.length + SUPERBLOCK_SIZE); // region_length
  sb.u32(crc32c(directory));
  const head = sb.concat();
  out.push(head);
  out.u32(crc32c(head));
  return out.concat();
}

// ---- reading

const fail = (errorClass: ConstructorParameters<typeof RefkvError>[0], message: string): never => {
  throw new RefkvError(errorClass, message);
};

interface Superblock {
  version: number;
  flags: number;
  distance: number;
  keyCount: number;
  directoryOffset: number;
  directoryLength: number;
  directoryCrc: number;
}

const safe = (v: bigint, what: string): number =>
  v > BigInt(Number.MAX_SAFE_INTEGER) ? fail("archive", `${what} is too large for this reader`) : Number(v);

/** Whether the four bytes at `at` are the magic: compared as bytes, since arbitrary bytes are not UTF-8. */
const hasMagic = (bytes: Uint8Array, at: number): boolean => [...MAGIC].every((c, i) => bytes[at + i] === c.charCodeAt(0));

/** The superblock of a standalone store, at the first probe distance that matches (spec §17.2 <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#172-finding-the-superblock>). */
function findSuperblock(bytes: Uint8Array): Superblock | undefined {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  for (const d of probeDistances()) {
    const at = bytes.length - d - SUPERBLOCK_SIZE;
    if (at < 0) break;
    if (
      !hasMagic(bytes, at) ||
      view.getUint32(at + 60, true) !== crc32c(bytes.subarray(at, at + 60)) ||
      view.getBigUint64(at + 8, true) !== BigInt(d)
    ) {
      continue;
    }
    return {
      version: view.getUint16(at + 4, true),
      flags: view.getUint16(at + 6, true),
      distance: d,
      keyCount: safe(view.getBigUint64(at + 16, true), "the key count"),
      directoryOffset: safe(view.getBigUint64(at + 24, true), "the directory offset"),
      directoryLength: safe(view.getBigUint64(at + 32, true), "the directory length"),
      directoryCrc: view.getUint32(at + 56, true),
    };
  }
  return undefined;
}

/** Whether `bytes` is a standalone phkv store, as far as its superblock says. */
export function isPhkv(bytes: Uint8Array): boolean {
  try {
    return findSuperblock(bytes) !== undefined;
  } catch {
    return false;
  }
}

interface Dir {
  type: number;
  codec: number;
  flags: number;
  offset: number;
  length: number;
  size: number;
  crc: number;
}

/** A varint at `at`, and where it ends. */
function varint(b: Uint8Array, at: number): [number, number] {
  let v = 0;
  let scale = 1;
  for (let i = 0; i < 10; i++) {
    if (at >= b.length) return fail("entry", "a truncated varint");
    const byte = b[at++];
    v += (byte & 0x7f) * scale;
    if (v > Number.MAX_SAFE_INTEGER) return fail("entry", "a varint too large for this reader");
    if (!(byte & 0x80)) return [v, at];
    scale *= 128;
  }
  return fail("entry", "an overlong varint");
}

export class PhkvArchive extends Archive {
  protected override maxPayload = MAX_PAYLOAD;
  private params!: Params;
  private pilots!: Uint8Array;
  private slots!: Uint8Array;
  private records!: Uint8Array;
  private recordsStart!: number;
  private keyCount!: number;
  private pages: { key: string; offset: number; length: number }[] = [];
  private listed: string[] | undefined;

  /**
   * Opens a standalone phkv store held in memory (spec §22.1
   * <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#221-opening>): `baseUrl`
   * resolves relative `url` sources, as `Archive.open`'s does, and `policy` is the reader policy.
   * Throws a `RefkvError` of class "archive" for a store that opening refuses.
   */
  static async openStore(
    bytes: Uint8Array,
    baseUrl: string,
    fetchRange?: RangeFetcher,
    policy: Policy = {},
  ): Promise<PhkvArchive> {
    validatePolicy(policy);
    const rules = { ...DEFAULT_POLICY, ...policy };
    const sb = findSuperblock(bytes);
    if (sb === undefined) return fail("archive", "not a phkv store");
    if (sb.version !== 0) return fail("archive", `unsupported phkv version ${sb.version}`);
    if (sb.flags & 1) return fail("archive", "a hybrid phkv store: this reader opens standalone stores only");
    if (sb.flags !== 0) return fail("archive", `unsupported phkv flags ${sb.flags}`);
    const n = bytes.length;
    if (sb.directoryLength % DIR_ENTRY_SIZE !== 0 || sb.directoryOffset + sb.directoryLength > n) {
      return fail("archive", "the section directory lies outside the file or has a partial entry");
    }
    const directory = bytes.subarray(sb.directoryOffset, sb.directoryOffset + sb.directoryLength);
    if (crc32c(directory) !== sb.directoryCrc) return fail("archive", "the section directory does not match its crc32c");
    const dir: Dir[] = [];
    for (let at = 0; at < directory.length; at += DIR_ENTRY_SIZE) {
      const d = new DataView(directory.buffer, directory.byteOffset + at, DIR_ENTRY_SIZE);
      if (d.getUint16(6, true) !== 0 || d.getUint32(36, true) !== 0) {
        return fail("archive", "a directory entry's reserved fields are not 0");
      }
      const flags = directory[at + 5];
      if (flags & ~1) return fail("archive", `a directory entry has unknown flags ${flags}`);
      const entry: Dir = {
        type: d.getUint32(0, true),
        codec: directory[at + 4],
        flags,
        offset: safe(d.getBigUint64(8, true), "a section offset"),
        length: safe(d.getBigUint64(16, true), "a section length"),
        size: safe(d.getBigUint64(24, true), "a section size"),
        crc: d.getUint32(32, true),
      };
      if (entry.offset + entry.length > n) return fail("archive", `section ${entry.type} lies outside the file`);
      const known = Object.values(SECTION).includes(entry.type as never);
      if (!known && entry.flags & 1) return fail("archive", `unknown section ${entry.type} must be understood`);
      if (known) dir.push(entry);
    }
    const one = (type: number, required: boolean): Dir | undefined => {
      const found = dir.filter((d) => d.type === type);
      if (found.length > 1) return fail("archive", `more than one section of type ${type}`);
      if (found.length === 0 && required) return fail("archive", `no section of type ${type}`);
      return found[0];
    };
    const sourcesDir = one(SECTION.SOURCES, true)!;
    const records = one(SECTION.RECORDS, sb.keyCount > 0);
    const pagesDir = one(SECTION.PAGES, sb.keyCount > 0);
    const ptrhashDir = one(SECTION.PTRHASH, sb.keyCount > 0);
    const slotsDir = one(SECTION.SLOTS, sb.keyCount > 0);
    const metadata = one(SECTION.METADATA, false);
    if (sb.keyCount === 0 && (records || pagesDir || ptrhashDir || slotsDir)) {
      return fail("archive", "a store with no keys has sections that need keys");
    }
    for (const d of [records, slotsDir]) if (d !== undefined && d.codec !== 0) fail("archive", `section ${d.type} has codec ${d.codec}`);

    /** A section read whole: checked against its crc32c and decoded to its size (spec §17.5 <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#175-codecs>). */
    const whole = async (d: Dir): Promise<Uint8Array> => {
      const stored = bytes.subarray(d.offset, d.offset + d.length);
      if (crc32c(stored) !== d.crc) return fail("archive", `section ${d.type} does not match its crc32c`);
      let body: Uint8Array;
      if (d.codec === 0) body = stored;
      else if (d.codec === 1) {
        try {
          body = await inflateRaw(stored, Math.min(d.size, rules.maxFormatEntry));
        } catch (e) {
          return fail("archive", `section ${d.type}: ${(e as Error).message}`);
        }
      } else if (d.codec === 2) {
        return fail(
          "archive",
          `section ${d.type} is compressed with Zstandard, which this reader does not decode yet (it reads codecs 0 and 1)`,
        );
      } else return fail("archive", `section ${d.type} has codec ${d.codec}, which the format does not define`);
      if (body.length !== d.size) return fail("archive", `section ${d.type} decodes to ${body.length} bytes, not ${d.size}`);
      if (d.size > rules.maxFormatEntry) return fail("archive", `section ${d.type} is larger than this reader allows`);
      return body;
    };

    let table: ReturnType<typeof decodeTable>;
    try {
      table = decodeTable(await whole(sourcesDir));
    } catch (e) {
      throw e instanceof RefkvError ? e : new RefkvError("archive", `source table: ${(e as Error).message}`);
    }
    if (table.sources.length > rules.maxSources) {
      return fail("archive", `the source table has ${table.sources.length} sources; this reader allows ${rules.maxSources}`);
    }
    if (metadata !== undefined) await whole(metadata);
    const store = new PhkvArchive(bytes, baseUrl, table.sources, new Map(), fetchRange ?? defaultFetch, rules, table.revision);
    store.keyCount = sb.keyCount;
    if (sb.keyCount > 0) {
      store.recordsStart = records!.offset;
      store.records = bytes.subarray(records!.offset, records!.offset + records!.length);
      const ph = await whole(ptrhashDir!);
      if (ph.length < 40 || ph[0] !== 1 || ph.subarray(1, 8).some((b) => b !== 0)) {
        return fail("archive", "the PTRHASH section is malformed");
      }
      const pv = new DataView(ph.buffer, ph.byteOffset, ph.byteLength);
      const params = {
        seed: pv.getBigUint64(8, true),
        parts: safe(pv.getBigUint64(16, true), "the part count"),
        slots: safe(pv.getBigUint64(24, true), "the slots per part"),
        buckets: safe(pv.getBigUint64(32, true), "the buckets per part"),
      };
      if (params.parts === 0 || params.slots === 0 || params.buckets === 0 || params.parts * params.slots < sb.keyCount ||
        params.parts * params.slots >= 2 ** 32 || params.slots >= 2 ** 32 || params.buckets >= 2 ** 32 || params.parts >= 2 ** 32 ||
        ph.length !== 40 + params.parts * params.buckets) {
        return fail("archive", "the PTRHASH section is malformed");
      }
      store.params = params;
      store.pilots = ph.subarray(40);
      if (slotsDir!.length !== SLOT_SIZE * params.parts * params.slots) {
        return fail("archive", "the SLOTS section is not 12 bytes per slot");
      }
      store.slots = bytes.subarray(slotsDir!.offset, slotsDir!.offset + slotsDir!.length);
      // the page index: contiguous over RECORDS, with strictly increasing first keys (spec §19.4 <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#194-page-index>)
      const pagesBytes = await whole(pagesDir!);
      let at = 0;
      let next = 0;
      let previous: Uint8Array | undefined;
      try {
        while (at < pagesBytes.length) {
          let len: number;
          [len, at] = varint(pagesBytes, at);
          if (len === 0 || at + len > pagesBytes.length) throw new Error("a page with a bad first_key");
          const first = pagesBytes.subarray(at, at + len);
          at += len;
          let offset: number;
          let length: number;
          [offset, at] = varint(pagesBytes, at);
          [length, at] = varint(pagesBytes, at);
          if (length === 0 || offset !== records!.offset + next) throw new Error("the pages are not contiguous over RECORDS");
          if (previous !== undefined && compareBytes(previous, first) >= 0) throw new Error("page keys are not increasing");
          previous = first;
          next += length;
          store.pages.push({ key: strictUtf8.decode(first), offset: offset - records!.offset, length });
        }
        if (next !== records!.length) throw new Error("the pages do not fill RECORDS");
      } catch (e) {
        return fail("archive", `the page index: ${(e as Error).message}`);
      }
    }
    return store;
  }

  /** A standalone phkv store reserves no names (spec/bindings/phkv.md). */
  protected override namesFormatEntry(): boolean {
    return false;
  }

  /** The record of `key`, found through the perfect hash (spec §19.5 <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#195-lookup>), as an entry. */
  protected override entryFor(key: string): Entry | undefined {
    const known = this.entries.get(key);
    if (known !== undefined || this.keyCount === 0) return known;
    const want = utf8.encode(key);
    if (want.length === 0) return undefined;
    const s = slot(this.params, this.pilots, hash(want, this.params.seed));
    const view = new DataView(this.slots.buffer, this.slots.byteOffset + s * SLOT_SIZE, SLOT_SIZE);
    const length = view.getUint32(8, true);
    if (length === 0) return undefined;
    const at = safe(view.getBigUint64(0, true), "a record offset");
    const entry = this.record(at, length === SLOT_LENGTH_MAX ? undefined : length, key);
    if (entry !== undefined) this.entries.set(key, entry);
    return entry;
  }

  /**
   * The record at `at` of RECORDS, as an entry, or undefined if its key is not `key`. A malformed
   * record is an entry error (spec §19.5 <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#195-lookup> step 3). `length` is the slot's, if it gave one.
   */
  private record(at: number, length: number | undefined, key: string): Entry | undefined {
    const bad = (m: string): never => fail("entry", `${JSON.stringify(key)}: ${m}`);
    const b = this.records;
    if (at >= b.length || (length !== undefined && (length < 6 || at + length > b.length))) bad("its slot is outside RECORDS");
    const end = length === undefined ? b.length : at + length;
    const body = b.subarray(0, end);
    let p = at;
    let keyLength: number;
    [keyLength, p] = varint(body, p);
    if (keyLength === 0 || p + 2 > end) bad("a malformed record");
    const kind = body[p++];
    const flags = body[p++];
    if (kind > KIND_CONCAT || flags & ~(REC_INLINE | REC_CRC32C | REC_HIDDEN)) bad("a malformed record");
    let rank: number;
    [rank, p] = varint(body, p);
    void rank;
    const entry: Entry = { key, method: 0, csize: 0, size: 0, bodyOffset: 0 };
    let dataLength = 0;
    let offset = 0;
    if (kind === KIND_BYTES) {
      if (p >= end) bad("a malformed record");
      const codec = body[p++];
      if (codec > 2) bad("a malformed record");
      if (!(flags & REC_INLINE)) [offset, p] = varint(body, p);
      [entry.csize, p] = varint(body, p);
      [entry.size, p] = varint(body, p);
      if (flags & REC_CRC32C) {
        if (p + 4 > end) bad("a malformed record");
        entry.crc32c = new DataView(body.buffer, body.byteOffset + p, 4).getUint32(0, true);
        p += 4;
      }
      entry.method = codec === 0 ? 0 : codec === 1 ? 8 : -1;
      if (codec === 2) entry.error = "Zstandard values are not supported by this reader";
      dataLength = flags & REC_INLINE ? entry.csize : 0;
    } else {
      if (flags & (REC_INLINE | REC_CRC32C)) bad("a malformed reference record");
      [dataLength, p] = varint(body, p);
      if (dataLength > MAX_PAYLOAD) bad("a reference payload over 2^24 bytes");
    }
    if (p + keyLength + dataLength > end) bad("a record that extends past its end");
    let name: string;
    try {
      name = strictUtf8.decode(body.subarray(p, p + keyLength));
    } catch {
      return bad("a key that is not UTF-8");
    }
    if (name !== key) return undefined;
    p += keyLength;
    if (kind === KIND_BYTES) {
      entry.bodyOffset = flags & REC_INLINE ? this.recordsStart + p : offset;
    } else {
      entry.reference = { headerId: kind === KIND_RANGE ? RANGE_ID : CONCAT_ID, payload: body.slice(p, p + dataLength) };
    }
    return entry;
  }

  /** A phkv store reserves no names (spec §16 <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#16-what-the-phkv-binding-defines>): every key is visible. */
  override lookup(key: string): Entry | undefined {
    const entry = this.entryFor(key);
    if (entry?.error !== undefined) throw new RefkvError("entry", `${JSON.stringify(key)}: ${entry.error}`);
    return entry;
  }

  /** The keys, in UTF-8 order, from the page index. */
  override keys(): string[] {
    if (this.listed === undefined) {
      const keys: string[] = [];
      for (const page of this.pages) {
        let p = page.offset;
        const end = page.offset + page.length;
        while (p < end) {
          let keyLength: number;
          const start = p;
          [keyLength, p] = varint(this.records, p);
          const kind = this.records[p];
          const flags = this.records[p + 1];
          p += 2;
          [, p] = varint(this.records, p); // rank
          let dataLength: number;
          if (kind === KIND_BYTES) {
            p++; // codec
            if (!(flags & REC_INLINE)) [, p] = varint(this.records, p);
            let stored: number;
            [stored, p] = varint(this.records, p);
            [, p] = varint(this.records, p);
            if (flags & REC_CRC32C) p += 4;
            dataLength = flags & REC_INLINE ? stored : 0;
          } else {
            [dataLength, p] = varint(this.records, p);
          }
          if (p + keyLength + dataLength > end) fail("entry", `a page that cannot be parsed, at ${start}`);
          if (!(flags & REC_HIDDEN)) keys.push(strictUtf8.decode(this.records.subarray(p, p + keyLength)));
          p += keyLength + dataLength;
        }
      }
      this.listed = keys;
    }
    return this.listed;
  }
}

const defaultFetch: RangeFetcher = (url, start, end, pins, options) =>
  readHttpRange(url, start, end, pins, undefined, options);

/**
 * Opens `bytes` as the store it is: a phkv store if its superblock is there, else a vzip archive
 * (as `Archive.open`, which gives its error for a file that is neither).
 */
export function openArchive(
  bytes: Uint8Array,
  baseUrl: string,
  fetchRange?: RangeFetcher,
  policy: Policy = {},
): Promise<Archive> {
  return isPhkv(bytes) ? PhkvArchive.openStore(bytes, baseUrl, fetchRange, policy) : Archive.open(bytes, baseUrl, fetchRange, policy);
}
