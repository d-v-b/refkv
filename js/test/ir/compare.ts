// Comparing the Rust core's output (src/zarrify/ir) with the frozen TypeScript
// zarrifiers' (conformance/reference): the hierarchy, outside the source
// metadata node, whose mirror differs by design.

import type { Range, Source } from "../../src/protobuf.ts";
import { CONVENTION_KEY, SOURCE_NODE } from "../../src/zarrify/common.ts";
import { currentNames, SOURCE_NODE as LEGACY_NODE } from "../../../conformance/zarrify/reference/ts/legacy.ts";
import type { ArchiveDesc } from "../../src/writer.ts";
import { REVISION_25 } from "../../../conformance/zarrify/reference/ts/revision.ts";

/** The profiles frozen at revision 25, and the members of their root's source metadata
 * the mirror holds instead (conformance/zarrify/compare.py's MIRRORED). */
const FROZEN_25: Record<string, string[]> = {
  nifti: ["header_rest", "extender", "extensions", "extensions_truncated", "unparsed", "trailing"],
};

/** The members of the root image's `ome` the Rust core writes and the frozen reference does
 * not, by profile (conformance/zarrify/compare.py's ADDED): TIFF's and NDPI's `omero`,
 * added after the freeze. */
const ADDED: Record<string, string[]> = { tiff: ["omero"], ndpi: ["omero"] };

const hex = (b: Uint8Array) => Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");

/** A range with its data source resolved to bytes (sources are numbered differently). */
function resolve(r: Range, sources: Source[]): unknown {
  if ("data" in r) return ["bytes", hex(r.data)];
  if (r.source === 0) return ["source", Number(r.offset), Number(r.length)];
  const d = sources[r.source].data!;
  return ["bytes", hex(d.subarray(Number(r.offset), Number(r.offset + r.length)))];
}

/** The entries outside the source metadata node (`refkv_source/`, or, in the frozen
 * reference's output, `vzip_source/`): documents parsed, other bytes as hex, ranges resolved.
 * Given `frozen` (the frozen reference's revision; 25 for the profiles of FROZEN_25),
 * the documents read with the current names, and a root that records it reads without it
 * (the shipped code records no revision) and without the members the mirror holds instead;
 * without `frozen`, a root reads without the members the reference does not write (ADDED). */
export function hierarchy(desc: ArchiveDesc, frozen?: number): Map<string, unknown> {
  const out = new Map<string, unknown>();
  const named = (json: unknown) => (frozen === undefined ? json : currentNames(json)) as Record<string, any>;
  for (const e of desc.entries) {
    if (e.key.startsWith(`${SOURCE_NODE}/`) || (frozen !== undefined && e.key.startsWith(`${LEGACY_NODE}/`))) continue;
    if ("ranges" in e) out.set(e.key, { ranges: e.ranges.map((r) => resolve(r, desc.sources)) });
    else if (e.key === "zarr.json") {
      const json = named(JSON.parse(new TextDecoder().decode(e.bytes)));
      const prop = json.attributes?.[CONVENTION_KEY];
      const moved = FROZEN_25[prop?.profile];
      if (frozen !== undefined && prop?.revision === (moved ? REVISION_25 : frozen)) {
        delete prop.revision;
        for (const k of moved ?? []) delete prop[prop.profile]?.[k];
      }
      if (frozen === undefined) for (const k of ADDED[prop?.profile] ?? []) delete json.attributes?.ome?.[k];
      out.set(e.key, { json, compress: !!e.compress });
    } else if (e.key.endsWith(".json")) out.set(e.key, { json: named(JSON.parse(new TextDecoder().decode(e.bytes))), compress: !!e.compress });
    else out.set(e.key, { bytes: hex(e.bytes), compress: !!e.compress });
  }
  return out;
}
