// Zarr v2 stores through the Rust core (src/virtualize/ir/store.ts, rust/vzip-ir/src/zarr2.rs):
// on every Zarr v2 fixture store, the core accepts what the frozen TypeScript twin accepts and
// writes its hierarchy outside `vzip_source` (each reference to the same object URL, its size
// pinned), and rejects what it rejects, as a StoreError; `vzip_source` is the IR's mirror, and
// the ignored keys a listing records are `vzip_source/ignored.json`.

import assert from "node:assert/strict";
import fs from "node:fs";
import { test } from "node:test";
import { isDeepStrictEqual } from "node:util";
import { fileURLToPath } from "node:url";
import { directoryStore } from "../../conformance/directory_store.ts";
import { virtualizeStore as referenceStore } from "../../../conformance/virtualize/reference/ts/index.ts";
import { REVISION as FROZEN } from "../../../conformance/virtualize/reference/ts/zarr2/declare.ts";
import { REVISION } from "../../src/virtualize/common.ts";
import { virtualizeStore } from "../../src/virtualize/index.ts";
import { StoreError } from "../../src/virtualize/store.ts";
import type { ArchiveDesc } from "../../src/writer.ts";

const FIXTURES = fileURLToPath(new URL("../../../fixtures/zarr2/", import.meta.url));

/** The entries outside vzip_source/: documents parsed (the frozen root's revision read as the
 * current one), references as (object URL, pinned size, offset, length). */
function hierarchy(desc: ArchiveDesc): Map<string, unknown> {
  const out = new Map<string, unknown>();
  for (const e of desc.entries) {
    if (e.key.startsWith("vzip_source/")) continue;
    if ("ranges" in e) {
      out.set(e.key, e.ranges.map((r) => {
        assert.ok(!("data" in r));
        const s = desc.sources[r.source];
        return [s.url, s.size, r.offset, r.length];
      }));
    } else {
      const json = JSON.parse(new TextDecoder().decode(e.bytes));
      const prop = json.attributes?.vzip_virtualized;
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
    assert.ok(isDeepStrictEqual(hierarchy(ir), hierarchy(ref)), name);
    const source = ir.entries.find((e) => e.key === "vzip_source/zarr.json") as { bytes: Uint8Array };
    const own = JSON.parse(new TextDecoder().decode(source.bytes)).attributes.vzip_virtualized.zarr2;
    assert.deepEqual(Object.keys(own), ["ir"], name);
    assert.equal(own.ir.objects, directoryStore(FIXTURES + name, url).objects.size, name);
  }
  assert.ok(accepted >= 14, `${accepted}`);
});

test("the ignored keys a listing records are vzip_source/ignored.json", async () => {
  const store = directoryStore(FIXTURES + "zarr2_hierarchy", "https://data.test/h/");
  store.ignored = ["b//c", "./x"];
  const v = await virtualizeStore(store);
  const e = v.entries.find((x) => x.key === "vzip_source/ignored.json") as { bytes: Uint8Array };
  assert.equal(new TextDecoder().decode(e.bytes), '{"ignored":["./x","b//c"]}');
});
