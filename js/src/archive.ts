// Reading a vzip archive held in memory (spec/bindings/vzip.md §10
// <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#10-zip-container>, §11
// <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#11-entries>, §6
// <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#6-reading>, §13
// <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#13-reading-a-vzip-archive>).

import { crc32c, inflateRaw, InflateLimitError } from "./deflate.ts";
import { checkedFetch } from "#net";
import { type CheckedFetch, HttpResolutionError, type ReadOptions, readHttpRange } from "./http.ts";
import {
  decodeReference,
  decodeTable,
  MalformedError,
  rangeSize,
  type Range,
  type Source,
} from "./protobuf.ts";
import { getScheme, resolveReference } from "./uri.ts";
import { CONCAT_ID, INDEX_KEY, RANGE_ID, SOURCES_KEY } from "./writer.ts";

/**
 * The six classes of error (spec §6.4 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#64-errors>),
 * by the names readers report them by.
 */
export type ErrorClass =
  | "archive"
  | "entry"
  | "body"
  | "payload"
  | "resolution"
  | "request";

/**
 * An error of one of the six classes of spec §6.4
 * <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#64-errors>: `Archive.open` throws an
 * "archive" error, and `Archive.read` the others: "entry" and "body" for a key's own entry,
 * "payload" for a malformed reference, "resolution" for a range it cannot resolve (its source, a
 * pin that fails, a URL the policy refuses, or a request that fails), and "request" for an invalid
 * range or a resource limit. Its message starts `refkv <class> error: `.
 */
export class RefkvError extends Error {
  /** The error's class, as spec §6.5 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#65-error-reporting> has readers report it. */
  errorClass: ErrorClass;
  /**
   * Whether a source is not the one the archive pins (spec §5.1
   * <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#51-pins>): a pin or a range checksum
   * failed, so the source changed since the archive was written.
   */
  sourceChanged: boolean;
  constructor(errorClass: ErrorClass, message: string, sourceChanged = false) {
    super(`refkv ${errorClass} error: ${message}`);
    this.errorClass = errorClass;
    this.sourceChanged = sourceChanged;
  }
}

export interface Entry {
  key: string;
  method: number;
  csize: number;
  size: number;
  bodyOffset: number;
  reference?: { headerId: number; payload: Uint8Array };
  /** The CRC-32C of a bytes value, where the binding records one (a phkv record does). */
  crc32c?: number;
  /**
   * Set if the record has an entry error (spec §11.1
   * <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#111-classification>, §10.2
   * <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#102-zip64>).
   */
  error?: string;
}

/**
 * Reads bytes `[start, end)` of an http(s) object (replaceable in tests).
 * `size` is the object's size, if the response gives it; `hidden` says that
 * the response hid the headers that would (a cross-origin browser read).
 */
export type RangeFetcher = (
  url: string,
  start: number,
  end: number,
  pins: Pick<Source, "size" | "etag" | "modifiedNotAfter">,
  options?: ReadOptions,
) => Promise<{ data: Uint8Array; size: number | undefined; hidden?: boolean }>;

/**
 * What the reader may resolve, and its limits (spec §6.7
 * <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>). Every member is
 * optional; `DEFAULT_POLICY` gives the defaults.
 *
 * - `schemes`: schemes allowed besides those rule 1 allows by default
 *   (`http` and `https`), such as `s3`. It may not name `file`: this reader
 *   reads only http(s) sources, so it refuses every `file:` source.
 * - `prefixes`: if given, a URL is allowed if and only if it matches one of
 *   them (rule 2), whatever its scheme. Rule 3 still applies.
 * - `allowPrivateHosts`: UNSAFE. Lets the archive reach loopback, private,
 *   link-local and other special hosts (rule 3), which every archive, local
 *   or remote, is otherwise refused. In a browser this reader sees host
 *   names as written, not the addresses they resolve to, and Private
 *   Network Access covers the rest; under Node it checks the addresses it
 *   connects to (`#net`).
 * - `allowUncheckedProxy`: UNSAFE. Under Node, lets requests go through the
 *   proxy that `NODE_USE_ENV_PROXY` and `HTTP_PROXY` / `HTTPS_PROXY` ask
 *   for while rule 3 applies; the reader then checks only what the name
 *   resolves to locally. Without it, such a request is refused.
 * - `maxSources`, `maxReads`, `maxGetBytes`, `maxFormatEntry`: rules 4 to 6 (rule 5 bounds the
 *   reads one read of a value makes, and the bytes it takes: those it fetches, the bytes between
 *   coalesced ranges included and, once a read, each `key` source's value (which it reads whole),
 *   and those it builds the value from).
 * - `maxCoalesceGap`: the most bytes between two ranges of a source that the reader combines into
 *   one read, which it then fetches but returns to no value (spec §6.3
 *   <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#63-resolving-a-reference>,
 *   **Coalescing**). Within one read of a value the closest ranges are combined first, and only
 *   while what it fetches stays within `maxGetBytes`; 0 combines only ranges that touch.
 * - `maxReadAmplification`: how many times the bytes they ask for the reads of different values
 *   combined into one request may fetch, plus `maxCoalesceGap`: more saves requests, and fetches
 *   bytes no value returns.
 * - `unverifiablePins`: skip pins whose headers a cross-origin response hides
 *   (rule 7). Range checksums are still checked.
 * - `allowSourceCredentials`: UNSAFE. In a browser, reads `url` sources on the
 *   page's own origin with the user's cookies and HTTP authentication
 *   (`fetch`'s `credentials: "same-origin"`). Without it every source is read
 *   as an anonymous client (`"omit"`): the store's author, not the user,
 *   chose those URLs, and the bytes go to whoever reads the store. The
 *   archive's own URL is the application's choice, and is read as it reads it.
 */
export interface Policy {
  schemes?: string[];
  prefixes?: string[];
  allowPrivateHosts?: boolean;
  allowUncheckedProxy?: boolean;
  maxSources?: number;
  maxReads?: number;
  maxGetBytes?: number;
  maxReadAmplification?: number;
  maxCoalesceGap?: number;
  maxFormatEntry?: number;
  unverifiablePins?: boolean;
  allowSourceCredentials?: boolean;
}

