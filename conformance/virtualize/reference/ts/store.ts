// The store dispatch of js/src/virtualize/index.ts as it was before SAFE directories and Zarr v2
// and N5 and OME-Zarr stores moved onto the Rust IR (revision 26), with the frozen SAFE, Zarr v2,
// N5 and OME-Zarr profiles (safe/, zarr2/, n5/, ome-zarr/).

import type { Policy } from "../../../../js/src/archive.ts";
import { addChecksums, type PinOptions } from "../../../../js/src/virtualize/index.ts";
import { declares04, virtualizeOmeZarr } from "./ome-zarr/virtualize.ts";
import { chooseProfile, closeStore, objectUrl, openHttpStore, type Store, storeArchive } from "./legacy.ts";
import type { ArchiveDesc } from "../../../../js/src/writer.ts";
import { virtualizeSafeStore } from "./safe/virtualize.ts";
import { virtualizeN5 } from "./n5/virtualize.ts";
import { virtualizeZarr2 } from "./zarr2/virtualize.ts";

export async function virtualizeStore(
  input: Store | string,
  options: PinOptions & { policy?: Policy; headers?: Record<string, string> } = {},
): Promise<ArchiveDesc & { format: "n5" | "zarr2" | "ome-zarr" | "safe"; summary: object }> {
  if (typeof input !== "string" && (options.policy !== undefined || options.headers !== undefined)) {
    throw new TypeError("virtualizeStore: a policy applies to a store URL; pass it to openHttpStore for an opened store");
  }
  const store = typeof input === "string"
    ? await openHttpStore(input, { policy: options.policy ?? {}, headers: options.headers ?? {} })
    : input;
  const desc = await virtualizeStoreProfile(store);
  if (options.checksums) {
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
    if (chosen === "safe") return { format: "safe", ...(await virtualizeSafeStore(store)) };
    let format: "n5" | "zarr2" | "ome-zarr" = chosen;
    if (format === "zarr2" && (await declares04(store))) format = "ome-zarr";
    const result = format === "n5"
      ? await virtualizeN5(store)
      : format === "ome-zarr"
      ? await virtualizeOmeZarr(store)
      : await virtualizeZarr2(store);
    return { format, ...storeArchive(store.url, result), summary: result.summary };
  } finally {
    closeStore(store);
  }
}
