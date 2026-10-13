// Zarr v2 stores through the Rust core (src/virtualize/ir/store.ts, rust/refkv-ir/src/zarr2.rs):
// on every Zarr v2 fixture store, the core accepts what the frozen TypeScript twin accepts and
// writes its hierarchy outside `refkv_source` (each reference to the same object URL, its size
// pinned), and rejects what it rejects, as a StoreError; `refkv_source` is the IR's mirror, and
// the ignored keys a listing records are the member `ignored` of its source metadata.

import assert from "node:assert/strict";
import fs from "node:fs";
import { test } from "node:test";
import { isDeepStrictEqual } from "node:util";
import { fileURLToPath } from "node:url";
import { directoryStore } from "../../conformance/directory_store.ts";
import { virtualizeStore as referenceStore } from "../../../conformance/virtualize/reference/ts/index.ts";
import { STORES_REVISION as FROZEN } from "../../../conformance/virtualize/reference/ts/revision.ts";
import { currentNames, SOURCE_NODE as LEGACY_NODE } from "../../../conformance/virtualize/reference/ts/legacy.ts";
import { REVISION } from "../../src/virtualize/common.ts";
import { virtualizeStore } from "../../src/virtualize/index.ts";
import { StoreError } from "../../src/virtualize/store.ts";
import type { ArchiveDesc } from "../../src/writer.ts";

const FIXTURES = fileURLToPath(new URL("../../../fixtures/zarr2/", import.meta.url));

/** The entries outside refkv_source/ (the frozen twin's, given `legacy`, outside its vzip_source/,
 * its documents read with the current names): documents parsed (the frozen root's revision read
 * as the current one), references as (object URL, pinned size, offset, length). */
function hierarchy(desc: ArchiveDesc, legacy = false): Map<string, unknown> {
  const out = new Map<string, unknown>();
  for (const e of desc.entries) {
    if (e.key.startsWith("refkv_source/") || (legacy && e.key.startsWith(`${LEGACY_NODE}/`))) continue;
    if ("ranges" in e) {
      out.set(e.key, e.ranges.map((r) => {
        assert.ok(!("data" in r));
        const s = desc.sources[r.source];
        return [s.url, s.size, r.offset, r.length];
      }));
    } else {
      const parsed = JSON.parse(new TextDecoder().decode(e.bytes));
      const json = (legacy ? currentNames(parsed) : parsed) as { attributes?: { refkv_virtualized?: { revision?: number } } };
      const prop = json.attributes?.refkv_virtualized;
      if (e.key === "zarr.json" && prop?.revision === FROZEN) prop.revision = REVISION;
      out.set(e.key, json);
    }
  }
  return out;
}

test("the core virtualizes every Zarr v2 fixture store as the frozen twin does, and mirrors it", async () => {
  let accepted = 0;
  for (const name of fs.readdirSync(FIXTURES).sort()) {
    const url = `https://data.test/zarr2/${name}/`;
    const ref = await referenceStore(directoryStore(FIXTURES + name, url)).catch((e: Error) => e);
    const ir = await virtualizeStore(directoryStore(FIXTURES + name, url)).catch((e: Error) => e);
    if (ref instanceof Error) {
      assert.ok(ir instanceof StoreError, `${name}: the twin rejects (${ref.message}), the core gives ${ir}`);
      continue;
    }
    assert.ok(!(ir instanceof Error), `${name}: ${ir}`);
    accepted++;
    assert.equal(ir.format, "zarr2");
    assert.ok(isDeepStrictEqual(hierarchy(ir), hierarchy(ref, true)), name);
    const source = ir.entries.find((e) => e.key === "refkv_source/zarr.json") as { bytes: Uint8Array };
    const own = JSON.parse(new TextDecoder().decode(source.bytes)).attributes.refkv_virtualized.zarr2;
    assert.deepEqual(Object.keys(own), ["ir"], name); // a directory records no ignored key
    assert.equal(own.ir.objects, directoryStore(FIXTURES + name, url).objects.size, name);
  }
  assert.ok(accepted >= 14, `${accepted}`);
});

test("the ignored keys a listing records are refkv_source's member ignored, in UTF-8 byte order", async () => {
  for (const [ignored, want] of [[[], undefined], [["b//c", "./x", "\u00e9/.."], ["./x", "b//c", "\u00e9/.."]]] as const) {
    const store = directoryStore(FIXTURES + "zarr2_hierarchy", "https://data.test/h/");
    store.ignored = [...ignored];
    const v = await virtualizeStore(store);
    const e = v.entries.find((x) => x.key === "refkv_source/zarr.json") as { bytes: Uint8Array };
    const own = JSON.parse(new TextDecoder().decode(e.bytes)).attributes.refkv_virtualized.zarr2;
    assert.deepEqual(own.ignored, want);
    assert.ok(!v.entries.some((x) => x.key === "refkv_source/ignored.json"));
  }
});