export const DEFAULT_POLICY: Required<Omit<Policy, "prefixes">> & Pick<Policy, "prefixes"> = {
  schemes: [],
  allowPrivateHosts: false,
  allowUncheckedProxy: false,
  maxSources: 1 << 22,
  maxReads: 1024,
  maxGetBytes: 2 ** 31,
  maxReadAmplification: 4,
  maxCoalesceGap: 1 << 16,
  maxFormatEntry: 256 << 20,
  unverifiablePins: false,
  allowSourceCredentials: false,
};

/**
 * The scheme of an absolute URL, lowercase, with https counted as http (§6.7
 * <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy> rule 1).
 */
function schemeOf(url: string): string {
  const i = url.indexOf(":");
  const s = i < 0 ? "" : url.slice(0, i).toLowerCase();
  return s === "https" ? "http" : s;
}

/**
 * Does `url` match `prefix` (spec §6.7
 * <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy> rule 2)?
 */
export function matchesPrefix(url: string, prefix: string): boolean {
  if (!url.startsWith(prefix)) return false;
  const rest = url.slice(prefix.length);
  if (!(prefix.endsWith("/") || rest === "" || "/?#".includes(rest[0]))) return false;
  const path = url.split(/[?#]/, 1)[0];
  return !/%(2[eEfF]|5[cC])/.test(path);
}

/**
 * The class of an IP address (spec §6.7
 * <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy> rule 3).
 */
export type AddressClass = "loopback" | "private" | "link-local" | "special" | "public";

function parseIPv4(s: string): bigint | undefined {
  const m = s.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
  if (!m) return undefined;
  const parts = m.slice(1).map(Number);
  if (parts.some((p) => p > 255)) return undefined;
  return parts.reduce((a, p) => (a << 8n) | BigInt(p), 0n);
}

function parseIPv6(s: string): bigint | undefined {
  let text = s.split("%", 1)[0];
  const tail: number[] = [];
  const dotted = text.match(/^(.*:)(\d+\.\d+\.\d+\.\d+)$/);
  if (dotted) {
    const v4 = parseIPv4(dotted[2]);
    if (v4 === undefined) return undefined;
    tail.push(Number(v4 >> 16n), Number(v4 & 0xffffn));
    text = dotted[1].endsWith("::") ? dotted[1] : dotted[1].slice(0, -1);
  }
  const halves = text.split("::");
  if (halves.length > 2) return undefined;
  const groups = (h: string) => (h === "" ? [] : h.split(":"));
  const head = groups(halves[0]);
  const rest = halves.length === 2 ? groups(halves[1]) : [];
  if (![...head, ...rest].every((g) => /^[0-9a-fA-F]{1,4}$/.test(g))) return undefined;
  const n = head.length + rest.length + tail.length;
  if (halves.length === 1 ? n !== 8 : n > 7) return undefined;
  const words = [
    ...head.map((g) => parseInt(g, 16)),
    ...Array(8 - n).fill(0),
    ...rest.map((g) => parseInt(g, 16)),
    ...tail,
  ];
  return words.reduce((a, w) => (a << 16n) | BigInt(w), 0n);
}

function nets(version: 4 | 6, list: string[]): [bigint, number][] {
  return list.map((n) => {
    const [addr, len] = n.split("/");
    return [(version === 4 ? parseIPv4(addr) : parseIPv6(addr))!, Number(len)];
  });
}

const CLASSES: [AddressClass, [bigint, number][], [bigint, number][]][] = [
  ["loopback", nets(4, ["127.0.0.0/8"]), nets(6, ["::1/128"])],
  ["private", nets(4, ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]), nets(6, ["fc00::/7"])],
  ["link-local", nets(4, ["169.254.0.0/16"]), nets(6, ["fe80::/10"])],
  [
    "special",
    nets(4, [
      "0.0.0.0/8", "100.64.0.0/10", "192.0.0.0/24", "192.0.2.0/24", "198.18.0.0/15",
      "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/3",
    ]),
    nets(6, [
      "::/8", "64:ff9b:1::/48", "100::/63", "2001::/23", "2001:db8::/32", "3fff::/20",
      "5f00::/16", "fec0::/10", "ff00::/8",
    ]),
  ],
];

const within = (a: bigint, bits: number, [net, len]: [bigint, number]) =>
  a >> BigInt(bits - len) === net >> BigInt(bits - len);

function classOf(a: bigint, version: 4 | 6): AddressClass {
  if (version === 6) {
    // IPv4 embedded in IPv6 (mapped, NAT64, 6to4) has the IPv4 address's class.
    if (a >> 32n === 0xffffn || a >> 32n === 0x64ff9b0000000000000000n) return classOf(a & 0xffffffffn, 4);
    if (a >> 112n === 0x2002n) return classOf((a >> 80n) & 0xffffffffn, 4);
  }
  for (const [c, v4, v6] of CLASSES) {
    if ((version === 4 ? v4 : v6).some((n) => within(a, version === 4 ? 32 : 128, n))) return c;
  }
  return "public";
}

/**
 * The class of an IP address in text form (spec §6.7
 * <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy> rule 3), or undefined
 * if it is not one.
 */
export function addressClass(address: string): AddressClass | undefined {
  const v4 = parseIPv4(address);
  if (v4 !== undefined) return classOf(v4, 4);
  const v6 = address.includes(":") ? parseIPv6(address) : undefined;
  return v6 === undefined ? undefined : classOf(v6, 6);
}

/**
 * The class of a URL's host as written (spec §6.7
 * <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy> rule 3): an IP
 * literal's class (the URL parser normalizes decimal, octal and hexadecimal IPv4
 * forms), loopback for `localhost` and names under `.localhost`, and
 * undefined for any other name.
 */
function hostClass(url: string): AddressClass | undefined {
  let host: string;
  try {
    host = new URL(url).hostname.toLowerCase().replace(/\.$/, "");
  } catch {
    return undefined;
  }
  if (host === "localhost" || host.endsWith(".localhost")) return "loopback";
  if (host.startsWith("[") && host.endsWith("]")) host = host.slice(1, -1);
  return addressClass(host);
}

/**
 * Throws unless `policy` is well formed: `schemes` may not name `file` (§6.7
 * <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy> rule 1).
 */
export function validatePolicy(policy: Policy): void {
  if ((policy.schemes ?? []).some((s) => s.toLowerCase() === "file")) {
    throw new TypeError(
      "Policy.schemes may not name 'file': this reader reads only http(s) sources",
    );
  }
  for (const name of ["maxSources", "maxReads", "maxGetBytes", "maxCoalesceGap", "maxFormatEntry"] as const) {
    const v = policy[name];
    if (name in policy && !(Number.isSafeInteger(v) && (v as number) >= 0)) {
      throw new TypeError(`Policy.${name} is a natural number, not ${String(v)}`);
    }
  }
  const a = policy.maxReadAmplification;
  if ("maxReadAmplification" in policy && !(typeof a === "number" && Number.isFinite(a) && a >= 0)) {
    throw new TypeError(`Policy.maxReadAmplification is a finite number of at least 0, not ${String(a)}`);
  }
}

/**
 * Throws a resolution error unless `policy` allows `url` for an archive at `base` (§6.7
 * <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>).
 */
export function checkPolicy(policy: Policy, _base: string, url: string): void {
  validatePolicy(policy);
  const scheme = schemeOf(url);
  if (policy.prefixes !== undefined) {
    if (!policy.prefixes.some((p) => matchesPrefix(url, p))) {
      throw new RefkvError("resolution", `${url}: not under an allowed URL prefix (spec §6.7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>)`);
    }
  } else if (scheme === "file") {
    throw new RefkvError("resolution", `${url}: this reader reads no local file (spec §6.7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>)`);
  } else if (scheme !== "http" && !(policy.schemes ?? []).some((s) => schemeOf(`${s}:`) === scheme)) {
    throw new RefkvError("resolution", `${url}: the scheme '${scheme}' is not allowed (spec §6.7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>)`);
  }
  if (scheme !== "http") return;
  // Rule 3, on the host as written; checkAddress applies it to resolved addresses.
  const c = hostClass(url);
  if (c !== undefined) checkClass(policy, url, c);
}

function checkClass(policy: Policy, url: string, c: AddressClass, address?: string): void {
  if (c === "public" || policy.allowPrivateHosts) return;
  const at = address === undefined ? "" : ` (at ${address})`;
  throw new RefkvError(
    "resolution",
    `${url}: the host${at} is a ${c} address, which the reader does not reach without allowPrivateHosts: true (spec §6.7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>)`,
  );
}

/**
 * Throws a resolution error unless rule 3 of §6.7
 * <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy> lets the reader
 * connect to `address` for `url`.
 */
export function checkAddress(policy: Policy, url: string, address: string): void {
  const c = addressClass(address);
  if (c === undefined) throw new RefkvError("resolution", `${url}: ${address} is not an IP address`);
  checkClass(policy, url, c, address);
}

/**
 * The default of the policy's `maxCoalesceGap`: reads of the same url source at most this far
 * apart are combined into one request (spec §5.2 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#52-reading-over-http>
 * allows it). A Concat of many short ranges, such as one
 * per image row, would otherwise cost one request per range.
 */
export const MERGE_GAP = 1 << 16;

/**
 * The reads of different values that wait at the same time merge as well, under that gap. They
 * only gain speed, where one value's reads must also stay within `maxReads`, so a run has limits
 * of its own: the policy's `maxReadAmplification` times the bytes it asks for, plus a gap, and
 * the bytes waiting over this many connections, between these lengths, so that a batch that
 * reads much keeps connections busy.
 */
const MERGE_CONNECTIONS = 8;
const MERGE_MIN_RUN = 1 << 20;
const MERGE_MAX_RUN = 32 << 20;
/** How long, in milliseconds, a read waits for the reads near it. */
export const MERGE_WINDOW = 2;
/**
 * The longest, in milliseconds, a read waits for a request near it, so that a stalled or very long
 * request does not stall the reads beside it.
 */
export const MERGE_MAX_WAIT = 1000;
/**
 * A read waits for a request near it only while a source has this many requests in flight. Fewer
 * leave connections free, where waiting only costs time: a client that limits its own reads would
 * spend its turns waiting. More are queued by the browser on HTTP/1.1, at 6 connections per host,
 * where they can no longer merge. A lower limit reads faster on HTTP/1.1 and a higher one on
 * HTTP/2, which has no such queue; this one lost the least across both when measured.
 */
export const MERGE_BUSY = 16;

/** A read of bytes `[offset, end)` of a url source, waiting to be served by one request with the reads near it. */
type Waiting = {
  offset: bigint;
  end: bigint;
  resolve: (data: Uint8Array) => void;
  reject: (reason: unknown) => void;
  /** When it began to wait, in milliseconds. */
  since: number;
  /** The value it reads for, whose reads its own coalescing already combined (`coalescedRuns`). */
  owner?: object;
};

/** A request in flight, and the reads it serves. */
type Serving = { offset: bigint; end: bigint; reads: Waiting[] };

/**
 * The reads of one value, as the runs that one request each serves, in order of source and
 * offset: reads that overlap or touch are one run, and of the gaps between runs of one source at
 * most `gap` bytes, the narrowest are closed first, while the runs fetch at most `budget` bytes, so
 * that the value makes as few requests as its budget allows (spec §6.3
 * <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#63-resolving-a-reference>,
 * **Coalescing**).
 */
export function coalescedRuns(
  reads: { source: number; offset: bigint; end: bigint }[],
  gap: number,
  budget: number,
): { source: number; offset: bigint; end: bigint }[] {
  const sorted = [...reads].sort((x, y) =>
    x.source - y.source || (x.offset < y.offset ? -1 : x.offset > y.offset ? 1 : 0));
  const runs: { source: number; offset: bigint; end: bigint }[] = [];
  for (const r of sorted) {
    const last = runs[runs.length - 1];
    if (last && last.source === r.source && r.offset <= last.end) {
      if (r.end > last.end) last.end = r.end;
    } else {
      runs.push({ ...r });
    }
  }
  let fetched = runs.reduce((n, run) => n + Number(run.end - run.offset), 0);
  const gaps: [number, number][] = [];
  for (let i = 0; i + 1 < runs.length; i++) {
    const width = Number(runs[i + 1].offset - runs[i].end);
    if (runs[i].source === runs[i + 1].source && width <= gap) gaps.push([width, i]);
  }
  gaps.sort((x, y) => x[0] - y[0] || x[1] - y[1]);
  const closed = new Set<number>();
  for (const [width, i] of gaps) {
    if (fetched + width > budget) break;
    fetched += width;
    closed.add(i);
  }
  const out: { source: number; offset: bigint; end: bigint }[] = [];
  runs.forEach((run, i) => {
    if (closed.has(i - 1)) out[out.length - 1].end = run.end;
    else out.push({ ...run });
  });
  return out;
}

/**
 * `reads` of one source, as the runs that one request each serves, in order of offset. A read
 * joins the run before it if it starts at most `gap` bytes after the run's end, and the run then
 * fetches at most `amplification` times the bytes its reads ask for, plus `gap`, and at most
 * `longest` bytes. A read never joins a run within `gap` of the end of a read of the same value
 * (`owner`) in it, unless it touches or overlaps the run: those are reads its value's own coalescing
 * could have combined and kept apart, within what it may fetch. A read is never cut, so a longer one
 * is a run of its own.
 */
export function mergedRuns<T extends { offset: bigint; end: bigint; owner?: object }>(
  reads: T[],
  longest: bigint,
  amplification: number = DEFAULT_POLICY.maxReadAmplification,
  gap: number = DEFAULT_POLICY.maxCoalesceGap,
): T[][] {
  const big = (v: bigint, w: bigint) => (v > w ? v : w);
  const sorted = [...reads].sort((x, y) =>
    x.offset < y.offset ? -1 : x.offset > y.offset ? 1 : x.end < y.end ? -1 : x.end > y.end ? 1 : 0);
  const runs: T[][] = [];
  let start = 0n;
  let end = 0n;
  let asked = 0n;
  let owners = new Map<object, bigint>(); // where each value's last read in the run ends
  for (const read of sorted) {
    const fresh = big(read.end - big(read.offset, end), 0n);
    const span = big(end, read.end) - start;
    const own = read.owner === undefined ? undefined : owners.get(read.owner);
    if (
      runs.length > 0 &&
      (own === undefined || read.offset <= end || read.offset - own > BigInt(gap)) &&
      read.offset - end <= BigInt(gap) &&
      span <= longest &&
      Number(span - BigInt(gap)) <= amplification * Number(asked + fresh)
    ) {
      runs[runs.length - 1].push(read);
      asked += fresh;
      end = big(end, read.end);
    } else {
      runs.push([read]);
      owners = new Map();
      start = read.offset;
      end = read.end;
      asked = read.end - read.offset;
    }
    if (read.owner !== undefined) owners.set(read.owner, big(owners.get(read.owner) ?? 0n, read.end));
  }
  return runs;
}

const U32_ALL = 0xffffffff;
const strictUtf8 = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });

