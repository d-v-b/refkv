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

/** JSON.parse's reviver keeping an integer literal past 2^53 − 1 exact, as a bigint: the Rust core
 * writes every integer exactly (spec/conventions.md §6
 * <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#6-source-metadata-as-json>). */
function exact(_key: string, value: unknown, context?: { source?: string }): unknown {
  return typeof value === "number" && !Number.isSafeInteger(value) && /^-?[0-9]+$/.test(context?.source ?? "")
    ? BigInt(context!.source!)
    : value;
}

const isObject = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const DECIMAL = /^[+-]?[0-9]+$/;

/** The reference's digit string of an integer past 2^53 − 1: no `+`, no leading zeros. */
const isBigDigits = (ref: unknown): ref is string =>
  typeof ref === "string" && /^-?[1-9][0-9]{15,}$/.test(ref) && !Number.isSafeInteger(Number(ref));

/** The integer the reference's `ref` writes as an integer: a JSON integer, ND2's tag
 * `{"int": digits}`, or its digit string past 2^53 − 1; undefined for anything else, any other
 * string among them. */
function integerForm(ref: unknown): bigint | undefined {
  if (typeof ref === "number" && Number.isInteger(ref)) return BigInt(ref);
  if (isObject(ref) && Object.keys(ref).join() === "int" && typeof ref.int === "string" && DECIMAL.test(ref.int)) return BigInt(ref.int);
  if (isBigDigits(ref)) return BigInt(ref);
  return undefined;
}

/** Whether the member `key` of `ours` holds values whose format declares their integers' range:
 * a DICOM `IS` attribute's `Value`, and an ND2 XML variant's document, a chunk `CustomDataVar|...`
 * (spec/zarrify/dicom.md §5.2
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/dicom.md#52-the-datasets-as-json>,
 * spec/zarrify/nd2.md §5.1
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nd2.md#51-decoded-chunks>). */
const rangedMember = (key: string, ours: Record<string, unknown>) =>
  (key === "Value" && ours.vr === "IS") || key.startsWith("CustomDataVar|");

const INTEGER_VRS = new Set(["SL", "SS", "UL", "US", "SV", "UV", "IS"]);

/** Whether the member `key` of `ours` (itself the member `parent`) holds numbers of the source,
 * which the reference writes past 2^53 − 1 as their digit strings: a DICOM attribute's `Value` of
 * an integer VR, an HDF5 attribute's `value` of an integer datatype (IMS), NIfTI's `header`, and
 * NDPI's `ExposureTime`. */
function numericMember(parent: string, key: string, ours: Record<string, unknown>): boolean {
  const datatype = ours.datatype;
  const integer = isObject(datatype) && ["integer", "bitfield", "enum"].includes(datatype.class as string);
  return (key === "Value" && INTEGER_VRS.has(ours.vr as string)) || (key === "value" && integer)
    || (key === "header" && parent === "nifti") || key === "ExposureTime";
}

/**
 * The frozen reference's JSON value `ref`, each of its forms of an integer read as `ours` writes it
 * where `ours` has that integer (a bigint past 2^53 − 1): in a member that holds numbers
 * (`numericMember`), its digit string as the exact integer; ND2's tag `{"int": digits}` as the exact
 * integer; in a value whose format declares a range (`rangedMember`), its integer forms that `ours`
 * writes as decimal text, outside that range, as that text; and the pairs `[["int", v]]` of a record
 * named `int` alone as the object `ours` has (conformance/zarrify/exact_integers.py). Anything else
 * stays as the reference wrote it.
 */
export function exactIntegers(ref: unknown, ours: unknown, parent = "", numeric = false, ranged = false): unknown {
  if (typeof ours === "bigint") {
    if (numeric && typeof ref === "string" && ref === ours.toString()) return ours;
    if (isObject(ref) && Object.keys(ref).join() === "int" && ref.int === ours.toString()) return ours;
    return ref;
  }
  if (ranged && typeof ours === "string" && DECIMAL.test(ours) && integerForm(ref) === BigInt(ours)) return ours;
  if (isObject(ours) && Object.keys(ours).join() === "int" && Array.isArray(ref) && ref.length === 1
    && Array.isArray(ref[0]) && ref[0].length === 2 && ref[0][0] === "int") {
    return { int: exactIntegers(ref[0][1], ours.int, "int", numeric, ranged) };
  }
  if (Array.isArray(ref) && Array.isArray(ours) && ref.length === ours.length) {
    return ref.map((x, i) => exactIntegers(x, ours[i], parent, numeric, ranged));
  }
  if (isObject(ref) && isObject(ours)) {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(ref)) {
      const value = Object.hasOwn(ours, k)
        ? exactIntegers(v, ours[k], k, numeric || numericMember(parent, k, ours), ranged || rangedMember(k, ours))
        : v;
      Object.defineProperty(out, k, { value, enumerable: true, writable: true, configurable: true });
    }
    return out;
  }
  return ref;
}

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
 * without `frozen`, a root reads without the members the reference does not write (ADDED).
 * With `bigints`, an integer literal past 2^53 − 1 reads as a bigint. */
export function hierarchy(desc: ArchiveDesc, frozen?: number, bigints = false): Map<string, unknown> {
  const out = new Map<string, unknown>();
  const named = (json: unknown) => (frozen === undefined ? json : currentNames(json)) as Record<string, any>;
  // with `bigints`, integers past 2^53 − 1 exact (`exact`), as the Rust core writes them
  const parse = (b: Uint8Array) => JSON.parse(new TextDecoder().decode(b), bigints ? exact as never : undefined);
  for (const e of desc.entries) {
    if (e.key.startsWith(`${SOURCE_NODE}/`) || (frozen !== undefined && e.key.startsWith(`${LEGACY_NODE}/`))) continue;
    if ("ranges" in e) out.set(e.key, { ranges: e.ranges.map((r) => resolve(r, desc.sources)) });
    else if (e.key === "zarr.json") {
      const json = named(parse(e.bytes));
      const prop = json.attributes?.[CONVENTION_KEY];
      const moved = FROZEN_25[prop?.profile];
      if (frozen !== undefined && prop?.revision === (moved ? REVISION_25 : frozen)) {
        delete prop.revision;
        for (const k of moved ?? []) delete prop[prop.profile]?.[k];
      }
      if (frozen === undefined) for (const k of ADDED[prop?.profile] ?? []) delete json.attributes?.ome?.[k];
      out.set(e.key, { json, compress: !!e.compress });
    } else if (e.key.endsWith(".json")) out.set(e.key, { json: named(parse(e.bytes)), compress: !!e.compress });
    else out.set(e.key, { bytes: hex(e.bytes), compress: !!e.compress });
  }
  return out;
}
