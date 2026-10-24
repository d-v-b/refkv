// `#profiles` in a bundle (package.json "imports"): the registry's entries, by name, which
// build.mjs inlines as `__REFKV_PROFILES__` from spec/virtualize/profiles/<name>.json.

import type { ProfileEntry } from "./profiles.ts";

declare const __REFKV_PROFILES__: ProfileEntry[];

export const REGISTRY: ProfileEntry[] = __REFKV_PROFILES__;