function safe(v: bigint, what: string): number {
  if (v > BigInt(Number.MAX_SAFE_INTEGER)) {
    throw new RefkvError("archive", `${what} is too large`);
  }
  return Number(v);
}

function parseExtra(extra: Uint8Array, view: DataView, base: number) {
  const blocks: { id: number; data: Uint8Array }[] = [];
  let at = 0;
  while (at < extra.length) {
    if (at + 4 > extra.length) return undefined;
    const id = view.getUint16(base + at, true);
    const n = view.getUint16(base + at + 2, true);
    if (at + 4 + n > extra.length) return undefined;
    blocks.push({ id, data: extra.subarray(at + 4, at + 4 + n) });
    at += 4 + n;
  }
  return blocks;
}

/**
 * A vzip archive (spec/bindings/vzip.md), opened from the bytes of its tail by `Archive.open`:
 * its source table and its entries, whose values `read` gives, resolving references under the
 * archive's reader policy. An archive never changes once opened.
 */
export class Archive {
  bytes: Uint8Array;
  baseUrl: string;
  sources: Source[];
  entries: Map<string, Entry>;
  /**
   * The spec revision the writer followed, if the archive records one (spec §1.3
   * <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#13-versioning>).
   */
  revision: number | undefined;
  policy: typeof DEFAULT_POLICY;
  /** The largest reference payload the binding allows (a ZIP extra field's, here). */
  protected maxPayload = 65519;
  private fetchRange: RangeFetcher;
  /** Sends this archive's requests, checking addresses (rule 3), where the platform allows. */
  private netFetch: CheckedFetch | undefined;
  /** The reads of url sources waiting to merge, by source. */
  private waiting = new Map<number, Waiting[]>();
  /** The requests in flight, by source. */
  private serving = new Map<number, Set<Serving>>();
  private flushPending = false;
  /** Set while some reads whose window has ended wait for a request to end. */
  private release: ReturnType<typeof setTimeout> | undefined;

