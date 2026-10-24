// `#profiles` in Node (package.json "imports"): the registry's entries read from the spec,
// spec/virtualize/profiles/<name>.json, by name.

import { readdirSync, readFileSync } from "node:fs";
import type { ProfileEntry } from "./profiles.ts";

const dir = new URL("../../../spec/virtualize/profiles/", import.meta.url);

export const REGISTRY: ProfileEntry[] = readdirSync(dir)
  .filter((f) => f.endsWith(".json"))
  .map((f) => JSON.parse(readFileSync(new URL(f, dir), "utf8")) as ProfileEntry)
  .sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
