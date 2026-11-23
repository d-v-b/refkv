// Store inputs (spec/zarrify.md §1.4
// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>–§1.6
// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#16-json-documents>): the listing,
// object reads, the strict JSON reader, and the output of a store profile (one url source per
// chunk object). Shared by the N5 (§8
// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/n5.md#8-n5-profile>), Zarr v2 (§7
// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#7-zarr-v2-profile>) and
// OME-Zarr (§11
// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ome-zarr.md#11-ome-zarr-profile>)
// profiles.

import { checkAddress, checkPolicy, type Policy, RefkvError } from "../archive.ts";
import { checkedFetch } from "#net";
import { checkHttpUrl, isUriReference, resolveReference } from "../uri.ts";
import { CONVENTION_KEY, declare, type Profile, stringifyJson } from "./common.ts";
import type { StoreProfile } from "./ir/store.ts";
import type { ArchiveDesc, EntryDesc } from "../writer.ts";
import { ir, latin1, rule, RuleError } from "./ir/wasm.ts";

/**
 * The store input is rejected for a reason no single store profile owns (§1.4
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>–§1.6
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#16-json-documents>).
 */
export class StoreError extends Error {
  /** The store format that refused the input, once one was chosen (the Rust core's store run:
   * `zarr2`, or `ome-zarr` once its 0.4 test passed); none for a rejection before that. */
  readonly format?: StoreProfile;
  constructor(message?: string, format?: StoreProfile) {
    super(message);
    if (format !== undefined) this.format = format;
  }
}

/**
 * A resource limit of this implementation was exceeded: a failure, not a rejection (§1.2
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>, Appendix A
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#appendix-a-conformance>).
 */
export class StoreLimitError extends Error {}

/**
 * The listing or an object could not be read: a failure (§1.4
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>).
 */
export class StoreReadError extends Error {}

const reject = (message: string): never => {
  throw new StoreError(message);
};

export const MAX_SAFE = Number.MAX_SAFE_INTEGER;
export const MAX_DOCUMENT = 1 << 24;
const MAX_DEPTH = 256;
const utf8 = new TextEncoder();
/**
 * The browser implementation's limit on listed objects (Appendix A
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#appendix-a-conformance>).
 */
export const MAX_OBJECTS = 100000;

/** A host rule of the core (rust/refkv-ir/src/host), a rejection as a StoreError. */
function hostRule<T>(name: string, params: Record<string, unknown>): T {
  try {
    return rule<T>(name, params);
  } catch (e) {
    if (e instanceof RuleError && e.kind === "rejected") throw new StoreError(e.message);
    throw e;
  }
}

/**
 * The URL of the object with relative key `key` (§1.4
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>): the core's rule.
 */
export function objectUrl(storeUrl: string, key: string): string {
  return hostRule("object_url", { store_url: storeUrl, key });
}

/** `objectUrl` of each key, in one call of the core. */
export function objectUrls(storeUrl: string, keys: string[]): string[] {
  return hostRule("object_urls", { store_url: storeUrl, keys });
}

/**
 * `enc` of §1.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>.
 */
export function queryEncode(s: string): string {
  return hostRule("query_encode", { s });
}

/**
 * [scheme, authority, path] of a valid store URL (§1.4
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>), or a rejection.
 */
export function checkStoreUrl(url: string): [string, string, string] {
  return hostRule("check_store_url", { url });
}

/**
 * [endpoint, prefix P] of a store URL (§1.5
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>).
 */
export function listingEndpoint(url: string): [string, string] {
  return hostRule("listing_endpoint", { url });
}

// ---------------------------------------------------------------- Hub stores (§1.5
// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>)

/** The Hugging Face Hub's endpoint, unless the application gives another. */
export const HUB = "https://huggingface.co";

/**
 * The Hub's endpoint `E` (§1.5
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>): `endpoint`, else
 * `HUB`, without a trailing `/`. A TypeError if it is not an http(s) URL with an authority,
 * without userinfo, a query or a fragment.
 */
export function hubEndpoint(endpoint?: string): string {
  try {
    return rule("hub_endpoint", { endpoint: endpoint ?? null });
  } catch (e) {
    if (e instanceof RuleError) throw new TypeError(e.message);
    throw e;
  }
}

/**
 * A Hub store (§1.5
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>): its endpoint,
 * kind, owner, name, revision `r` and directory `D` (empty, or ending in `/`), the last two
 * percent-decoded.
 */
export interface HubRepo {
  endpoint: string;
  kind: "models" | "datasets";
  owner: string;
  name: string;
  revision: string;
  directory: string;
}

function hubUrls(repo: HubRepo, commit: string | null): { revision_url: string; tree_url: string; base: string } {
  const { endpoint, kind, owner, name, revision, directory } = repo;
  return hostRule("hub_urls", { repo: { endpoint, kind, owner, name, revision, directory }, commit });
}

export function hubRevisionUrl(repo: HubRepo): string {
  return hubUrls(repo, null).revision_url;
}

export function hubTreeUrl(repo: HubRepo, commit: string): string {
  return hubUrls(repo, commit).tree_url;
}

/**
 * The store's base (§1.4
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>) at `commit`.
 */
export function hubBase(repo: HubRepo, commit: string): string {
  return hubUrls(repo, commit).base;
}

/**
 * The Hub store that the store URL `url` names under `endpoint` (§1.5
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>), undefined if
 * `url` is not under it, or a rejection if it is but does not name one.
 */
export function hubRepo(url: string, endpoint: string): HubRepo | undefined {
  const r = hostRule<(HubRepo & { revision_url?: string }) | null>("hub_repo", { url, endpoint });
  if (r === null) return undefined;
  const { kind, owner, name, revision, directory } = r;
  return { endpoint: r.endpoint, kind, owner, name, revision, directory };
}

