// Virtualizing an image file by the profile its first bytes select
// (spec/virtualize.md §1.2 <https://github.com/d-v-b/vzip/blob/main/spec/virtualize.md#12-input>):
// TIFF, NDPI, ND2, DICOM, NIfTI, IMS, CZI or a SAFE zip, or a
// store by the profile its root keys select (§1.4
// <https://github.com/d-v-b/vzip/blob/main/spec/virtualize.md#14-store-inputs>): N5, Zarr v2,
// OME-Zarr 0.4 or a SAFE directory. TIFF (NDPI included: the core's TIFF parser finds it), ND2,
// CZI, DICOM, NIfTI, IMS and SAFE files, and SAFE directories and Zarr v2 and N5 stores, are
// virtualized by the Rust core (ir/, rust/vzip-ir); each other profile is in its own directory;
// store.ts holds the store machinery.

import { blockReader, type ByteReader, ImageError } from "./common.ts";
import { type IrFormat, virtualizeIr } from "./ir/run.ts";
import { type RangeSource, readerSource } from "./ir/source.ts";
import { virtualizeStoreIr } from "./ir/store.ts";
import { declares04, virtualizeOmeZarr } from "./ome-zarr/virtualize.ts";
import { chooseProfile, closeStore, objectUrl, openHttpStore, type Store, storeArchive } from "./store.ts";
import type { Policy } from "../archive.ts";
import { crc32c } from "../deflate.ts";
import type { ArchiveDesc } from "../writer.ts";

/**
 * How a virtualizer pins its sources and checksums its ranges (spec/virtualize.md §1.2
 * <https://github.com/d-v-b/vzip/blob/main/spec/virtualize.md#12-input>, §1.4
 * <https://github.com/d-v-b/vzip/blob/main/spec/virtualize.md#14-store-inputs>).
 */
export interface PinOptions {
  /** The input file's strong ETag, if every response gave the same one (an `etag` pin). */
  etag?: () => string | undefined;
  /**
   * Record the CRC-32C of every range of a url source (spec/archive.md §4.2
   * <https://github.com/d-v-b/vzip/blob/main/spec/archive.md#42-range>), reading its bytes.
   */
  checksums?: boolean;
}

/**
 * Records on every range of a url source the CRC-32C of its bytes (spec/archive.md
 * §4.2 <https://github.com/d-v-b/vzip/blob/main/spec/archive.md#42-range>), read by `read(url,
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

const NOT_SUPPORTED = "not a TIFF, NDPI, ND2, DICOM, NIfTI, IMS, CZI or SAFE zip file";

/**
 * True if `head` starts with a TIFF or BigTIFF header, in either byte order (§1.2
 * <https://github.com/d-v-b/vzip/blob/main/spec/virtualize.md#12-input>).
 */
function isTiffMagic(head: Uint8Array): boolean {
  const magic = Array.from(head.subarray(0, 4), (b) => b.toString(16).padStart(2, "0")).join("");
  return ["49492a00", "4d4d002a", "49492b00", "4d4d002b"].includes(magic);
}

// The HDF5 signature, which starts Imaris IMS files (§1.2
// <https://github.com/d-v-b/vzip/blob/main/spec/virtualize.md#12-input>).
/** The DICOM row of §1.2 <https://github.com/d-v-b/vzip/blob/main/spec/virtualize.md#12-input>:
 * `DICM` after the 128-byte preamble. */
export function isDicom(head: Uint8Array): boolean {
  return head.length >= 132 && String.fromCharCode(...head.subarray(128, 132)) === "DICM";
}

const HDF5 = [0x89, 0x48, 0x44, 0x46, 0x0d, 0x0a, 0x1a, 0x0a];

type ImageDesc = ArchiveDesc & { format: "tiff" | "ndpi" | "nd2" | "dicom" | "nifti" | "ims" | "czi" | "safe"; summary: object };

