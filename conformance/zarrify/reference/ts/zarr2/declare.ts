// The frozen Zarr v2 profile's declaration: `declareNodes` of js/src/zarrify/store.ts, its root
// recording the revision the profile is frozen at (`STORES_REVISION` of revision.ts, 25, the last
// before Zarr v2 stores moved onto the Rust IR in revision 26), as revision.ts pins the other twins.

import { declare, type Profile } from "../legacy.ts";
import type { Json } from "../legacy.ts";
import { STORES_REVISION } from "../revision.ts";

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
      node.attributes as { [k: string]: Json }, STORES_REVISION) as { [k: string]: Json };
    docs.set(key, { ...node, attributes });
  }
}
