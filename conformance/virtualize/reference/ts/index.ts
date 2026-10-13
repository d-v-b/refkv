// The frozen TypeScript virtualizers of TIFF, ND2, CZI, NDPI, DICOM and SAFE (the twins in this
// directory, as they were before the browser adopted the Rust core), dispatched as
// js/src/virtualize/index.ts dispatched them; the other profiles are the shipped
// code's. For comparing the Rust core's output with the TypeScript one's.

import { type ByteReader, ImageError } from "../../../../js/src/virtualize/common.ts";
import { isDicom, virtualizeDicom } from "./dicom/virtualize.ts";
import { virtualizeIms } from "../../../../js/src/virtualize/ims/virtualize.ts";
import { detectNifti, virtualizeNifti } from "../../../../js/src/virtualize/nifti/virtualize.ts";
import { isZip, virtualizeSafeStore, virtualizeSafeZip } from "./safe/virtualize.ts";
import { addChecksums, type PinOptions } from "../../../../js/src/virtualize/index.ts";
import { virtualizeStore as shippedStore } from "../../../../js/src/virtualize/index.ts";
import { chooseProfile, closeStore, objectUrl, openHttpStore, type Store } from "../../../../js/src/virtualize/store.ts";
import type { Policy } from "../../../../js/src/archive.ts";
import type { ArchiveDesc } from "../../../../js/src/writer.ts";
import { isCzi, virtualizeCzi } from "./czi/virtualize.ts";
import { isNd2, virtualizeNd2 } from "./nd2/virtualize.ts";
import { detectNdpi, virtualizeNdpi } from "./ndpi/virtualize.ts";
import { virtualizeTiff } from "./tiff/virtualize.ts";

export { isStoreUrl } from "../../../../js/src/virtualize/index.ts";

/** `virtualizeStore` of js/src/virtualize/index.ts as it was: a SAFE directory goes to the
 * frozen twin, any other store to the shipped code. */
export async function virtualizeStore(
  input: Store | string,
  options: PinOptions & { policy?: Policy; headers?: Record<string, string> } = {},
): Promise<ArchiveDesc & { format: "n5" | "zarr2" | "ome-zarr" | "safe"; summary: object }> {
  const store = typeof input === "string"
    ? await openHttpStore(input, { policy: options.policy ?? {}, headers: options.headers ?? {} })
    : input;
  if (chooseProfile(store) !== "safe") return shippedStore(store, { ...(options.checksums ? { checksums: true } : {}) });
  let desc: ArchiveDesc & { summary: object };
  try {
    desc = await virtualizeSafeStore(store);
  } finally {
    closeStore(store);
  }
  if (options.checksums) {
    const keys = new Map([...store.objects.keys()].map((k) => [objectUrl(store.url, k), k]));
    await addChecksums(desc, async (u, o, n) => {
      const key = keys.get(u)!;
      return store.readRange ? store.readRange(key, o, n) : (await store.read(key)).subarray(o, o + n);
    });
  }
  return { format: "safe", ...desc };
}

const NOT_SUPPORTED = "not a TIFF, NDPI, ND2, DICOM, NIfTI, IMS, CZI or SAFE zip file";

function isTiffMagic(head: Uint8Array): boolean {
  const magic = Array.from(head.subarray(0, 4), (b) => b.toString(16).padStart(2, "0")).join("");
  return ["49492a00", "4d4d002a", "49492b00", "4d4d002b"].includes(magic);
}

const HDF5 = [0x89, 0x48, 0x44, 0x46, 0x0d, 0x0a, 0x1a, 0x0a];

type ImageDesc = ArchiveDesc & { format: "tiff" | "ndpi" | "nd2" | "dicom" | "nifti" | "ims" | "czi" | "safe"; summary: object };

/** `virtualizeImage` of js/src/virtualize/index.ts, with the frozen TIFF, ND2, CZI, NDPI, DICOM and SAFE virtualizers. */
export async function virtualizeImage(
  url: string,
  read: ByteReader,
  fileSize: number,
  exact?: ByteReader,
  pins: PinOptions = {},
): Promise<ImageDesc> {
  const desc = await virtualizeFile(url, read, fileSize, exact);
  const etag = pins.etag?.();
  desc.sources[0] = { ...desc.sources[0], size: BigInt(fileSize), ...(etag !== undefined ? { etag } : {}) };
  if (pins.checksums) await addChecksums(desc, (_u, o, n) => (exact ?? read)(o, n));
  return desc;
}

async function virtualizeFile(url: string, read: ByteReader, fileSize: number, exact?: ByteReader): Promise<ImageDesc> {
  const head = await read(0, Math.min(552, fileSize));
  if (isDicom(head)) return { format: "dicom", ...(await virtualizeDicom(url, read, fileSize)) };
  if (isTiffMagic(head)) {
    const first = await detectNdpi(read, fileSize);
    if (first !== undefined) return { format: "ndpi", ...(await virtualizeNdpi(url, read, fileSize, first)) };
    return { format: "tiff", ...(await virtualizeTiff(url, read, fileSize)) };
  }
  if (isNd2(head)) return { format: "nd2", ...(await virtualizeNd2(url, read, fileSize, exact)) };
  if (HDF5.every((b, i) => head[i] === b)) return { format: "ims", ...(await virtualizeIms(url, read, fileSize)) };
  if (detectNifti(head) !== undefined) return { format: "nifti", ...(await virtualizeNifti(url, read, fileSize)) };
  if (isCzi(head)) return { format: "czi", ...(await virtualizeCzi(url, read, fileSize, exact)) };
  if (isZip(head)) return { format: "safe", ...(await virtualizeSafeZip(url, read, fileSize, exact ?? read)) };
  throw new ImageError(NOT_SUPPORTED);
}