  protected constructor(
    bytes: Uint8Array,
    baseUrl: string,
    sources: Source[],
    entries: Map<string, Entry>,
    fetchRange: RangeFetcher,
    policy: typeof DEFAULT_POLICY,
    revision: number | undefined,
  ) {
    this.bytes = bytes;
    this.baseUrl = baseUrl;
    this.sources = sources;
    this.entries = entries;
    this.fetchRange = fetchRange;
    this.policy = policy;
    this.revision = revision;
    this.netFetch = policy.allowPrivateHosts
      ? undefined
      : checkedFetch((url, address) => checkAddress(policy, url, address), policy);
  }

  /**
   * Opens an archive (spec §6.1
   * <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#61-opening>); `baseUrl` resolves
   * relative `url` sources.
   * `policy` (spec §6.7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>)
   * defaults to `DEFAULT_POLICY`, which allows http(s)
   * sources on public hosts, and for a local archive (a `file:` `baseUrl`) also `file`
   * sources. Loopback and private hosts are refused for every archive, local or not,
   * unless the policy has `allowPrivateHosts: true`. Throws a `RefkvError` of class
   * "archive" for an archive opening refuses.
   */
  static async open(
    bytes: Uint8Array,
    baseUrl: string,
    fetchRange: RangeFetcher = (url, start, end, pins, options) =>
      readHttpRange(url, start, end, pins, undefined, options),
    policy: Policy = {},
  ): Promise<Archive> {
    validatePolicy(policy);
    const rules = { ...DEFAULT_POLICY, ...policy };
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const n = bytes.length;
    let eocd = -1;
    for (const commentLength of [38, 22]) {
      const at = n - 22 - commentLength;
      if (
        at >= 0 &&
        view.getUint32(at, true) === 0x06054b50 &&
        view.getUint16(at + 20, true) === commentLength
      ) {
        eocd = at;
        break;
      }
    }
    if (eocd < 0) throw new RefkvError("archive", "not a vzip archive");
    const comment = bytes.subarray(eocd + 22);
    const magic = new TextDecoder().decode(comment.subarray(0, 6));
    if (!magic.startsWith("vzip/")) {
      throw new RefkvError("archive", "not a vzip archive");
    }
    if (magic !== "vzip/0") {
      throw new RefkvError("archive", `unsupported version ${magic}`);
    }
    // spec §10.2 <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#102-zip64>: the directory's size and offset always come
    // from the zip64 record; the end record's counts, size and offset, and the zip64
    // record's counts, are ignored
    const loc = eocd - 20;
    if (loc < 0 || view.getUint32(loc, true) !== 0x07064b50) {
      throw new RefkvError("archive", "no zip64 locator before the end of central directory record");
    }
    const at64 = safe(view.getBigUint64(loc + 8, true), "zip64 record offset");
    if (
      at64 + 56 > n ||
      view.getUint32(at64, true) !== 0x06064b50 ||
      view.getBigUint64(at64 + 4, true) !== 44n
    ) {
      throw new RefkvError("archive", "invalid zip64 end of central directory");
    }
    const cdSize = view.getBigUint64(at64 + 40, true);
    const cdOffset = view.getBigUint64(at64 + 48, true);
    const cdStart = safe(cdOffset, "central directory offset");
    const cdEnd = cdStart + safe(cdSize, "central directory size");
    if (cdEnd > n) throw new RefkvError("archive", "central directory outside the file");

    const entries = new Map<string, Entry>();
    let at = cdStart;
    while (at < cdEnd) {
      if (at + 46 > cdEnd || view.getUint32(at, true) !== 0x02014b50) {
        throw new RefkvError("archive", `bad central directory record at ${at}`);
      }
      const flags = view.getUint16(at + 8, true);
      const method = view.getUint16(at + 10, true);
      let csize = view.getUint32(at + 20, true);
      let size = view.getUint32(at + 24, true);
      const nameLen = view.getUint16(at + 28, true);
      const extraLen = view.getUint16(at + 30, true);
      const commentLen = view.getUint16(at + 32, true);
      let offset: number | bigint = view.getUint32(at + 42, true);
      const end = at + 46 + nameLen + extraLen + commentLen;
      if (end > cdEnd) throw new RefkvError("archive", "truncated central directory");
      const nameBytes = bytes.subarray(at + 46, at + 46 + nameLen);
      const extraStart = at + 46 + nameLen;
      const extra = bytes.subarray(extraStart, extraStart + extraLen);
      at = end;
      let key: string;
      try {
        key = strictUtf8.decode(nameBytes);
      } catch {
        continue; // names no key (spec §10.3 <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#103-file-names>)
      }
      if (key === "") continue;
      if (entries.has(key)) throw new RefkvError("archive", `duplicate key ${key}`);
      const entry: Entry = { key, method, csize, size, bodyOffset: 0 };
      entries.set(key, entry);
      const blocks = parseExtra(extra, view, extraStart);
      const refs = blocks?.filter((b) => b.id === RANGE_ID || b.id === CONCAT_ID) ?? [];
      const zip64 = blocks?.filter((b) => b.id === 0x0001) ?? [];
      // spec §10.2 <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#102-zip64>: a large entry's sizes, then an
      // offset that does not fit its field, come from the one ZIP64 block, in that order
      const large = size === U32_ALL && csize === U32_ALL;
      const need = (large ? 16 : 0) + (offset === U32_ALL ? 8 : 0);
      if (blocks === undefined) entry.error = "unparseable extra field";
      else if (refs.length > 1) entry.error = "more than one reference block";
      else if (zip64.length > 1) entry.error = "more than one zip64 block";
      else if ((size === U32_ALL) !== (csize === U32_ALL)) {
        entry.error = "exactly one size field is 0xFFFFFFFF";
      } else if (need > 0) {
        if (zip64.length === 0 || zip64[0].data.length < need) {
          entry.error = "zip64 block missing or too short";
        } else {
          const z = new DataView(zip64[0].data.buffer, zip64[0].data.byteOffset, need);
          if (large) {
            size = safe(z.getBigUint64(0, true), "entry size");
            csize = safe(z.getBigUint64(8, true), "entry size");
          }
          if (offset === U32_ALL) offset = z.getBigUint64(large ? 16 : 0, true);
        }
      }
      if (entry.error === undefined && method !== 0 && method !== 8) {
        entry.error = `compression method ${method}`;
      } else if (entry.error === undefined && flags & 1) {
        entry.error = "encrypted";
      } else if (entry.error === undefined && large && method !== 0) {
        // spec §10.1 <https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#101-structure> rule 7
        entry.error = "large entry is not STORED";
      }
      if (entry.error === undefined && refs.length === 1) {
        if (method !== 0) entry.error = "reference entry with method 8";
        else if (large) entry.error = "reference entry is large";
        entry.reference = { headerId: refs[0].id, payload: refs[0].data };
      }
      entry.size = size;
      entry.csize = csize;
      entry.bodyOffset =
        (typeof offset === "bigint" ? Number(offset) : offset) + 30 + nameLen + (large ? 20 : 0);
    }

    const cview = new DataView(comment.buffer, comment.byteOffset, comment.byteLength);
    const sOffset = safe(cview.getBigUint64(6, true), "sources offset");
    const sSize = safe(cview.getBigUint64(14, true), "sources size");
    if (sOffset + sSize > n) throw new RefkvError("archive", "source table outside the file");
    let table: { sources: Source[]; revision?: number };
    try {
      table = decodeTable(
        await inflateRaw(bytes.subarray(sOffset, sOffset + sSize), rules.maxFormatEntry),
      );
    } catch (e) {
      const why = e instanceof InflateLimitError
        ? `${e.message}; this reader allows ${rules.maxFormatEntry} (spec §6.7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>)`
        : (e as Error).message;
      throw new RefkvError("archive", `source table: ${why}`);
    }
    if (table.sources.length > rules.maxSources) {
      throw new RefkvError(
        "archive",
        `the source table has ${table.sources.length} sources; this reader allows ${rules.maxSources}`,
      );
    }
    return new Archive(bytes, baseUrl, table.sources, entries, fetchRange, rules, table.revision);
  }

