// Zarrifying an image file by the profile its first bytes select
// (spec/zarrify.md §1.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>),
// or a store by the profile its root keys select (§1.4
// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>); the formats are
// those of docs/zarrify/README.md (the registry, spec/zarrify/profiles/). Every one is
// zarrified by the Rust core (ir/, rust/refkv-ir), which may decide the profile itself (its
// TIFF parser finds NDPI; its Zarr v2 store format finds OME-Zarr 0.4); store.ts holds the store
// machinery.

import { blockReader, type ByteReader, ImageError } from "./common.ts";
import { type IrFormat, zarrifyIr } from "./ir/run.ts";
import { ir, out, put } from "./ir/wasm.ts";
import { type RangeSource, readerSource } from "./ir/source.ts";
import { zarrifyStoreIr } from "./ir/store.ts";
import { BESIDE, type BesideFormat, zarrifyVsi } from "./ir/vsi.ts";
import { chooseProfile, closeStore, objectUrl, openHttpStore, type Store } from "./store.ts";
import type { Policy } from "../archive.ts";
import { crc32c } from "../deflate.ts";
import type { ArchiveDesc } from "../writer.ts";

/**
 * How a zarrifier pins its sources and checksums its ranges (spec/zarrify.md §1.2
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>, §1.4
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>).
 */
export interface PinOptions {
  /** The input file's strong ETag, if every response gave the same one (an `etag` pin). */
  etag?: () => string | undefined;
  /**
   * Record the CRC-32C of every range of a url source (spec/refkv.md §4.2
   * <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#42-range>), reading its bytes.
   */
  checksums?: boolean;
  /** For a VSI or NRRD input: opens the file `key` beside the named file (`key` relative to its
   * directory), or undefined when it does not exist (ir/vsi.ts); by default the files are read
   * at the input URL's directory over http(s), with the named file's options. */
  open?: (key: string) => Promise<RangeSource | undefined>;
}

/**
 * Records on every range of a url source the CRC-32C of its bytes (spec/refkv.md
 * §4.2 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#42-range>), read by `read(url,
 * offset, length)`.
 */
export async function addChecksums(
  desc: ArchiveDesc,
  read: (url: string, offset: number, length: number) => Promise<Uint8Array>,
): Promise<void> {
  for (const e of desc.entries) {
    if (!("ranges" in e)) continue;
    for (const r of e.ranges) {
      if ("data" in r || r.crc32c !== undefined) continue;
      const url = desc.sources[r.source].url;
      if (url !== undefined) r.crc32c = crc32c(await read(url, Number(r.offset), Number(r.length)));
    }
  }
}

const NOT_SUPPORTED = "not a file of any supported format";

type ImageDesc = ArchiveDesc & { format: IrFormat; summary: object };

/** `exact`, when given, reads exactly the ranges asked for, past `read`'s block cache: the
 * Rust core's requests, which it plans itself, are read with it.
 * Source 0 pins the file's size, and its ETag when `pins.etag` gives one (§1.2
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>); with
 * `pins.checksums`, the ranges of source 0 carry the CRC-32C of their bytes. */
export async function zarrifyImage(
  url: string,
  read: ByteReader,
  fileSize: number,
  exact?: ByteReader,
  pins: PinOptions = {},
): Promise<ImageDesc> {
  return zarrifyWith(url, { read, size: fileSize, exact, source: readerSource(exact ?? read, fileSize) }, pins);
}

/** Zarrifies the file a range source reads, such as an http(s) object opened under the
 * reader policy by `openHttpSource` (ir/source.ts): the Rust core plans its requests
 * (several at once, multi-range ones where the server answers them), and the other
 * profiles read it through a block cache. Source 0 pins the size, and the ETag when
 * every response gave the same strong one (`pins.etag`, else the source's). */
export async function zarrifySource(url: string, source: RangeSource, pins: PinOptions = {}): Promise<ImageDesc> {
  const exact: ByteReader = (o, n) => source.read(o, n);
  const read = blockReader(exact, source.size);
  const etag = pins.etag ?? (source.etag ? () => source.etag!() : undefined);
  return zarrifyWith(url, { read, size: source.size, exact, source }, { ...pins, ...(etag ? { etag } : {}) });
}

interface Input {
  read: ByteReader;
  size: number;
  exact?: ByteReader;
  source: RangeSource;
}

