// vzip writer (spec/bindings/vzip.md §10
// <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#10-zip-container>, §11
// <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#11-entries>, §5
// <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#5-source-table>, §14
// <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#14-writing-a-vzip-archive>), without a page index.

import { crc32, deflateRaw } from "./deflate.ts";
import {
  encodeConcat,
  encodeRange,
  encodeSourceTable,
  type Range,
  type Source,
} from "./protobuf.ts";
import { isUriReference } from "./uri.ts";

export class InvalidInputError extends Error {}

export type EntryDesc =
  | { key: string; bytes: Uint8Array; compress?: boolean }
  | { key: string; ranges: Range[] };

export interface ArchiveDesc {
  sources: Source[];
  entries: EntryDesc[];
  /** Whether a reference entry's body repeats its payload (default true). */
  mirror?: boolean;
}

/**
 * The revision of spec/refkv.md the writer follows, recorded in every archive (§1.3
 * <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#13-versioning>, §5
 * <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#5-source-table>).
 */
export const SPEC_REVISION = 11;
export const SOURCES_KEY = "__vz__/sources";
export const INDEX_KEY = "__vz__/index";
export const RANGE_ID = 0x7a76;
export const CONCAT_ID = 0x7a77;
const U16_ALL = 0xffff;
const U32_ALL = 0xffffffff;
const U64_MAX = (1n << 64n) - 1n;
const MAX_PAYLOAD = 65519;
const STRONG_ETAG = /^"[\x21\x23-\x7e]*"$/;

const utf8 = new TextEncoder();

const reject = (message: string): never => {
  throw new InvalidInputError(message);
};

/** An entry's headers, less its body. */
export interface Header {
  name: Uint8Array;
  method: number;
  crc: number;
  size: number; // uncompressed
  csize: number; // compressed: the body's length
  extra: Uint8Array; // reference block, or empty
  offset: number; // of the local header
}

interface Built extends Header {
  body: Uint8Array;
}

/**
 * Whether an entry is large: its sizes go in ZIP64 fields (spec §10.1
 * <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#101-structure> rule 7).
 */
export const isLarge = (h: Pick<Header, "size" | "csize">): boolean =>
  h.size >= U32_ALL || h.csize >= U32_ALL;

/**
 * The local file header, with the name: its extra field is empty, except a large entry's,
 * which is the 20-byte ZIP64 field (spec §10.1
 * <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#101-structure> rules 4 and 7).
 */
export function localHeader(h: Header): Uint8Array {
  const large = isLarge(h);
  const extra = large ? le([[1, 2], [16, 2], [h.size, 8], [h.csize, 8]]) : new Uint8Array();
  return concat([
    le([
      [0x04034b50, 4], [large ? 45 : 20, 2], [0x0800, 2], [h.method, 2], [0, 2], [0x21, 2],
      [h.crc, 4], [large ? U32_ALL : h.csize, 4], [large ? U32_ALL : h.size, 4],
      [h.name.length, 2], [extra.length, 2],
    ]),
    h.name,
    extra,
  ]);
}

/** The body offset of an entry (spec §10.1 <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#101-structure> rule 4). */
export const bodyOffset = (h: Header): number =>
  h.offset + 30 + h.name.length + (isLarge(h) ? 20 : 0);

/**
 * The central directory record. Its ZIP64 block holds a large entry's sizes, then an
 * offset that does not fit its field, and nothing else (spec §10.2
 * <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#102-zip64>).
 */
export function centralRecord(h: Header): Uint8Array {
  const large = isLarge(h);
  const farOffset = h.offset >= U32_ALL;
  const values: [number, 8][] = [];
  if (large) values.push([h.size, 8], [h.csize, 8]);
  if (farOffset) values.push([h.offset, 8]);
  const zip64 = values.length > 0 ? le([[1, 2], [8 * values.length, 2], ...values]) : new Uint8Array();
  const extra = concat([zip64, h.extra]);
  return concat([
    le([
      [0x02014b50, 4], [20, 2], [zip64.length > 0 ? 45 : 20, 2], [0x0800, 2], [h.method, 2],
      [0, 2], [0x21, 2], [h.crc, 4], [large ? U32_ALL : h.csize, 4], [large ? U32_ALL : h.size, 4],
      [h.name.length, 2], [extra.length, 2], [0, 2], [0, 2], [0, 2], [0, 4],
      [farOffset ? U32_ALL : h.offset, 4],
    ]),
    h.name,
    extra,
  ]);
}