/**
 * Whether the file input `url` would be a Hub store if it ended in `/` (§1.5
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>): a web page
 * that is rejected without being read.
 */
export function hubPage(url: string, endpoint: string): boolean {
  return hostRule("hub_page", { url, endpoint });
}

// ---------------------------------------------------------------- listing bodies (§1.5
// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>)

/**
 * The objects listed by one ListObjectsV2 response, and the continuation token if truncated (§1.5
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>).
 */
export function parseListing(body: Uint8Array, prefix: string): { objects: [string, number][]; token?: string } {
  const r = hostRule<{ objects: [string, number][]; token: string | null }>("parse_listing", { body: latin1(body), prefix });
  return r.token === null ? { objects: r.objects } : { objects: r.objects, token: r.token };
}

// ---------------------------------------------------------------- JSON (§1.6
// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#16-json-documents>)

export type Json = null | boolean | number | string | Json[] | { [k: string]: Json };

function checkNumbers(v: unknown): void {
  if (typeof v === "number") {
    if (!Number.isFinite(v)) reject("JSON number is not finite in binary64");
  } else if (Array.isArray(v)) {
    for (const x of v) checkNumbers(x);
  } else if (v !== null && typeof v === "object") {
    for (const x of Object.values(v)) checkNumbers(x);
  }
}

// JSON.rawJSON and JSON.isRawJSON (Node 21+; not yet in TypeScript's lib).
const RawJSON = JSON as unknown as { rawJSON(text: string): object; isRawJSON(v: unknown): boolean };
const INTEGER = /^-?(?:0|[1-9][0-9]*)$/;

/** JSON.parse gave the reviver no source text (an engine without JSON.parse source text
 * access): an integer beyond 2^53 − 1 would silently lose digits, so reading fails. This
 * is a failure of the implementation, not a rejection of the input. */
export class NoSourceText extends Error {
  constructor() {
    super("JSON.parse gives the reviver no source text (needs JSON.parse source text access, as in Node 21+)");
  }
}

/** The JSON.parse reviver that keeps an integer literal beyond 2^53 − 1 exact, as the
 * raw JSON of its digits, so that a copied value keeps every digit (as Python's
 * parser does), and reads the literal `-0` as the integer 0. A literal whose binary64
 * value is infinite stays a number, which §1.6
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#16-json-documents> rejects. */
export function keepIntegers(_key: string, value: unknown, context?: { source?: string }): unknown {
  if (typeof value !== "number") return value;
  if (context?.source === undefined) throw new NoSourceText();
  // A non-integer literal of -0 (such as -0.0) stays -0.0, as Python writes it.
  if (!INTEGER.test(context.source)) return Object.is(value, -0) ? RawJSON.rawJSON("-0.0") : value;
  if (Object.is(value, -0)) return 0;
  if (Math.abs(value) <= MAX_SAFE || !Number.isFinite(value)) return value;
  return RawJSON.rawJSON(context.source);
}

/** Is `v` an integer that the reader kept exact (beyond 2^53 − 1)? */
export function isRawInteger(v: unknown): boolean {
  return v !== null && typeof v === "object" && RawJSON.isRawJSON(v);
}

/**
 * A metadata document by §1.6
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#16-json-documents>, or a rejection.
 */
export function parseJson(data: Uint8Array): Json {
  if (data.length > MAX_DOCUMENT) reject(`JSON document of ${data.length} bytes exceeds ${MAX_DOCUMENT}`);
  if (data[0] === 0xef && data[1] === 0xbb && data[2] === 0xbf) reject("JSON document starts with a byte order mark");
  let s = "";
  try {
    s = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(data);
  } catch {
    reject("JSON document is not UTF-8");
  }
  if (nesting(s) > MAX_DEPTH) reject(`JSON nests more than ${MAX_DEPTH} deep`);
  let v: Json = null;
  try {
    v = JSON.parse(s, keepIntegers);
  } catch (e) {
    if (e instanceof NoSourceText) throw e;
    reject(`invalid JSON: ${(e as Error).message}`);
  }
  checkNumbers(v);
  return v;
}

/** The deepest nesting of brackets in `s`, outside strings: a character loop, since a
 * regular expression overflows V8's stack on strings of millions of characters. After a
 * quote with no closing quote, later quotes are plain characters (as Python's scan reads them). */
export function nesting(s: string): number {
  let depth = 0, deepest = 0, open = true;
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if (c === 0x22 && open) {
      let j = i + 1;
      while (j < s.length && s.charCodeAt(j) !== 0x22) j += s.charCodeAt(j) === 0x5c ? 2 : 1;
      if (j >= s.length) open = false;
      else i = j;
    } else if (c === 0x5b || c === 0x7b) {
      deepest = Math.max(deepest, ++depth);
    } else if (c === 0x5d || c === 0x7d) {
      depth--;
    }
  }
  return deepest;
}

/**
 * The commit `C` of a Hub revision's response (§1.5
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>), or a rejection:
 * a gated repository's too.
 */
export function parseHubRevision(body: Uint8Array): string {
  return hostRule("parse_hub_revision", { body: latin1(body) });
}

/**
 * The objects [path, size] of one page of a Hub tree listing (§1.5
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>), its files under
 * the directory `prefix` (its directories list nothing), and its number of entries.
 */
export function parseHubTree(body: Uint8Array, prefix: string): { objects: [string, number][]; entries: number } {
  return hostRule("parse_hub_tree", { body: latin1(body), prefix });
}