/** `exact`, when given, reads exactly the ranges asked for, past `read`'s block cache: the
 * SAFE profile's tile-part headers, scattered through a zip file, are read with it, and the
 * Rust core's requests (TIFF and NDPI, ND2, CZI, DICOM, NIfTI, IMS), which it plans itself.
 * Source 0 pins the file's size, and its ETag when `pins.etag` gives one (§1.2
 * <https://github.com/d-v-b/vzip/blob/main/spec/virtualize.md#12-input>); with
 * `pins.checksums`, the ranges of source 0 carry the CRC-32C of their bytes. */
export async function virtualizeImage(
  url: string,
  read: ByteReader,
  fileSize: number,
  exact?: ByteReader,
  pins: PinOptions = {},
): Promise<ImageDesc> {
  return virtualizeWith(url, { read, size: fileSize, exact, source: readerSource(exact ?? read, fileSize) }, pins);
}

/** Virtualizes the file a range source reads, such as an http(s) object opened under the
 * reader policy by `openHttpSource` (ir/source.ts): the Rust core plans its requests
 * (several at once, multi-range ones where the server answers them), and the other
 * profiles read it through a block cache. Source 0 pins the size, and the ETag when
 * every response gave the same strong one (`pins.etag`, else the source's). */
export async function virtualizeSource(url: string, source: RangeSource, pins: PinOptions = {}): Promise<ImageDesc> {
  const exact: ByteReader = (o, n) => source.read(o, n);
  const read = blockReader(exact, source.size);
  const etag = pins.etag ?? (source.etag ? () => source.etag!() : undefined);
  return virtualizeWith(url, { read, size: source.size, exact, source }, { ...pins, ...(etag ? { etag } : {}) });
}

interface Input {
  read: ByteReader;
  size: number;
  exact?: ByteReader;
  source: RangeSource;
}

async function virtualizeWith(url: string, input: Input, pins: PinOptions): Promise<ImageDesc> {
  const desc = await virtualizeFile(url, input);
  const etag = pins.etag?.();
  desc.sources[0] = { ...desc.sources[0], size: BigInt(input.size), ...(etag !== undefined ? { etag } : {}) };
  if (pins.checksums) await addChecksums(desc, (_u, o, n) => (input.exact ?? input.read)(o, n));
  return desc;
}

/**
 * The NIfTI version a file's first bytes (at least 348 of them, when the file has them)
 * select (§1.2 <https://github.com/d-v-b/vzip/blob/main/spec/virtualize.md#12-input>), if one.
 */
export function niftiVersion(head: Uint8Array): 1 | 2 | undefined {
  if (head.length < 12) return undefined;
  const v = new DataView(head.buffer, head.byteOffset, head.byteLength);
  const sizes = [v.getInt32(0, true), v.getInt32(0, false)];
  const ascii = (a: number, b: number) => String.fromCharCode(...head.subarray(a, b));
  if (sizes.includes(348) && head.length >= 348 && ascii(344, 348) === "n+1\0") return 1;
  if (sizes.includes(540) && ascii(4, 12) === "n+2\0\r\n\x1a\n") return 2;
  return undefined;
}

/**
 * The profile of the Rust core a file's first bytes select, if one (§1.2
 * <https://github.com/d-v-b/vzip/blob/main/spec/virtualize.md#12-input>; an NDPI is a TIFF
 * here, and the core's run reports it as NDPI).
 */
export function irFormat(head: Uint8Array): IrFormat | undefined {
  // DICOM first: a dual-personality file's preamble may hold a TIFF header
  if (isDicom(head)) return "dicom";
  if (isTiffMagic(head)) return "tiff";
  if (head.length >= 4 && head[0] === 0xda && head[1] === 0xce && head[2] === 0xbe && head[3] === 0x0a) return "nd2";
  if (head.length >= 16 && String.fromCharCode(...head.subarray(0, 16)) === "ZISRAWFILE\0\0\0\0\0\0") return "czi";
  // a ZIP local file header: a SAFE product's zip form (spec/virtualize/safe.md §12.1
  // <https://github.com/d-v-b/vzip/blob/main/spec/virtualize/safe.md#121-choosing-the-profile>)
  if (head.length >= 4 && head[0] === 0x50 && head[1] === 0x4b && head[2] === 0x03 && head[3] === 0x04) return "safe";
  if (niftiVersion(head) !== undefined) return "nifti";
  if (HDF5.every((b, i) => head[i] === b)) return "ims";
  return undefined;
}