  /**
   * The visible entry for `key`, or undefined (spec §6.2
   * <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#62-operations>).
   */
  lookup(key: string): Entry | undefined {
    if (key.startsWith("__vz__/")) return undefined;
    const entry = this.entryFor(key);
    if (entry?.error !== undefined) {
      throw new RefkvError("entry", `${JSON.stringify(key)}: ${entry.error}`);
    }
    return entry;
  }

  /** Whether `key` names a format entry, which no `key` source may name: vzip's (spec/bindings/vzip.md). */
  protected namesFormatEntry(key: string): boolean {
    return key === SOURCES_KEY || key === INDEX_KEY;
  }

  /** The entry of `key` as the container has it, hidden or not: a binding finds keys here. */
  protected entryFor(key: string): Entry | undefined {
    return this.entries.get(key);
  }

  /** Visible keys, in UTF-8 order. */
  keys(): string[] {
    return [...this.entries.keys()]
      .filter((k) => !k.startsWith("__vz__/"))
      .sort((a, b) => compareUtf8(a, b));
  }

  ranges(entry: Entry): Range[] {
    try {
      return decodeReference(
        entry.reference!.headerId,
        entry.reference!.payload,
        this.sources.length,
        this.maxPayload,
      );
    } catch (e) {
      if (!(e instanceof MalformedError)) throw e;
      throw new RefkvError("payload", `${JSON.stringify(entry.key)}: ${e.message}`);
    }
  }

