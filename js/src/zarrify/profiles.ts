// The profile registry, spec/zarrify/profiles/<name>.json (one file per profile), as `#profiles`
// gives it: read from the spec in Node (profiles_node.ts) and inlined into the bundles by
// build.mjs (profiles_bundled.ts), so that adding a profile adds its file and edits nothing here.

import { REGISTRY as ENTRIES } from "#profiles";

/** A profile's or dataset layout's name: a registry entry's `name`. */
export type Profile = string;

/** A registry entry (spec/tools/profiles.py documents its members). */
export interface ProfileEntry {
  name: Profile;
  title: string;
  uuid: string;
  version: number;
  kind: "file" | "store" | "both" | "layout";
  /** Where the file profile's signature is tested among the others (lower first; ties by name),
   * or null for one a file's first bytes never select. */
  sniff: number | null;
  suffixes: string[];
  reference: number | null;
  source: string;
  inputs: string;
  for: string;
}

/** The registry's entries, by name. */
export const REGISTRY: readonly ProfileEntry[] = ENTRIES;

/** The file profiles' names (kind "file" or "both"): the formats of the core's file runs. */
export const FILE_FORMATS: ReadonlySet<Profile> = new Set(
  REGISTRY.filter((e) => e.kind === "file" || e.kind === "both").map((e) => e.name),
);