async function virtualizeFile(url: string, { read, size: fileSize, exact, source }: Input): Promise<ImageDesc> {
  const head = await read(0, Math.min(552, fileSize));
  if (isDicom(head)) return virtualizeIr("dicom", url, source);
  const ir = irFormat(head);
  if (ir === "tiff" || ir === "nd2") return virtualizeIr(ir, url, source);
  if (ir === "nifti" || ir === "ims" || ir === "czi" || ir === "safe") return virtualizeIr(ir, url, source);
  throw new ImageError(NOT_SUPPORTED);
}

/**
 * Is `url` a store input (§1.2
 * <https://github.com/d-v-b/vzip/blob/main/spec/virtualize.md#12-input>): a URL whose path ends in
 * `/`?
 */
export function isStoreUrl(url: string): boolean {
  return url.split(/[?#]/, 1)[0].endsWith("/");
}

/** Virtualizes a store by the profile its root keys select (§1.4 <https://github.com/d-v-b/vzip/blob/main/spec/virtualize.md#14-store-inputs>): a listed one, or the
 * one at an http(s) URL, listed and read by `openHttpStore` under the reader policy
 * `options.policy` (spec/archive.md §6.7
 * <https://github.com/d-v-b/vzip/blob/main/spec/archive.md#67-reader-policy>; default: private and
 * special hosts refused). Every
 * source pins its object's size; with `options.checksums`, every range carries the
 * CRC-32C of its bytes. */
export async function virtualizeStore(
  input: Store | string,
  options: PinOptions & { policy?: Policy; headers?: Record<string, string> } = {},
): Promise<ArchiveDesc & { format: "n5" | "zarr2" | "ome-zarr" | "safe"; summary: object }> {
  if (typeof input !== "string" && (options.policy !== undefined || options.headers !== undefined)) {
    // a listed store was opened under its policy already
    throw new TypeError("virtualizeStore: a policy applies to a store URL; pass it to openHttpStore for an opened store");
  }
  const store = typeof input === "string"
    ? await openHttpStore(input, { policy: options.policy ?? {}, headers: options.headers ?? {} })
    : input;
  const pins = options;
  const desc = await virtualizeStoreProfile(store);
  if (pins.checksums) {
    const keys = new Map([...store.objects.keys()].map((k) => [objectUrl(store.url, k), k]));
    await addChecksums(desc, async (u, o, n) => {
      const key = keys.get(u)!;
      return store.readRange ? store.readRange(key, o, n) : (await store.read(key)).subarray(o, o + n);
    });
  }
  return desc;
}

async function virtualizeStoreProfile(
  store: Store,
): Promise<ArchiveDesc & { format: "n5" | "zarr2" | "ome-zarr" | "safe"; summary: object }> {
  try {
    const chosen = chooseProfile(store);
    if (chosen === "safe") return { format: "safe", ...(await virtualizeStoreIr("safe", store)) };
    if (chosen === "n5") return { format: "n5", ...(await virtualizeStoreIr("n5", store)) };
    let format: "zarr2" | "ome-zarr" = chosen;
    if (format === "zarr2" && (await declares04(store))) format = "ome-zarr";
    if (format === "zarr2") return { format, ...(await virtualizeStoreIr("zarr2", store)) };
    const result = await virtualizeOmeZarr(store);
    return { format, ...storeArchive(store.url, result), summary: result.summary };
  } finally {
    closeStore(store);
  }
}