/**
 * The end records, which every archive has (spec §10.2
 * <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#102-zip64>): the zip64 end of central
 * directory record at `at`, its locator, and the end of central directory record, whose
 * counts, size and offset are all ones, followed by `comment`.
 */
export function endRecords(
  at: number,
  count: number,
  cdOffset: number,
  cdSize: number,
  comment: Uint8Array,
): Uint8Array {
  return concat([
    le([
      [0x06064b50, 4], [44, 8], [20, 2], [45, 2], [0, 4], [0, 4],
      [count, 8], [count, 8], [cdSize, 8], [cdOffset, 8],
    ]),
    le([[0x07064b50, 4], [0, 4], [at, 8], [1, 4]]),
    le([
      [0x06054b50, 4], [0, 2], [0, 2], [U16_ALL, 2], [U16_ALL, 2],
      [U32_ALL, 4], [U32_ALL, 4], [comment.length, 2],
    ]),
    comment,
  ]);
}

/** The canonical extra block (header ID and payload) of a reference. */
export function referenceBlock(ranges: Range[]): {
  id: number;
  payload: Uint8Array;
} {
  return ranges.length === 1
    ? { id: RANGE_ID, payload: encodeRange(ranges[0]) }
    : { id: CONCAT_ID, payload: encodeConcat(ranges) };
}

function validate(desc: ArchiveDesc) {
  const n = desc.sources.length;
  const byKey = new Map<string, EntryDesc>();
  for (const e of desc.entries) {
    if (e.key === "") reject("empty key");
    if (!e.key.isWellFormed()) reject(`key ${JSON.stringify(e.key)} is not valid Unicode`);
    if (utf8.encode(e.key).length > U16_ALL) reject(`key longer than 65535 bytes`);
    if (e.key === SOURCES_KEY || e.key === INDEX_KEY) reject(`${e.key} is a format entry`);
    if (byKey.has(e.key)) reject(`duplicate key ${JSON.stringify(e.key)}`);
    byKey.set(e.key, e);
  }
  for (const [i, s] of desc.sources.entries()) {
    const kinds = [s.url, s.key, s.data].filter((k) => k !== undefined);
    if (kinds.length !== 1) reject(`source ${i} must have exactly one of url, key, data`);
    const pinned =
      s.size !== undefined || s.etag !== undefined || s.modifiedNotAfter !== undefined;
    if (pinned && s.url === undefined) reject(`source ${i}: pin on a non-url source`);
    if (s.etag !== undefined && !STRONG_ETAG.test(s.etag)) {
      reject(`source ${i}: etag ${s.etag} is not a strong entity tag`);
    }
    if (s.url !== undefined && (s.url === "" || !isUriReference(s.url))) {
      reject(`source ${i}: url ${JSON.stringify(s.url)} is not a URI reference`);
    }
    if (s.key !== undefined) {
      const target = byKey.get(s.key);
      if (s.key === "") reject(`source ${i}: empty key`);
      if (target === undefined || !("bytes" in target)) {
        reject(`source ${i}: key ${JSON.stringify(s.key)} is not a bytes entry`);
      }
    }
  }
  const sourceSize = (i: number): bigint | undefined => {
    const s = desc.sources[i];
    if (s.data !== undefined) return BigInt(s.data.length);
    if (s.key !== undefined) return BigInt((byKey.get(s.key) as { bytes: Uint8Array }).bytes.length);
    return undefined; // url sources are not checked
  };
  for (const e of desc.entries) {
    if ("bytes" in e) {
      // spec §10.1 <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#101-structure> rule 7
      if (e.compress && e.bytes.length >= U32_ALL) {
        reject(`${e.key}: an entry of 4 GiB or more must be STORED`);
      }
      continue;
    }
    let total = 0n;
    for (const r of e.ranges) {
      if ("data" in r) {
        if ((r as { crc32c?: number }).crc32c !== undefined) {
          reject(`${e.key}: a literal range with a crc32c`);
        }
        total += BigInt(r.data.length);
        continue;
      }
      if (r.crc32c !== undefined && !(Number.isInteger(r.crc32c) && r.crc32c >= 0 && r.crc32c <= U32_ALL)) {
        reject(`${e.key}: crc32c ${r.crc32c} is not a uint32`);
      }
      if (!Number.isInteger(r.source) || r.source < 0 || r.source >= n) {
        reject(`${e.key}: source ${r.source} >= ${n} sources`);
      }
      if (r.offset < 0n || r.length < 0n || r.offset + r.length > U64_MAX) {
        reject(`${e.key}: range exceeds 2^64 - 1`);
      }
      const size = sourceSize(r.source);
      if (size !== undefined && r.offset + r.length > size) {
        reject(`${e.key}: range [${r.offset}, ${r.offset + r.length}) past the end of source ${r.source}`);
      }
      total += r.length;
    }
    if (total > U64_MAX) reject(`${e.key}: value size exceeds 2^64 - 1`);
    if (referenceBlock(e.ranges).payload.length > MAX_PAYLOAD) {
      reject(`${e.key}: reference payload exceeds ${MAX_PAYLOAD} bytes`);
    }
  }
}