  /** Size of an entry's value. */
  size(entry: Entry): number {
    if (entry.reference === undefined) return entry.size;
    const total = this.ranges(entry).reduce((n, r) => n + rangeSize(r), 0n);
    if (total > BigInt(Number.MAX_SAFE_INTEGER)) {
      throw new RefkvError("request", "value is too large for this reader");
    }
    return Number(total);
  }

  protected async body(entry: Entry): Promise<Uint8Array> {
    const end = entry.bodyOffset + entry.csize;
    if (end > this.bytes.length) {
      throw new RefkvError("body", `${entry.key}: body outside the file`);
    }
    const stored = this.bytes.subarray(entry.bodyOffset, end);
    let body = stored;
    if (entry.method === 8) {
      try {
        body = await inflateRaw(stored, entry.size); // spec §6.7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy> rule 6
      } catch (e) {
        throw new RefkvError("body", `${entry.key}: ${(e as Error).message}`);
      }
    } else if (entry.csize !== entry.size) {
      throw new RefkvError("body", `${entry.key}: STORED sizes differ`);
    }
    if (body.length !== entry.size) {
      throw new RefkvError("body", `${entry.key}: inflates to ${body.length} bytes`);
    }
    if (entry.crc32c !== undefined && crc32c(body) !== entry.crc32c) {
      throw new RefkvError("body", `${entry.key}: the value does not match its crc32c`);
    }
    return body;
  }

  /**
   * The bytes of `r`. A `key` source's value is read whole, once a read: `keyValues` holds the
   * values one read has read (spec §5 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#5-source-table>).
   */
  private async source(
    r: Extract<Range, { source: number }>,
    keyValues = new Map<string, Promise<Uint8Array>>(),
  ): Promise<Uint8Array> {
    const s = this.sources[r.source];
    const start = r.offset;
    const end = r.offset + r.length;
    const fail = (m: string, sourceChanged = false): never => {
      throw new RefkvError("resolution", `source ${r.source}: ${m}`, sourceChanged);
    };
    if (s.data !== undefined) {
      if (end > BigInt(s.data.length)) fail("range past the end of the data");
      return s.data.subarray(Number(start), Number(end));
    }
    if (s.key !== undefined) {
      if (this.namesFormatEntry(s.key)) fail(`key ${JSON.stringify(s.key)} names a format entry`);
      const target = this.entryFor(s.key);
      if (target === undefined || target.reference !== undefined || target.error) {
        fail(`key ${JSON.stringify(s.key)} is not a bytes entry`);
      }
      let pending = keyValues.get(s.key);
      if (pending === undefined) {
        pending = this.body(target!);
        keyValues.set(s.key, pending);
      }
      let value: Uint8Array;
      try {
        value = await pending;
      } catch (e) {
        // spec §6.4 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#64-errors>: a range of a broken entry
        return fail(`key ${JSON.stringify(s.key)}: ${(e as Error).message}`);
      }
      if (end > BigInt(value.length)) fail("range past the end of the key's value");
      return value.subarray(Number(start), Number(end));
    }
    let url: string;
    try {
      url = resolveReference(this.baseUrl, s.url!);
    } catch (e) {
      return fail((e as Error).message);
    }
    checkPolicy(this.policy, this.baseUrl, url); // spec §6.7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>
    const scheme = getScheme(url);
    if (scheme !== "http" && scheme !== "https") fail(`unsupported scheme in ${url}`);
    try {
      const { data, size, hidden } = await this.fetchRange(
        url,
        safe(start, "offset"),
        safe(end, "end"),
        s,
        {
          allow: (u) => checkPolicy(this.policy, this.baseUrl, u),
          unverifiablePins: this.policy.unverifiablePins,
          sendCredentials: this.policy.allowSourceCredentials,
          ...(this.netFetch ? { fetch: this.netFetch } : {}),
        },
      );
      const unseen = size === undefined && hidden === true && this.policy.unverifiablePins;
      if (s.size !== undefined && !unseen && BigInt(size ?? -1) !== s.size) {
        fail(
          size === undefined
            ? `${url}: the archive pins the size ${s.size}, which the response does not give`
            : `${url}: the source is ${size} bytes, but the archive pins ${s.size}: the source ` +
                "changed since the archive was written",
          size !== undefined,
        );
      }
      return data;
    } catch (e) {
      if (e instanceof HttpResolutionError) fail(e.message, e.sourceChanged);
      throw e;
    }
  }

