// The names the frozen profiles write: those of the revisions they implement (20 for
// TIFF, ND2 and CZI, 25 for the profiles frozen later), which revision 27 changed. Their source metadata node
// is `vzip_source`, their convention's property `vzip_virtualized`, and the
// convention metadata names its URLs under d-v-b/vzip and calls the layout
// "virtualized by vzip".
//
// The frozen profiles import what they share with the shipped code from here: this
// module is src/zarrify/common.ts and store.ts, but for those names and the
// functions that write them (`convention`, `declare`, `emitPlans`, `objectKey`,
// `keepObjects`, `storeArchive`). A store twin also rejects a node at `refkv_source`,
// where the shipped code keeps a store's other objects since revision 27, so that the
// corpus's collision stores are rejected by every implementation. `currentNames` reads
// the old names as the current ones, for the tests that compare the frozen profiles with
// the Rust core, as compare.py does (conformance/zarrify/HARNESS.md), and the codecs they
// name as the shipped code names them (`CODEC_NAMES`).

import {
  convention as currentConvention, CONVENTION_KEY as CURRENT_KEY, planEntries, type Plan, PROFILES,
  type Profile, rootProperty, SOURCE_NODE as CURRENT_NODE, stringifyJson,
} from "../../../../js/src/zarrify/common.ts";
import type { Range } from "../../../../js/src/protobuf.ts";
import {
  compareKeys, docKey, type Json, objectUrl, OBJECTS as CURRENT_OBJECTS, SOURCE_GROUP as CURRENT_GROUP,
  StoreError, type StoreResult,
} from "../../../../js/src/zarrify/store.ts";
import type { ArchiveDesc, EntryDesc } from "../../../../js/src/writer.ts";

export * from "../../../../js/src/zarrify/common.ts";
export * from "../../../../js/src/zarrify/store.ts";

export const SOURCE_NODE = "vzip_source";
export const CONVENTION_KEY = "vzip_virtualized";
export const SOURCE_GROUP = SOURCE_NODE;
export const OBJECTS = `${SOURCE_NODE}/objects/`;
export const EMPTY_KEY = `${SOURCE_NODE}/empty.json`;
const NODE_NAME = /^(?:zarr\.json|\.zarray|\.zgroup)~*$/;
// The codecs the frozen profiles name that the shipped code names otherwise: a zlib stream
// is zarr-python's `numcodecs.zlib`, and refkv's own TIFF LZW codec `refkv.tiff_lzw`.
const CODEC_NAMES: Record<string, string> = { zlib: "numcodecs.zlib", imagecodecs_lzw: "refkv.tiff_lzw" };

export function objectKey(key: string): string {
  return OBJECTS + key + (NODE_NAME.test(key.slice(key.lastIndexOf("/") + 1)) ? "~" : "");
}

type Entry = { key: string; bytes: Uint8Array; compress?: boolean } | { key: string; ranges: Range[] };

export function convention(profile: Profile): { [k: string]: string } {
  const [uuid, version, title] = PROFILES[profile];
  const tag = `virtualize-${profile}-v${version}`;
  const [ref, blob] = version === 0 ? ["heads/main", "main"] : [`tags/${tag}`, tag];
  return {
    uuid,
    schema_url: `https://raw.githubusercontent.com/d-v-b/vzip/refs/${ref}/spec/zarrify/${profile}/schema.json`,
    spec_url: `https://github.com/d-v-b/vzip/blob/${blob}/spec/zarrify/${profile}.md`,
    name: CONVENTION_KEY,
    description: `The Zarr layout of a ${title} source virtualized by vzip, and the source's metadata`,
  };
}

export function declare(
  attributes: { [k: string]: unknown },
  profile: Profile,
  url: string | undefined,
  own?: object,
  revision?: number,
): { [k: string]: unknown } {
  const value: { [k: string]: unknown } = url === undefined
    ? {}
    : rootProperty(profile, url, revision);
  if (own !== undefined && Object.keys(own).length > 0) value[profile] = own;
  if (Object.keys(value).length === 0) return { ...attributes };
  return { ...attributes, zarr_conventions: [convention(profile)], [CONVENTION_KEY]: value };
}

