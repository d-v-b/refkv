// The demo's Python script (demo/python.ts): its name, dependencies and code for each kind of input.

import assert from "node:assert/strict";
import { test } from "node:test";

import { pythonScript, stem } from "../../demo/python.ts";

test("pythonScript names the input, its extra and the release, and zarrifies or opens it", () => {
  const cases: [url: string, isArchive: boolean, profile: string | undefined, release: string | undefined, has: string[], lacks: string[]][] = [
    ["https://example.org/a%20b.ome.tiff", false, "tiff", "0.2.0",
      ['dependencies = ["refkv[images]>=0.2.0"]', 'reference = zarrify("https://example.org/a%20b.ome.tiff")',
        'reference.save("a_b.ome.vzip")'],
      ["store = refkv.zarr.open("]],
    ["https://zenodo.org/api/records/1/files/s.ndpi/content", false, "ndpi", "0.2.0rc1",
      ['"refkv[images]>=0.2.0rc1"', '"""Zarrify s and open'], []],
    ["https://example.org/x.mp3", false, "mp3", undefined, ['dependencies = ["refkv[media]"]'], ["refkv[media]>="]],
    ["https://example.org/v.mov", false, "mp4", "0.2.0", ['"refkv[media]>=0.2.0"'], []],
    ["https://example.org/data/img.zarr/", false, "ome-zarr", "0.2.0", ['reference.save("img.vzip")'], []],
    ["https://example.org/out.vzip", true, "czi", "0.2.0",
      ['store = refkv.zarr.open("https://example.org/out.vzip")', '"""Open the vzip archive out.vzip'],
      ["zarrify", ".save("]],
  ];
  for (const [url, isArchive, profile, release, has, lacks] of cases) {
    const script = pythonScript(url, isArchive, profile, release);
    assert.ok(script.startsWith('# /// script\n# requires-python = ">=3.12"\n'), url);
    assert.ok(script.includes('root = zarr.open(store, mode="r")'), url);
    for (const s of has) assert.ok(script.includes(s), `${url}: ${s}`);
    for (const s of lacks) assert.ok(!script.includes(s), `${url}: not ${s}`);
  }
  assert.equal(stem("https://example.org/"), "input");
});