async function zarrifyWith(url: string, input: Input, pins: PinOptions): Promise<ImageDesc> {
  // a VSI file is read with its tile files, and a NRRD header with its data file, as a store
  // (ir/vsi.ts): its sources are the files some reference uses, their sizes pinned, not source 0
  // alone
  const format = await irFormat(await input.read(0, Math.min(552, input.size)));
  if (format === undefined) throw new ImageError(NOT_SUPPORTED);
  if (format in BESIDE) {
    const beside = format as BesideFormat;
    const { store, ...desc } = await zarrifyVsi(url, input.source, pins.open ? { open: pins.open } : {}, beside);
    if (pins.checksums) {
      const keys = new Map([...store.objects.keys()].map((k) => [objectUrl(store.url, k), k]));
      await addChecksums(desc, (u, o, n) => store.readRange!(keys.get(u)!, o, n));
    }
    return { format: beside, ...desc };
  }
  const desc = await zarrifyIr(format, url, input.source, { checksums: pins.checksums === true });
  const etag = pins.etag?.();
  desc.sources[0] = { ...desc.sources[0], size: BigInt(input.size), ...(etag !== undefined ? { etag } : {}) };
  if (pins.checksums) await addChecksums(desc, (_u, o, n) => (input.exact ?? input.read)(o, n));
  return desc;
}

/**
 * The format of the Rust core a file's first bytes select, if one: its `Format::sniff` (§1.2
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>), which tests each file
 * profile's signature in the registry's `sniff` order (an NDPI is a TIFF here, and the core's run
 * reports it as NDPI).
 */
export async function irFormat(head: Uint8Array): Promise<IrFormat | undefined> {
  const w = await ir();
  const n = Number(w.vz_sniff(put(w, head), head.length));
  return n > 0 ? new TextDecoder().decode(out(w, n)) : undefined;
}

/**
 * Is `url` a store input (§1.2
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>): a URL whose path ends in
 * `/`?
 */
export function isStoreUrl(url: string): boolean {
  return url.split(/[?#]/, 1)[0].endsWith("/");
}

/** Zarrifies a store by the profile its root keys select (§1.4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>): a listed one, or the
 * one at an http(s) URL, listed and read by `openHttpStore` under the reader policy
 * `options.policy` (spec/refkv.md §6.7
 * <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>; default: private and
 * special hosts refused). Every
 * source pins its object's size; with `options.checksums`, every range carries the
 * CRC-32C of its bytes. */
export async function zarrifyStore(
  input: Store | string,
  options: PinOptions & { policy?: Policy; headers?: Record<string, string> } = {},
): Promise<ArchiveDesc & { format: "n5" | "zarr2" | "ome-zarr" | "safe"; summary: object }> {
  if (typeof input !== "string" && (options.policy !== undefined || options.headers !== undefined)) {
    // a listed store was opened under its policy already
    throw new TypeError("zarrifyStore: a policy applies to a store URL; pass it to openHttpStore for an opened store");
  }
  const store = typeof input === "string"
    ? await openHttpStore(input, { policy: options.policy ?? {}, headers: options.headers ?? {} })
    : input;
  const pins = options;
  const desc = await zarrifyStoreProfile(store);
  if (pins.checksums) {
    const keys = new Map([...store.objects.keys()].map((k) => [objectUrl(store.url, k), k]));
    await addChecksums(desc, async (u, o, n) => {
      const key = keys.get(u)!;
      return store.readRange ? store.readRange(key, o, n) : (await store.read(key)).subarray(o, o + n);
    });
  }
  return desc;
}

async function zarrifyStoreProfile(
  store: Store,
): Promise<ArchiveDesc & { format: "n5" | "zarr2" | "ome-zarr" | "safe"; summary: object }> {
  try {
    const chosen = chooseProfile(store);
    if (chosen === "safe") return { format: "safe", ...(await zarrifyStoreIr("safe", store)) };
    if (chosen === "n5") return { format: "n5", ...(await zarrifyStoreIr("n5", store)) };
    // the core decides between Zarr v2 and OME-Zarr 0.4 (its summary's `format`)
    const desc = await zarrifyStoreIr("zarr2", store);
    return { format: desc.summary.format === "ome-zarr" ? "ome-zarr" : "zarr2", ...desc };
  } finally {
    closeStore(store);
  }
}