  /**
   * Bytes of a url source, read with the reads of other values waiting at the same time (spec §6.3
   * <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#63-resolving-a-reference>).
   */
  private sharedRead(source: number, offset: bigint, end: bigint, owner?: object): Promise<Uint8Array> {
    return new Promise((resolve, reject) => {
      if (!this.flushPending) {
        this.flushPending = true;
        setTimeout(() => {
          this.flushPending = false;
          this.flush();
        }, MERGE_WINDOW);
      }
      const reads = this.waiting.get(source);
      const read = { offset, end, resolve, reject, since: Date.now(), owner };
      if (reads) reads.push(read);
      else this.waiting.set(source, [read]);
    });
  }

  /**
   * Starts the requests that serve the reads waiting. While its source is busy (`MERGE_BUSY`), a
   * read near a request in flight waits for a request to end, up to `MERGE_MAX_WAIT`, and merges
   * with the reads that come meanwhile: a client that starts its next chunks as the last ones
   * finish starts them a few at a time, and those are the chunks beside them. Reads far from any
   * request do not wait, so reads that cannot merge are as concurrent as they were. A read inside a
   * request in flight joins it, since its bytes are on their way.
   */
  private flush(): void {
    const now = Date.now();
    clearTimeout(this.release);
    this.release = undefined;
    let oldest = Infinity;
    for (const [source, waiting] of [...this.waiting]) {
      // Not the requests this loop starts, which share out one batch of reads.
      const serving = [...(this.serving.get(source) ?? [])];
      const reads = waiting.filter((r) => !Archive.join(r, serving));
      const bytes = reads.reduce((n, r) => n + (r.end - r.offset), 0n);
      const share = bytes / BigInt(MERGE_CONNECTIONS);
      const floor = BigInt(MERGE_MIN_RUN);
      const ceiling = BigInt(MERGE_MAX_RUN);
      const clamped = share < floor ? floor : share > ceiling ? ceiling : share;
      // a shared request fetches no more than one value's read may take (a longer read alone does)
      const budget = BigInt(this.policy.maxGetBytes);
      const longest = clamped < budget ? clamped : budget;
      const gap = BigInt(this.policy.maxCoalesceGap);
      const held: Waiting[] = [];
      for (const run of mergedRuns(reads, longest, this.policy.maxReadAmplification, this.policy.maxCoalesceGap)) {
        const offset = run[0].offset;
        const end = run.reduce((e, r) => (r.end > e ? r.end : e), offset);
        const near = serving.some((s) => s.offset - end <= gap && offset - s.end <= gap);
        const since = run.reduce((t, r) => Math.min(t, r.since), now);
        if (near && serving.length >= MERGE_BUSY && now - since < MERGE_MAX_WAIT) {
          held.push(...run);
          oldest = Math.min(oldest, since);
        } else {
          void this.serve(source, run);
        }
      }
      if (held.length > 0) this.waiting.set(source, held);
      else this.waiting.delete(source);
    }
    if (oldest < Infinity) {
      this.release = setTimeout(() => this.flush(), oldest + MERGE_MAX_WAIT - now);
    }
  }

  /** Adds `read` to a request in flight that covers it, if there is one. */
  private static join(read: Waiting, serving: Serving[]): boolean {
    const request = serving.find((s) => s.offset <= read.offset && read.end <= s.end);
    request?.reads.push(read);
    return request !== undefined;
  }

  /**
   * Reads `run` in one request, and gives each read its bytes. A run ends where its last read
   * ends, so it may fail where that read alone would; the others must not fail with it, so each
   * read of a run that fails is read on its own, and gets its own result.
   */
  private async serve(source: number, run: Waiting[]): Promise<void> {
    const offset = run[0].offset;
    const end = run.reduce((e, r) => (r.end > e ? r.end : e), offset);
    const serving = this.serving.get(source) ?? new Set<Serving>();
    this.serving.set(source, serving);
    const request = { offset, end, reads: run };
    serving.add(request);
    const finished = () => {
      serving.delete(request);
      if (serving.size === 0) this.serving.delete(source);
      if (this.release !== undefined) this.flush();
    };
    let data: Uint8Array;
    try {
      data = await this.source({ source, offset, length: end - offset });
    } catch (e) {
      // Finished before the reads are made alone, so that no other read joins a request that failed.
      finished();
      if (run.length > 1) await Promise.all(run.map((r) => this.serve(source, [r])));
      else run[0].reject(e);
      return;
    }
    finished();
    for (const r of run) {
      const from = Number(r.offset - offset);
      r.resolve(data.subarray(from, from + Number(r.end - r.offset)));
    }
  }