/**
 * The next link of a Hub tree response whose `Link` field is `field` (several joined by `,`,
 * as `fetch` joins them) and which `url` answered, resolved against `url` (§1.5
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>); undefined if
 * it has none; a rejection if the field is not links, gives two next links, or one off
 * `endpoint`'s origin.
 */
export function nextLink(field: string | null, url: string, endpoint: string): string | undefined {
  const next = hostRule<string | null>("next_link", { fields: field === null ? [] : [field], url, endpoint });
  return next ?? undefined;
}

/** A value for a message (`JSON.stringify` gives undefined for undefined). */
export function show(v: unknown): string {
  return String(JSON.stringify(v));
}

/** Is `v` a JSON number (an integer kept exact included)? */
export function isNumber(v: unknown): boolean {
  return typeof v === "number" || isRawInteger(v);
}

/**
 * A number as the layout reads it (§1.6
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#16-json-documents>): its binary64
 * value.
 */
export function num(v: unknown): number {
  return isRawInteger(v) ? Number((v as { rawJSON: string }).rawJSON) : (v as number);
}

/**
 * `v` as an integer of §1.6
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#16-json-documents> within [lo, hi],
 * or undefined.
 */
export function asInt(v: unknown, lo = -MAX_SAFE, hi = MAX_SAFE): number | undefined {
  if (isRawInteger(v)) v = num(v) + 0; // + 0: -0.0 reads as 0
  if (typeof v !== "number" || !Number.isInteger(v) || Math.abs(v) > MAX_SAFE) return undefined;
  return v >= lo && v <= hi ? v : undefined;
}

export function isObject(v: unknown): v is { [k: string]: Json } {
  return v !== null && typeof v === "object" && !Array.isArray(v) && !RawJSON.isRawJSON(v);
}

/** Object.hasOwn, for JSON objects (whose members may include `__proto__`). */
export function has(o: object, k: string): boolean {
  return Object.hasOwn(o, k);
}

/** A copy of `o` without the members `drop`. */
export function without(o: { [k: string]: Json }, drop: string[]): { [k: string]: Json } {
  return Object.fromEntries(Object.entries(o).filter(([k]) => !drop.includes(k)));
}

// ---------------------------------------------------------------- stores (§1.4
// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>, §1.5
// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>)

/** Code point (UTF-8 byte) order, unlike JavaScript's UTF-16 order. */
export function compareKeys(a: string, b: string): number {
  const n = Math.min(a.length, b.length);
  for (let i = 0; i < n; i++) {
    const x = a.codePointAt(i)!;
    const y = b.codePointAt(i)!;
    if (x !== y) return x < y ? -1 : 1;
    if (x > 0xffff) i++;
  }
  return a.length - b.length;
}

export interface Store {
  url: string;
  /**
   * Relative key → size, of the objects that are not ignored (§1.4
   * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>).
   */
  objects: Map<string, number>;
  /**
   * The relative keys of the ignored objects that are recorded (§1.4
   * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>).
   */
  ignored?: string[];
  /** The relative keys of the empty objects whose keys end in `/` (directories, to the SAFE profile). */
  folders?: string[];
  listed: number;
  requests: number;
  read(key: string): Promise<Uint8Array>;
  /**
   * The bytes [offset, offset + length) of the object `key` (spec/zarrify/safe.md §8.2
   * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/safe.md#82-objects-and-reading>).
   */
  readRange?(key: string, offset: number, length: number): Promise<Uint8Array>;
  /** The size of the object `key` when the store has it, else undefined: the answer to a run's
   * probe (ir/store.ts), for a store whose objects are not listed (a VSI input's files,
   * ir/vsi.ts). A listed store has the objects it listed. */
  probe?(key: string): Promise<number | undefined>;
  /** Documents read at a time by `prefetchDocuments`; read one at a time if absent or 1. */
  concurrency?: number;
  /** Bytes read ahead and not yet taken, at most (but always one document). */
  prefetchBytes?: number;
  prefetch?: Prefetch;
  /** Reads kept for a second read of the same key (`readDocument` with `keep`). */
  kept?: Map<string, Promise<Settled>>;
}

/** Concurrent document reads by default: 16. Over HTTP/1.1 a browser opens about 6
 * connections per host and queues the rest, so up to 16 keeps those 6 busy at the cost
 * of a short queue; over HTTP/2 the requests share one connection and 16 run at once.
 * Node (undici) opens a connection per request in flight. */
export const CONCURRENCY = 16;
export const PREFETCH_BYTES = 64 << 20;

type Settled = { ok: true; value: Uint8Array } | { ok: false; error: unknown };

const settle = (p: Promise<Uint8Array>): Promise<Settled> =>
  p.then((value) => ({ ok: true as const, value }), (error) => ({ ok: false as const, error }));

/** Reads the objects of a plan (keys, in the order a profile will read them) ahead of
 * the profile, `concurrency` at a time, holding at most `budget` bytes read or being read
 * and not yet taken (but always one object). Each result, the bytes or the error the read
 * threw, is handed over once, by `take`, when the profile reads that key: so a failed read
 * fails the profile exactly where the sequential read would, and only if it reads that key. */
export class Prefetch {
  #queue: string[];
  #pos = 0;
  #size: Map<string, number>;
  #state = new Map<string, "queued" | "started">();
  #result = new Map<string, Promise<Settled>>();
  #held = 0;
  #running = 0;
  #closed = false;
  #read: (key: string) => Promise<Uint8Array>;
  #concurrency: number;
  #budget: number;
  #wanted?: (key: string) => boolean | undefined;