export function emitPlans(plans: Plan[], nodeAttributes: Record<string, unknown>): Entry[] {
  const utf8 = new TextEncoder();
  const group = (attributes: unknown) => utf8.encode(stringifyJson({ zarr_format: 3, node_type: "group", attributes }));
  const out: Entry[] = [{ key: `${SOURCE_NODE}/zarr.json`, bytes: group(nodeAttributes), compress: true }];
  const groups = new Set<string>();
  for (const a of plans) {
    const parts = a.path.split("/");
    for (let k = 1; k < parts.length; k++) groups.add(parts.slice(0, k).join("/"));
  }
  for (const g of [...groups].sort()) out.push({ key: `${SOURCE_NODE}/${g}/zarr.json`, bytes: group({}), compress: true });
  for (const a of plans) out.push(...planEntries(SOURCE_NODE, a));
  return out;
}

const PROFILE_OF_UUID = new Map(Object.entries(PROFILES).map(([p, [uuid]]) => [uuid, p as Profile]));

/** A JSON value of a frozen profile's output with the names the shipped code writes: the
 * convention's property, its convention metadata, the codecs of `CODEC_NAMES`, and, within the property (where the
 * profiles name them), paths into the source metadata node. The source's own values,
 * outside the property, are left as they are; within it, a value copied from the source
 * that starts with `vzip_source/` would read as such a path too, which no fixture has. */
export function currentNames(v: unknown, inside = false): unknown {
  if (Array.isArray(v)) return v.map((x) => currentNames(x, inside));
  if (typeof v === "string") {
    return inside && v.startsWith(`${SOURCE_NODE}/`) ? CURRENT_NODE + v.slice(SOURCE_NODE.length) : v;
  }
  if (v === null || typeof v !== "object") return v;
  const out: Record<string, unknown> = {};
  for (let [k, x] of Object.entries(v)) {
    if (k === "codecs" && Array.isArray(x)) {
      x = x.map((c) => typeof c?.name === "string" && Object.hasOwn(CODEC_NAMES, c.name) ? { ...c, name: CODEC_NAMES[c.name] } : c);
    }
    const legacy = (m: { uuid?: string; name?: string }) => m?.name === CONVENTION_KEY && PROFILE_OF_UUID.has(m.uuid!);
    out[k === CONVENTION_KEY ? CURRENT_KEY : k] = k === "zarr_conventions" && Array.isArray(x)
      ? x.map((m) => legacy(m) ? currentConvention(PROFILE_OF_UUID.get(m.uuid)!) : currentNames(m, inside))
      : currentNames(x, inside || k === CONVENTION_KEY);
  }
  return out;
}

/** src/zarrify/store.ts's `keepObjects`, with this module's names. */
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
      for (const [group, objects] of [[CURRENT_GROUP, CURRENT_OBJECTS], [SOURCE_GROUP, OBJECTS]]) {
        if (p === group || p.startsWith(group + "/")) {
          fail(`the node ${p} is where the store's other objects go (${objects}...)`);
        }
      }
    }
    const profile = root.attributes[CONVENTION_KEY].profile as Profile;
    result.docs.set(docKey(SOURCE_GROUP), { zarr_format: 3, node_type: "group", attributes: declare({}, profile, undefined, own) as Json });
  } else if (own) {
    result.docs.set(EMPTY_KEY, own);
  }
  result.origins ??= new Map();
  for (const [k] of kept) result.origins.set(objectKey(k), k);
  result.chunks = [...result.chunks, ...kept.map(([k, n]): [string, number] => [objectKey(k), n])]
    .sort((a, b) => compareKeys(a[0], b[0]));
  return kept.length;
}

const utf8 = new TextEncoder();

/** src/zarrify/store.ts's `storeArchive`, with this module's names. */
export function storeArchive(url: string, { docs, chunks, origins }: StoreResult): ArchiveDesc {
  const reject = (message: string): never => {
    throw new StoreError(message);
  };
  for (const key of [...docs.keys(), ...chunks.map(([k]) => k)]) {
    if (utf8.encode(key).length > 65535) reject("an output key is longer than 65535 bytes");
    if (key.startsWith("__vz__/")) reject(`output key ${JSON.stringify(key)} is in the reserved __vz__/ space`);
  }
  const entries: EntryDesc[] = chunks.map(([key, size], i) => ({
    key, ranges: [{ source: i, offset: 0n, length: BigInt(size) }],
  }));
  for (const [key, doc] of docs) {
    const compress = key.startsWith(SOURCE_GROUP + "/");
    entries.push({ key, bytes: utf8.encode(stringifyJson(doc)), ...(compress ? { compress } : {}) });
  }
  return { sources: chunks.map(([key, size]) => ({ url: objectUrl(url, origins?.get(key) ?? key), size: BigInt(size) })), entries };
}
