// N5 stores through the Rust core in the browser's host (src/virtualize/ir/store.ts): every
// accepted fixture store gives the frozen TypeScript N5 virtualizer's hierarchy outside
// refkv_source (conformance/virtualize/reference/ts/n5, revision 25), the same sources and
// references, and a store mirror under refkv_source; every rejected one is rejected by both.

import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { isDeepStrictEqual } from "node:util";
import { directoryStore } from "../../conformance/directory_store.ts";
import { virtualizeStore as referenceStore } from "../../../conformance/virtualize/reference/ts/index.ts";
import { STORES_REVISION as FROZEN } from "../../../conformance/virtualize/reference/ts/revision.ts";
import { currentNames, SOURCE_NODE as LEGACY_NODE } from "../../../conformance/virtualize/reference/ts/legacy.ts";
import { virtualizeStore } from "../../src/virtualize/index.ts";
import { StoreError } from "../../src/virtualize/store.ts";
import type { ArchiveDesc } from "../../src/writer.ts";

const FIXTURES = fileURLToPath(new URL("../../../fixtures/n5/", import.meta.url));
const URL_OF = (name: string) => `https://data.test/n5/${name}/`;
const STORES = fs.readdirSync(FIXTURES).filter((n) => fs.statSync(FIXTURES + n).isDirectory()).sort();

/** The entries outside refkv_source (given `frozen`, the frozen twin's, outside its vzip_source,
 * its documents read with the current names): documents parsed (a root recording `frozen` read
 * at the current revision), references as (url, size) of their source. */
function hierarchy(desc: ArchiveDesc, frozen?: number): Map<string, unknown> {
  const out = new Map<string, unknown>();
  for (const e of desc.entries) {
    if (e.key.startsWith("refkv_source/") || (frozen !== undefined && e.key.startsWith(`${LEGACY_NODE}/`))) continue;
    if ("ranges" in e) {
      out.set(e.key, e.ranges.map((r) => ("source" in r ? [desc.sources[r.source].url, desc.sources[r.source].size, r.offset, r.length] : r)));
    } else {
      const parsed = JSON.parse(new TextDecoder().decode(e.bytes));
      // the frozen twin's documents, read with the current names
      const json = (frozen !== undefined ? currentNames(parsed) : parsed) as { attributes?: { refkv_virtualized?: { revision?: number } } };
      const prop = json.attributes?.refkv_virtualized;
      if (frozen !== undefined && prop?.revision === frozen) delete prop.revision;
      out.set(e.key, json);
    }
  }
  return out;
}

test("N5 stores through the core have the frozen virtualizer's hierarchy and a store mirror", async () => {
  let accepted = 0;
  for (const name of STORES) {
    if (name.startsWith("n5_reject_")) continue;
    const want = await referenceStore(directoryStore(FIXTURES + name, URL_OF(name)));
    const got = await virtualizeStore(directoryStore(FIXTURES + name, URL_OF(name)));
    assert.equal(got.format, "n5", name);
    assert.ok(isDeepStrictEqual(hierarchy(got), hierarchy(want, FROZEN)), name);
    assert.deepEqual(got.sources, want.sources, name);
    const src = got.entries.find((e) => e.key === "refkv_source/zarr.json");
    assert.ok(src && "bytes" in src, name);
    const own = JSON.parse(new TextDecoder().decode(src.bytes)).attributes.refkv_virtualized.n5;
    assert.equal(typeof own.ir.objects, "number", name);
    assert.ok(got.entries.some((e) => e.key === "refkv_source/ir/held/data/c/0"), name);
    if (!got.entries.some((e) => "ranges" in e)) assert.deepEqual(got.sources, [], name); // no reference, no source
    accepted++;
  }
  assert.equal(accepted, 17);
});

test("under a root dataset, the mirror's keys and the other objects are plain keys of refkv_source/", async () => {
  // spec/virtualize/n5.md §6 <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/n5.md#6-other-objects>:
  // no node is a child of the root array; the empty block is in the mirror's listing, not in empty.json
  for (const name of ["n5_root_dataset_objects", "n5_root_dataset_empty_block"]) {
    const v = await virtualizeStore(directoryStore(FIXTURES + name, URL_OF(name)));
    const keys = v.entries.map((e) => e.key);
    assert.ok(keys.includes("refkv_source/zarr.json") && !keys.includes("refkv_source/empty.json"), name);
    assert.deepEqual(keys.filter((k) => k.endsWith("zarr.json") && !k.startsWith("refkv_source/")), ["zarr.json"], name);
  }
  const v = await virtualizeStore(directoryStore(FIXTURES + "n5_root_dataset_objects", URL_OF("x")));
  assert.ok(v.entries.some((e) => e.key === "refkv_source/objects/README" && "ranges" in e));
});

test("a store that references no object has no source", async () => {
  const dir = fs.mkdtempSync(`${fs.realpathSync(os.tmpdir())}/n5-`);
  fs.writeFileSync(`${dir}/attributes.json`, '{"dimensions": [4], "blockSize": [2], "dataType": "uint8", "compression": {"type": "raw"}}');
  const v = await virtualizeStore(directoryStore(dir, "https://data.test/n5/none/"));
  assert.deepEqual(v.sources, []);
  assert.ok(!v.entries.some((e) => "ranges" in e));
  fs.rmSync(dir, { recursive: true });
});

test("a read that fails fails the run, and no other read of its batch starts after it", async () => {
  const objects = new Map<string, number>([["attributes.json", 2]]);
  for (let k = 0; k < 50; k++) objects.set(`g${k}/attributes.json`, 2);
  let reads = 0;
  const store = {
    url: "https://data.test/n5/failing/", objects, listed: objects.size, requests: 0, concurrency: 2,
    async read(key: string) {
      reads++;
      if (key !== "attributes.json") throw new Error("connection reset");
      return new TextEncoder().encode("{}");
    },
  };
  await assert.rejects(virtualizeStore(store), /connection reset/);
  assert.ok(reads <= 3, `${reads} reads`);
});

test("N5 stores the frozen virtualizer rejects, the core rejects", async () => {
  for (const name of STORES.filter((n) => n.startsWith("n5_reject_"))) {
    await assert.rejects(referenceStore(directoryStore(FIXTURES + name, URL_OF(name))), name);
    await assert.rejects(virtualizeStore(directoryStore(FIXTURES + name, URL_OF(name))), (e) => e instanceof StoreError, name);
  }
});