  /** Bytes `[start, end)` of a visible key's value, or undefined if absent. */
  async read(key: string, start = 0, end?: number): Promise<Uint8Array | undefined> {
    const entry = this.lookup(key);
    if (entry === undefined) return undefined;
    const size = this.size(entry);
    end ??= size;
    if (start < 0 || end < start || end > size) {
      throw new RefkvError("request", `range [${start}, ${end}) of a ${size}-byte value`);
    }
    if (entry.reference === undefined) {
      return (await this.body(entry)).subarray(start, end);
    }
    type Read = { source: number; offset: bigint; length: bigint };
    // Each part is the bytes of a range that overlaps the request. A range with
    // a crc32c is read whole and checked (spec §6.3
    // <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#63-resolving-a-reference>); `cuts`
    // says which part of each part's bytes the request takes, and which checksum they must have.
    // a read of a data or key source is made only once the value passes rule 5, as a url read is
    const parts: (Promise<Uint8Array> | Read | { local: Read })[] = [];
    const cuts: { lo: number; hi: number; crc?: number }[] = [];
    let at = 0;
    let taken = 0; // the bytes the value is built from, each range's each time: a range with a crc32c whole
    // the bytes read to hold each key's value whole, which `source` does once a read to read a
    // range of it: fetched once
    const held = new Map<string, number>();
    for (const r of this.ranges(entry)) {
      if (taken > this.policy.maxGetBytes) break; // rule 5, refused below: nothing is read
      const n = Number(rangeSize(r));
      const a = Math.max(start, at);
      const b = Math.min(end, at + n);
      // Only ranges that overlap the request are resolved.
      if (a < b) {
        if ("data" in r) {
          parts.push(Promise.resolve(r.data.subarray(a - at, b - at)));
          cuts.push({ lo: 0, hi: b - a });
          taken += b - a;
        } else {
          let read: Read;
          if (r.crc32c === undefined) {
            read = { source: r.source, offset: r.offset + BigInt(a - at), length: BigInt(b - a) };
            cuts.push({ lo: 0, hi: b - a });
          } else {
            read = { source: r.source, offset: r.offset, length: r.length };
            cuts.push({ lo: a - at, hi: b - at, crc: r.crc32c });
          }
          taken += Number(read.length);
          const key = this.sources[r.source].key;
          if (key !== undefined && !held.has(key)) {
            let target: Entry | undefined;
            try {
              target = this.entryFor(key);
            } catch (e) {
              // spec §6.4 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#64-errors>: a range of a broken entry
              throw new RefkvError("resolution", `key ${JSON.stringify(key)}: ${(e as Error).message}`);
            }
            const whole = target !== undefined && target.reference === undefined && !target.error &&
              !this.namesFormatEntry(key);
            held.set(key, whole ? Math.max(target!.size, target!.csize) : 0);
          }
          if (taken > this.policy.maxGetBytes) break;
          parts.push(this.sources[r.source].url !== undefined ? read : { local: read });
        }
      }
      at += n;
    }
    // Combine nearby reads of each url source into runs within what the value may fetch besides
    // the key values it holds whole, fetch each run once, and slice the reads back out of it.
    const reads = parts.filter((p): p is Read => !(p instanceof Promise) && !("local" in p));
    let heldBytes = 0;
    for (const n of held.values()) heldBytes += n;
    const runs: { source: number; offset: bigint; end: bigint; data?: Promise<Uint8Array> }[] = coalescedRuns(
      reads.map((r) => ({ source: r.source, offset: r.offset, end: r.offset + r.length })),
      this.policy.maxCoalesceGap,
      Math.max(this.policy.maxGetBytes - heldBytes, 0),
    );
    // each read's run: runs and reads both in order of source and offset
    const runOf = new Map<Read, (typeof runs)[number]>();
    const ordered = [...reads].sort((x, y) =>
      x.source - y.source || (x.offset < y.offset ? -1 : x.offset > y.offset ? 1 : 0));
    let k = 0;
    for (const read of ordered) {
      while (runs[k].source !== read.source || runs[k].end < read.offset + read.length) k++;
      runOf.set(read, runs[k]);
    }
    // spec §6.7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy> rule 5,
    // before any read: the bytes it is built from, and those it fetches, the bytes between
    // coalesced ranges included
    const fetched = runs.reduce((n, run) => n + Number(run.end - run.offset), heldBytes);
    if (Math.max(taken, fetched) > this.policy.maxGetBytes) {
      throw new RefkvError(
        "request",
        `the value needs more than ${this.policy.maxGetBytes} bytes; this reader allows ${this.policy.maxGetBytes} (spec §6.7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>)`,
      );
    }
    if (runs.length > this.policy.maxReads) {
      throw new RefkvError(
        "request",
        `the value needs ${runs.length} reads; this reader allows ${this.policy.maxReads} (spec §6.7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>)`,
      );
    }
    const owner = {}; // this value's reads, which the shared reads keep apart
    const keyValues = new Map<string, Promise<Uint8Array>>(); // each key's value, read once
    for (const run of runs) {
      run.data = this.sharedRead(run.source, run.offset, run.end, owner);
    }
    const whole = await Promise.all(
      parts.map(async (p) => {
        if (p instanceof Promise) return p;
        if ("local" in p) return this.source(p.local, keyValues);
        const run = runOf.get(p)!;
        const from = Number(p.offset - run.offset);
        return (await run.data!).subarray(from, from + Number(p.length));
      }),
    );
    const chunks = whole.map((data, i) => {
      const { lo, hi, crc } = cuts[i];
      if (crc !== undefined && crc32c(data) !== crc) {
        throw new RefkvError(
          "resolution",
          `the bytes read do not match the range's crc32c ${crc} (got ${crc32c(data)}): the ` +
            "source changed since the archive was written",
          true,
        );
      }
      return data.subarray(lo, hi);
    });
    const out = new Uint8Array(end - start);
    let o = 0;
    for (const c of chunks) {
      out.set(c, o);
      o += c.length;
    }
    return out;
  }
}

export function compareUtf8(a: string, b: string): number {
  // Code point order equals UTF-8 byte order.
  const x = [...a];
  const y = [...b];
  for (let i = 0; i < Math.min(x.length, y.length); i++) {
    const d = x[i].codePointAt(0)! - y[i].codePointAt(0)!;
    if (d !== 0) return d;
  }
  return x.length - y.length;
}

export { SOURCES_KEY };