class Out {
  chunks: Uint8Array[] = [];
  length = 0;
  push(b: Uint8Array) {
    this.chunks.push(b);
    this.length += b.length;
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

function concat(parts: Uint8Array[]): Uint8Array {
  const out = new Out();
  for (const p of parts) out.push(p);
  return out.concat();
}

function le(fields: [number, 2 | 4 | 8][]): Uint8Array {
  const size = fields.reduce((n, [, w]) => n + w, 0);
  const out = new Uint8Array(size);
  const view = new DataView(out.buffer);
  let at = 0;
  for (const [v, w] of fields) {
    if (w === 2) view.setUint16(at, v, true);
    else if (w === 4) view.setUint32(at, v, true);
    else view.setBigUint64(at, BigInt(v), true);
    at += w;
  }
  return out;
}

/**
 * Writes an archive, or throws InvalidInputError (spec §7
 * <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#7-writing>).
 */
export async function writeVzip(desc: ArchiveDesc): Promise<Uint8Array> {
  validate(desc);
  const mirror = desc.mirror ?? true;
  const built: Built[] = [];
  for (const e of desc.entries) {
    if ("bytes" in e) {
      const body = e.compress ? await deflateRaw(e.bytes) : e.bytes;
      if (e.compress && body.length >= U32_ALL) {
        reject(`${e.key}: a DEFLATE body of 4 GiB or more would make a large entry that is not STORED`);
      }
      built.push({
        name: utf8.encode(e.key),
        method: e.compress ? 8 : 0,
        crc: crc32(e.bytes),
        size: e.bytes.length,
        csize: body.length,
        body,
        extra: new Uint8Array(),
        offset: 0,
      });
    } else {
      const { id, payload } = referenceBlock(e.ranges);
      const body = mirror ? payload : new Uint8Array();
      const extra = new Uint8Array(4 + payload.length);
      extra.set(le([[id, 2], [payload.length, 2]]));
      extra.set(payload, 4);
      built.push({
        name: utf8.encode(e.key),
        method: 0,
        crc: crc32(body),
        size: body.length,
        csize: body.length,
        body,
        extra,
        offset: 0,
      });
    }
  }
  const table = encodeSourceTable(desc.sources, SPEC_REVISION);
  const tableBody = await deflateRaw(table);
  // a format entry is never large (spec §10.1
  // <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#101-structure> rule 7)
  if (table.length >= U32_ALL || tableBody.length >= U32_ALL) reject("the source table reaches 4 GiB");
  const sources: Built = {
    name: utf8.encode(SOURCES_KEY),
    method: 8,
    crc: crc32(table),
    size: table.length,
    csize: tableBody.length,
    body: tableBody,
    extra: new Uint8Array(),
    offset: 0,
  };
  built.push(sources);

  const out = new Out();
  for (const b of built) {
    b.offset = out.length;
    out.push(localHeader(b));
    out.push(b.body);
  }
  const cdOffset = out.length;
  for (const b of built) out.push(centralRecord(b));
  const cdSize = out.length - cdOffset;
  const comment = new Uint8Array(22);
  comment.set(utf8.encode("vzip/0"));
  const view = new DataView(comment.buffer);
  view.setBigUint64(6, BigInt(bodyOffset(sources)), true);
  view.setBigUint64(14, BigInt(sources.csize), true);
  out.push(endRecords(out.length, built.length, cdOffset, cdSize, comment));
  return out.concat();
}
