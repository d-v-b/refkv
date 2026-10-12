// The frozen Zarr v2 profile's declaration: `declareNodes` of js/src/virtualize/store.ts, its root
// recording the revision the profile is frozen at (25, the last before Zarr v2 stores moved onto
// the Rust IR in revision 26), as revision.ts pins the TIFF, ND2 and CZI twins.

import { declare, type Profile } from "../../../../../js/src/virtualize/common.ts";
import type { Json } from "../../../../../js/src/virtualize/store.ts";

export const REVISION = 25;

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
      node.attributes as { [k: string]: Json }, REVISION) as { [k: string]: Json };
    docs.set(key, { ...node, attributes });
  }
}
