import assert from "node:assert/strict";
import { test } from "node:test";
import type { RawOutput } from "../../src/virtualize/ir/run.ts";
import { storeDesc } from "../../src/virtualize/ir/store.ts";

// The browser's store output (src/virtualize/ir/store.ts): one url source per object a
// reference uses, in order of first use (references by key, ranges in order), each pinning
// its listed size; then the data sources, by first use too; literals stay.
test("a store output numbers its sources by first use", () => {
  const raw: RawOutput = {
    url: "https://h/b/s/",
    summary: {},
    lazy: ["refkv_source/"],
    data: [Uint8Array.of(0x61, 0x62), Uint8Array.of(0x63, 0x64, 0x65)],
    entries: [["zarr.json", Uint8Array.of(0x7b, 0x7d)]],
    refs: [
      ["b/c/0", [{ kind: 2, data: Uint8Array.of(1) }, { kind: 3, key: "x.jp2", offset: 10n, length: 4n }, { kind: 1, source: 2, offset: 0n, length: 3n }]],
      ["a/c/0", [{ kind: 3, key: "y.jp2", offset: 0n, length: 8n }, { kind: 1, source: 2, offset: 0n, length: 3n }, { kind: 1, source: 1, offset: 0n, length: 2n }]],
      ["z", [{ kind: 3, key: "x.jp2", offset: 0n, length: 1n }, { kind: 3, key: "w.jp2", offset: 0n, length: 1n }]],
    ],
  };
  const desc = storeDesc(raw, new Map([["x.jp2", 20], ["y.jp2", 9]]));
  assert.deepEqual(desc.sources, [
    { url: "https://h/b/s/y.jp2", size: 9n },
    { url: "https://h/b/s/x.jp2", size: 20n },
    { url: "https://h/b/s/w.jp2" },
    { data: Uint8Array.of(0x63, 0x64, 0x65) },
    { data: Uint8Array.of(0x61, 0x62) },
  ]);
  assert.deepEqual(desc.entries, [
    { key: "zarr.json", bytes: Uint8Array.of(0x7b, 0x7d) },
    { key: "a/c/0", ranges: [{ source: 0, offset: 0n, length: 8n }, { source: 3, offset: 0n, length: 3n }, { source: 4, offset: 0n, length: 2n }] },
    { key: "b/c/0", ranges: [{ data: Uint8Array.of(1) }, { source: 1, offset: 10n, length: 4n }, { source: 3, offset: 0n, length: 3n }] },
    { key: "z", ranges: [{ source: 1, offset: 0n, length: 1n }, { source: 2, offset: 0n, length: 1n }] },
  ]);
});

test("a store output refuses a reference to source 0", () => {
  const raw: RawOutput = { url: "https://h/", summary: {}, lazy: [], data: [], entries: [],
                           refs: [["a", [{ kind: 0, offset: 0n, length: 1n }]]] };
  assert.throws(() => storeDesc(raw, new Map()), /source 0 in a store's output/);
});