  /** `wanted(key)` is false for a key the profile will not read after all, and undefined
   * while that is not known yet (asked again after each document the profile reads). */
  constructor(
    read: (key: string) => Promise<Uint8Array>,
    plan: [string, number][],
    concurrency: number,
    budget: number,
    wanted?: (key: string) => boolean | undefined,
  ) {
    this.#read = read;
    this.#concurrency = concurrency;
    this.#budget = budget;
    this.#wanted = wanted;
    this.#queue = plan.map(([k]) => k);
    this.#size = new Map(plan);
    for (const [k] of plan) this.#state.set(k, "queued");
    this.#pump();
  }

  #pump() {
    while (!this.#closed && this.#running < this.#concurrency && this.#pos < this.#queue.length) {
      const key = this.#queue[this.#pos];
      if (this.#state.get(key) !== "queued") { // taken by the profile before it was started
        this.#pos++;
        continue;
      }
      const wanted = this.#wanted === undefined ? true : this.#wanted(key);
      if (wanted === undefined) return;
      if (!wanted) {
        this.#state.delete(key);
        this.#pos++;
        continue;
      }
      const size = this.#size.get(key)!;
      if (this.#held > 0 && this.#held + size > this.#budget) return;
      this.#pos++;
      this.#state.set(key, "started");
      this.#held += size;
      this.#running++;
      const r = settle(this.#read(key));
      this.#result.set(key, r);
      void r.then(() => {
        this.#running--;
        this.#pump();
      });
    }
  }

  /** The result of reading `key`; undefined if it was not planned, was taken already, or
   * was not started (the caller reads it). */
  take(key: string): Promise<Settled> | undefined {
    const state = this.#state.get(key);
    this.#state.delete(key);
    let r: Promise<Settled> | undefined;
    if (state === "started") {
      r = this.#result.get(key)!;
      this.#result.delete(key);
      void r.then(() => {
        this.#held -= this.#size.get(key)!;
        this.#pump();
      });
    }
    this.#pump(); // the profile has read on: `wanted` may now know more
    return r;
  }

  close() {
    this.#closed = true;
  }

  /** Starts no more reads, and waits for those running to end: their results stay to be
   * taken, and a key not started is the caller's to read. */
  async drain(): Promise<void> {
    this.#closed = true;
    await Promise.all(this.#result.values());
  }
}

/** Starts reading the documents `keys`, in that order (the order the profile reads them),
 * `store.concurrency` at a time; once per store. Empty documents and documents §1.6
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#16-json-documents> rejects
 * for their size are not read. */
export function prefetchDocuments(store: Store, keys: Iterable<string>, wanted?: (key: string) => boolean | undefined) {
  if ((store.concurrency ?? 1) <= 1 || store.prefetch !== undefined) return;
  const plan: [string, number][] = [];
  for (const k of new Set(keys)) {
    const size = store.objects.get(k)!;
    if (size > 0 && size <= MAX_DOCUMENT) plan.push([k, size]);
  }
  store.prefetch = new Prefetch((k) => store.read(k), plan, store.concurrency!, store.prefetchBytes ?? PREFETCH_BYTES, wanted);
}

/** Stops reading ahead. */
export function closeStore(store: Store) {
  store.prefetch?.close();
}

/** Sets the objects of `store` from every object its listing gave (`listed`, each key under
 * `prefix`), and records the keys of the ignored ones that are not folder markers, and the folder
 * markers (§1.4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>): the
 * core's `store_listing`, which rejects a key listed twice. */
export function setListing(store: Store, listed: [string, number][], prefix: string) {
  const r = hostRule<{ objects: [string, number][]; ignored: string[]; folders: string[] }>("store_listing", { listed, prefix });
  store.objects = new Map(r.objects);
  store.ignored = r.ignored;
  store.folders = r.folders;
  store.listed = listed.length;
}

/** The JSON document at `key` (§1.6 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#16-json-documents>). With `keep`, its bytes are kept for the next read
 * of `key` (each document is read once). */
export async function readDocument(store: Store, key: string, { keep = false } = {}): Promise<Json> {
  const size = store.objects.get(key)!;
  if (size > MAX_DOCUMENT) reject(`${key}: JSON document of ${size} bytes exceeds ${MAX_DOCUMENT}`);
  if (!size) return parseJson(new Uint8Array(0));
  const kept = (store.kept ??= new Map());
  const r = kept.get(key) ?? store.prefetch?.take(key) ?? settle(store.read(key));
  if (keep) kept.set(key, r);
  else kept.delete(key);
  const s = await r;
  if (!s.ok) throw s.error;
  return parseJson(s.value);
}

export type Fetch = (url: string, init?: RequestInit) => Promise<Response>;

/** Throws a resolution error unless the reader policy (spec/refkv.md §6.7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>) lets the store at
 * `base` request `url`: an http(s) URL (§7.2
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#72-file>), allowed by rules 1
 * and 2, its host as
 * written (an IP literal, or `localhost`) allowed by rule 3. */
function allowStoreUrl(policy: Policy, base: string, url: string): void {
  try {
    checkHttpUrl(url);
  } catch (e) {
    throw new RefkvError("resolution", (e as Error).message);
  }
  checkPolicy(policy, base, url);
}

/**
 * Requests for the store at `base` under the reader policy (spec/refkv.md §6.7
 * <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>), as an
 * image file's (`HttpSource`, ir/source.ts): each URL, and each redirect target, is
 * checked by `checkPolicy`. Under Node (`#net`), unless the policy has
 * `allowPrivateHosts`, redirects are followed here (at most 5), each target checked
 * before it is requested; the address each request is sent to is checked, on a new
 * connection and on a kept-alive one; and a request that would go through a proxy is
 * refused unless the policy has `allowUncheckedProxy`. In a browser, `fetch` hides
 * the address it connects to and follows redirects itself, so rule 3 is checked on
 * hosts as written and on the final URL, and Private Network Access covers the
 * rest; a range read (an object read) there bypasses the HTTP cache. A refusal is a resolution error (`RefkvError`); a network error is thrown as
 * `fetch` throws it. `check` applies rule 3 to an address (default: `checkAddress`
 * under `policy`).
 */
export function storeFetch(
  base: string,
  policy: Policy = {},
  headers: Record<string, string> = {},
  check: (url: string, address: string) => void = (u, address) => checkAddress(policy, u, address),
): Fetch {
  const allow = (u: string) => allowStoreUrl(policy, base, u);
  const refusals = new Set<string>();
  const checked = policy.allowPrivateHosts
    ? undefined
    : checkedFetch((u, address) => {
      try {
        check(u, address);
      } catch (e) {
        refusals.add((e as Error).message);
        throw e;
      }
    }, policy);
  return async (u, init = {}) => {
    allow(u);
    const h = { ...headers, ...(init.headers as Record<string, string> | undefined) };
    let r: Response;
    try {
      r = checked
        ? await checked(u, { headers: h, ...(init.signal ? { signal: init.signal } : {}) }, allow)
        // A range read bypasses the HTTP cache (`no-store`), as readHttpRange's do
        // (http.ts): Chromium's cache sends the range requests to one URL one at a
        // time. The listing, which is not a range read, goes through it.
        : await fetch(u, { cache: h.Range === undefined ? "default" : "no-store", ...init, headers: h, redirect: "follow" });
    } catch (e) {
      if (e instanceof RefkvError || (e as Error).name === "AbortError") throw e;
      const message = (e as Error).message;
      if (refusals.has(message)) throw new RefkvError("resolution", message);
      // a network error (fetch's TypeError, a Node socket error) is thrown as is; the
      // others (a proxy the reader cannot check, a bad redirect) are refusals
      if (!checked || e instanceof TypeError || typeof (e as { code?: unknown }).code === "string") throw e;
      throw new RefkvError("resolution", `${u}: ${message}`);
    }
    if (r.redirected) {
      try {
        allow(r.url);
      } catch (e) {
        await r.body?.cancel();
        throw e;
      }
    }
    return r;
  };
}

/** A store listed by S3 ListObjectsV2, or, under the Hub's endpoint (`hubEndpoint`, default
 * `HUB`), a Hugging Face Hub repository at a revision, resolved once to a commit whose files the
 * store reads at its base, `store.url` (§1.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>). Fails (StoreLimitError) past `maxObjects`.
 * Every request (the listing's, each object read's, and each redirect's) is made under
 * the reader policy `policy` (spec/refkv.md §6.7
 * <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>; default: private and
 * special hosts refused),
 * by `storeFetch`; a `fetch` given in its place is still checked by the policy on each
 * URL as written and on each response's final URL. A refusal is a resolution error
 * (`RefkvError`). */
export async function openHttpStore(
  url: string,
  { fetch: given, policy = {}, headers = {}, maxObjects = MAX_OBJECTS, concurrency = CONCURRENCY, hubEndpoint: hub }: {
    fetch?: Fetch;
    /**
     * The reader policy (spec/refkv.md §6.7
     * <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>).
     */
    policy?: Policy;
    /** Extra request headers (such as a `User-Agent` under Node), sent by `storeFetch`. */
    headers?: Record<string, string>;
    maxObjects?: number;
    /** Documents read at a time (CONCURRENCY). */
    concurrency?: number;
    /** The Hub's endpoint (`HUB` if absent): a store URL under it is a Hub store. */
    hubEndpoint?: string;
  } = {},
): Promise<Store> {
  await ir(); // the host rules are the core's
  const repo = hubRepo(url, hubEndpoint(hub));
  const allow = (u: string) => allowStoreUrl(policy, url, u);
  const send = given ?? storeFetch(url, policy, headers);
  const f: Fetch = async (u, init) => {
    allow(u);
    const r = await send(u, init);
    if (r.redirected) {
      try {
        allow(r.url);
      } catch (e) {
        await r.body?.cancel();
        throw e;
      }
    }
    return r;
  };
  // where the objects are read: the store URL, or a Hub store's base once its commit is known
  let base = url;
  const store: Store = {
    url,
    objects: new Map(),
    listed: 0,
    requests: 0,
    concurrency,
    async read(key) {
      const size = this.objects.get(key)!;
      if (size === 0) return new Uint8Array(0);
      const u = objectUrl(base, key);
      let r: Response;
      try {
        r = await f(u, { headers: { Range: `bytes=0-${size - 1}` } });
      } catch (e) {
        if (e instanceof RefkvError) throw e;
        throw new StoreReadError(`${u}: ${(e as Error).message}`);
      }
      if (r.status !== 200 && r.status !== 206) throw new StoreReadError(`${u}: HTTP ${r.status}`);
      const data = new Uint8Array(await r.arrayBuffer());
      if (data.length !== size) throw new StoreReadError(`${u}: read ${data.length} bytes, the listing says ${size}`);
      return data;
    },
    async readRange(key, offset, length) {
      if (length === 0) return new Uint8Array(0);
      const u = objectUrl(base, key);
      let r: Response;
      try {
        r = await f(u, { headers: { Range: `bytes=${offset}-${offset + length - 1}` } });
      } catch (e) {
        if (e instanceof RefkvError) throw e;
        throw new StoreReadError(`${u}: ${(e as Error).message}`);
      }
      if (r.status !== 206 && !(r.status === 200 && offset === 0)) throw new StoreReadError(`${u}: HTTP ${r.status}`);
      const data = new Uint8Array(await r.arrayBuffer());
      if (data.length !== length) throw new StoreReadError(`${u}: read ${data.length} bytes at ${offset}, not ${length}`);
      return data;
    },
  };
  // A listing request's 200 response: 408, 429 and 5xx fail, any other status rejects.
  const list = async (u: string): Promise<Response> => {
    store.requests++;
    let r: Response;
    try {
      r = await f(u);
    } catch (e) {
      if (e instanceof RefkvError) throw e;
      throw new StoreReadError(`${u}: ${(e as Error).message}`);
    }
    if (r.status === 408 || r.status === 429 || r.status >= 500) {
      await r.body?.cancel();
      throw new StoreReadError(`${u}: HTTP ${r.status}`);
    }
    if (r.status !== 200) {
      await r.body?.cancel();
      if (repo === undefined) reject(`the store has no listing: ${u} answered HTTP ${r.status}`);
      const why = r.status === 401
        ? " (a repository that does not exist, or a private one)"
        : r.status === 404 ? " (a revision or a directory the repository does not have)" : "";
      reject(`the Hub answered HTTP ${r.status} for ${u}${why}`);
    }
    return r;
  };
  const listed: [string, number][] = [];
  if (repo !== undefined) {
    const commit = parseHubRevision(new Uint8Array(await (await list(hubRevisionUrl(repo))).arrayBuffer()));
    base = store.url = hubBase(repo, commit);
    const requested = new Set<string>();
    let link: string | undefined = hubTreeUrl(repo, commit);
    while (link !== undefined) {
      requested.add(link);
      const r = await list(link);
      // the URL that answered, after redirects (a stand-in fetch may give none)
      const answered = r.url || link;
      const page = parseHubTree(new Uint8Array(await r.arrayBuffer()), repo.directory);
      listed.push(...page.objects);
      if (listed.length > maxObjects) throw new StoreLimitError(`the store lists more than ${maxObjects} objects`);
      link = nextLink(r.headers.get("Link"), answered, repo.endpoint);
      // a page that lists nothing could be followed forever
      if (link !== undefined && page.entries === 0) reject("a Hub tree page with a next link lists nothing");
      if (link !== undefined && requested.has(link)) {
        reject(`the Hub tree's next link ${JSON.stringify(link.slice(0, 200))} was requested already`);
      }
    }
    setListing(store, listed, repo.directory);
    return store;
  }
  const [endpoint, prefix] = listingEndpoint(url);
  const tokens = new Set<string>();
  let token: string | undefined;
  for (;;) {
    const r = await list(hostRule("listing_url", { endpoint, prefix, token: token ?? null }));
    const listing = parseListing(new Uint8Array(await r.arrayBuffer()), prefix);
    listed.push(...listing.objects);
    if (listed.length > maxObjects) throw new StoreLimitError(`the store lists more than ${maxObjects} objects`);
    token = listing.token;
    if (token === undefined) break;
    // The listing would repeat itself (§1.5
    // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>).
    if (tokens.has(token)) reject(`the listing gives the continuation token ${JSON.stringify(token.slice(0, 80))} again`);
    tokens.add(token);
  }
  setListing(store, listed, prefix);
  return store;
}

/**
 * The store profile that the root keys select (§1.4
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>).
 */
export function chooseProfile(store: Store): "zarr3" | "n5" | "zarr2" | "safe" | "precomputed" | "safetensors" {
  if (store.objects.has(".zarray") || store.objects.has(".zgroup")) return "zarr2";
  if (store.objects.has("attributes.json")) return "n5";
  if (store.objects.has("manifest.safe")) return "safe";
  if (store.objects.has("info")) return "precomputed";
  if (store.objects.has("zarr.json")) return "zarr3";
  for (const k of store.objects.keys()) if (k.endsWith(".safetensors")) return "safetensors";
  return reject(
    "not a Zarr v3, Zarr v2, N5, SAFE, Neuroglancer precomputed or safetensors store: the root has no zarr.json, " +
      ".zarray, .zgroup, " +
      "attributes.json, manifest.safe or info, and no key ends in .safetensors",
  );
}

// ---------------------------------------------------------------- hierarchy and output

export function depth(path: string): number {
  return path === "" ? 0 : path.split("/").length;
}

export function join(parent: string, name: string): string {
  return parent === "" ? name : `${parent}/${name}`;
}

export function parentOf(path: string): string {
  const i = path.lastIndexOf("/");
  return i < 0 ? "" : path.slice(0, i);
}

/**
 * Paths in classification order: by number of segments, then by key (spec/zarrify/n5.md §2
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/n5.md#2-nodes>, spec/zarrify/zarr2.md
 * §2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#2-nodes>).
 */
export function byDepth(paths: Iterable<string>): string[] {
  return [...paths].sort((a, b) => depth(a) - depth(b) || compareKeys(a, b));
}

/** Paths (the root `""` included), each with a value, as a tree of their segments:
 * finding a key's nearest ancestor among them takes time linear in the key's
 * length, without rebuilding any prefix. */
export class PathTrie<T> {
  children = new Map<string, PathTrie<T>>();
  value: T | undefined = undefined;

  add(path: string, value: T): void {
    let node: PathTrie<T> = this;
    if (path !== "") {
      for (const s of path.split("/")) {
        let child = node.children.get(s);
        if (child === undefined) node.children.set(s, (child = new PathTrie<T>()));
        node = child;
      }
    }
    node.value = value;
  }

  /** [value, j] of the nearest proper ancestor of the key with segments `segs`
   * that has a value, at the path `segs.slice(0, j)`; or undefined. */
  nearest(segs: string[]): [T, number] | undefined {
    let node: PathTrie<T> | undefined = this;
    let best: [T, number] | undefined;
    if (this.value !== undefined && !(segs.length === 1 && segs[0] === "")) best = [this.value, 0];
    for (let j = 0; j < segs.length - 1; j++) {
      node = node.children.get(segs[j]);
      if (node === undefined) break;
      if (node.value !== undefined) best = [node.value, j + 1];
    }
    return best;
  }
}

/** Is `path` a proper descendant of one of the paths of `arrays`? */
export function insideArray(path: string, arrays: PathTrie<true>): boolean {
  return arrays.nearest(path.split("/")) !== undefined;
}

/** The implicit groups: proper ancestors of nodes that are not nodes. Each walk
 * stops at a path already seen, so the time is that of the paths it adds. */
export function implicitGroups(nodes: Iterable<string>): Set<string> {
  const all = new Set(nodes);
  const out = new Set<string>();
  for (const path of all) {
    let i = path.length;
    while (i > 0) {
      i = path.lastIndexOf("/", i - 1);
      const p = i > 0 ? path.slice(0, i) : "";
      if (all.has(p) || out.has(p)) break;
      out.add(p);
      i = Math.max(i, 0);
    }
  }
  return out;
}

/**
 * Is `s` a decimal integer without leading zeros below `limit` (spec/zarrify/n5.md §3.1
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/n5.md#31-chunks>,
 * spec/zarrify/zarr2.md §3
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#3-arrays>)?
 */
export function canonicalIndex(s: string, limit: number): boolean {
  return /^(0|[1-9][0-9]{0,16})$/.test(s) && Number(s) < limit;
}

/** ceil(shape / chunks), exactly (a binary64 quotient can round down past an integer). */
export function grid(shape: number[], chunks: number[]): number[] {
  return shape.map((s, i) => Number((BigInt(s) + BigInt(chunks[i]) - 1n) / BigInt(chunks[i])));
}

/**
 * The chunk objects (§1.4
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>): each object's
 * nearest ancestor array decides. Sorted by key.
 */
export function findChunks(objects: Map<string, number>, arrays: Map<string, (rest: string) => boolean>): [string, number][] {
  const trie = new PathTrie<(rest: string) => boolean>();
  for (const [path, test] of arrays) trie.add(path, test);
  const out: [string, number][] = [];
  for (const [key, size] of objects) {
    const segs = key.split("/");
    const found = trie.nearest(segs);
    if (found !== undefined && found[0](segs.slice(found[1]).join("/"))) out.push([key, size]);
  }
  return out.sort((a, b) => compareKeys(a[0], b[0]));
}

export const docKey = (path: string) => join(path, "zarr.json");

/**
 * Declares the profile's convention on the root node of `docs` (conventions §2
 * <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#2-attributes>).
 */
/** Declares the profile's convention (conventions §2 <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#2-attributes>). Each document's `attributes`
 * holds, until then, the attributes copied from the source; they move into the
 * property `refkv_zarrified` (as its member named after the profile), and the
 * node's attributes are the member `ome` that `omes` gives for its path, if
 * any, and the convention's members. The root always declares the convention;
 * any other node only when it has copied attributes. */
export function declareNodes(
  docs: Map<string, Json>,
  profile: Profile,
  url: string,
  omes: Map<string, Json> = new Map(),
): void {
  for (const [key, doc] of docs) {
    const path = key.slice(0, key.length - "zarr.json".length).replace(/\/$/, "");
    const node = doc as { [k: string]: Json };
    const target: { [k: string]: Json } = omes.has(path) ? { ome: omes.get(path)! } : {};
    const attributes = declare(target, profile, path === "" ? url : undefined,
      node.attributes as { [k: string]: Json }) as { [k: string]: Json };
    docs.set(key, { ...node, attributes });
  }
}

export interface StoreResult {
  docs: Map<string, Json>;
  /** (key, size) of the chunk entries, sorted, sizes > 0. */
  chunks: [string, number][];
  /** The source key of each entry whose key is not its object's (the objects under refkv_source/objects/). */
  origins?: Map<string, string>;
}

export const SOURCE_GROUP = "refkv_source";
export const OBJECTS = "refkv_source/objects/";
/**
 * The empty objects' keys under a root array (spec/zarrify/zarr2.md §5
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#5-other-objects>).
 */
export const EMPTY_KEY = "refkv_source/empty.json";
// A last segment that a Zarr reader takes for a node's document, followed by any number of `~`.
const NODE_NAME = /^(?:zarr\.json|\.zarray|\.zgroup)~*$/;

/** The hierarchy's key of the other object `key` (spec/zarrify/zarr2.md §5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#5-other-objects>,
 * spec/zarrify/n5.md §6
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/n5.md#6-other-objects>):
 * `refkv_source/objects/<key>`, with `~` appended to a last
 * segment that is `zarr.json`, `.zarray` or `.zgroup` followed by any number of `~`, so
 * that no Zarr reader opens a node there, and the key maps back one to one. */
export function objectKey(key: string): string {
  return OBJECTS + key + (NODE_NAME.test(key.slice(key.lastIndexOf("/") + 1)) ? "~" : "");
}

/** A store node's source metadata S (spec/zarrify/zarr2.md §4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#4-source-metadata>, spec/zarrify/n5.md §5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/n5.md#5-source-metadata>,
 * spec/zarrify/ome-zarr.md §8
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ome-zarr.md#8-source-metadata>):
 * `{"attributes": A, "metadata": M, "unversioned": U}`,
 * each member only when it is not empty. */
export function sourceMetadata(
  attributes: { [k: string]: Json },
  metadata: { [k: string]: Json },
  unversioned: string[] = [],
): { [k: string]: Json } {
  const s: { [k: string]: Json } = {};
  if (Object.keys(attributes).length) s.attributes = attributes;
  if (Object.keys(metadata).length) s.metadata = metadata;
  if (unversioned.length) s.unversioned = unversioned;
  return s;
}

/** Adds every nonempty object of the store that is not in `used` (the node documents
 * the hierarchy represents, the nonempty chunk objects, the objects referenced under
 * their own key, and those the convention leaves out), whole, at its `objectKey`, lists
 * the keys of the empty ones (empty chunk objects included) and the recorded `ignored`
 * keys (§1.4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>), and
 * adds the group `refkv_source` when there is one
 * (spec/zarrify/zarr2.md §5
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#5-other-objects>,
 * spec/zarrify/n5.md §6
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/n5.md#6-other-objects>). Returns how
 * many objects
 * are kept. A hierarchy with a node at `refkv_source` or below it then rejects the input
 * (`nodes`: every node path). */
export function keepObjects(
  result: StoreResult,
  objects: Map<string, number>,
  used: Set<string>,
  nodes: Iterable<string>,
  fail: (m: string) => never,
  ignoredKeys: string[] = [],
): number {
  const empty = [...objects].filter(([k, n]) => n === 0 && !used.has(k)).map(([k]) => k).sort(compareKeys);
  const kept = [...objects].filter(([k, n]) => n > 0 && !used.has(k));
  const ignored = [...ignoredKeys].sort(compareKeys);
  if (kept.length === 0 && empty.length === 0 && ignored.length === 0) return 0;
  const own = empty.length || ignored.length
    ? { ...(empty.length ? { empty } : {}), ...(ignored.length ? { ignored } : {}) }
    : undefined;
  const root = result.docs.get(docKey(""))! as { node_type?: string; attributes: Record<string, any> };
  if (root.node_type !== "array") {
    for (const p of nodes) {
      if (p === SOURCE_GROUP || p.startsWith(SOURCE_GROUP + "/")) {
        fail(`the node ${p} is where the store's other objects go (${OBJECTS}...)`);
      }
    }
    // The keys of empty and ignored objects are the source metadata of refkv_source.
    const profile = root.attributes[CONVENTION_KEY].profile as Profile;
    result.docs.set(docKey(SOURCE_GROUP), { zarr_format: 3, node_type: "group", attributes: declare({}, profile, undefined, own) as Json });
  } else if (own) {
    // An array has no children: under a root array, the keys are plain keys of the archive.
    result.docs.set(EMPTY_KEY, own);
  }
  result.origins ??= new Map();
  for (const [k] of kept) result.origins.set(objectKey(k), k);
  result.chunks = [...result.chunks, ...kept.map(([k, n]): [string, number] => [objectKey(k), n])]
    .sort((a, b) => compareKeys(a[0], b[0]));
  return kept.length;
}

/**
 * The archive of a store profile's output (§1.4
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>): one url source per
 * chunk entry.
 */
export function storeArchive(url: string, { docs, chunks, origins }: StoreResult): ArchiveDesc {
  for (const key of [...docs.keys(), ...chunks.map(([k]) => k)]) {
    if (utf8.encode(key).length > 65535) reject("an output key is longer than 65535 bytes");
    if (key.startsWith("__vz__/")) reject(`output key ${JSON.stringify(key)} is in the reserved __vz__/ space`);
  }
  const entries: EntryDesc[] = chunks.map(([key, size], i) => ({
    key, ranges: [{ source: i, offset: 0n, length: BigInt(size) }],
  }));
  for (const [key, doc] of docs) {
    // refkv_source's documents (the empty objects' keys) are read only when asked for: compressed.
    const compress = key.startsWith(SOURCE_GROUP + "/");
    entries.push({ key, bytes: utf8.encode(stringifyJson(doc)), ...(compress ? { compress } : {}) });
  }
  // Each source pins its object's listed size (§1.4
  // <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>).
  return { sources: chunks.map(([key, size]) => ({ url: objectUrl(url, origins?.get(key) ?? key), size: BigInt(size) })), entries };
}

export const GROUP_IMPLICIT = (): Json => ({ zarr_format: 3, node_type: "group", attributes: {} });

// ---------------------------------------------------------------- codecs

const BLOSC_NAMES = ["blosclz", "lz4", "lz4hc", "snappy", "zlib", "zstd"];
const MAX_BLOCK = 2 ** 31 - 1;

/**
 * The Zarr v3 blosc codec of an n5-blosc or numcodecs Blosc configuration (spec/zarrify/n5.md §3
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/n5.md#3-datasets>,
 * spec/zarrify/zarr2.md §3.1
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/zarr2.md#31-compressors>).
 */
export function bloscCodec(
  c: { [k: string]: Json },
  typesize: number,
  shuffles: Map<number, string>,
  fail: (m: string) => never,
): Json {
  const cname = c.cname;
  const clevel = asInt(c.clevel, 0, 9);
  const shuffle = asInt(c.shuffle);
  const blocksize = asInt(has(c, "blocksize") ? c.blocksize : 0, 0, MAX_BLOCK);
  if (typeof cname !== "string" || !BLOSC_NAMES.includes(cname)) fail(`blosc cname ${JSON.stringify(cname)} is not supported`);
  if (clevel === undefined) fail(`blosc clevel ${JSON.stringify(c.clevel)} is not an integer from 0 to 9`);
  if (shuffle === undefined || !shuffles.has(shuffle)) fail(`blosc shuffle ${JSON.stringify(c.shuffle)} is not supported`);
  if (blocksize === undefined) fail(`blosc blocksize ${JSON.stringify(c.blocksize)} is not supported`);
  let s = shuffles.get(shuffle!)!;
  if (s === "auto") s = typesize === 1 ? "bitshuffle" : "shuffle";
  return { name: "blosc", configuration: { cname: cname as string, clevel: clevel!, shuffle: s, typesize, blocksize: blocksize! } };
}
