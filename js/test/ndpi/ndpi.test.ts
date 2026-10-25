// NDPI in the browser zarrifier, through the Rust core (spec/zarrify/ndpi.md §7
// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#7-ndpi-profile>): the
// data sources that hold the strips' headers, JPEG XR levels, and rejections. The JPEG header parser is
// tested in Rust (rust/refkv-ir/src/ndpi.rs); whole files are checked against tifffile
// by ndpi/verify.py.

import assert from "node:assert/strict";
import fs from "node:fs";
import { test } from "node:test";
import { zarrifyImage } from "../../src/zarrify/index.ts";

async function zarrifyFixture(name: string) {
  const bytes = new Uint8Array(fs.readFileSync(new URL(`../../../fixtures/ndpi/${name}`, import.meta.url)));
  return zarrifyImage(`https://data.test/${name}`, async (o, n) => bytes.subarray(o, o + n), bytes.length);
}

test("an NDPI is zarrified as NDPI, the header around SOF0 in data sources numbered in order of first use", async () => {
  const out = await zarrifyFixture("ndpi_levels.ndpi");
  assert.equal(out.format, "ndpi");
  assert.equal(out.sources[0].url, "https://data.test/ndpi_levels.ndpi");
  const data = out.sources.slice(1).map((s) => s.data!);
  assert.ok(data.length >= 2 && data[0][0] === 0xff && data[0][1] === 0xd8);
  let next = 1;
  for (const e of out.entries) {
    if (!("ranges" in e) || e.ranges.length === 1 || e.key.startsWith("refkv_source/")) continue;
    const [before, sof, after] = e.ranges;
    assert.ok("data" in sof && sof.data[1] === 0xc0, e.key);
    for (const r of [before, after]) {
      assert.ok("source" in r && r.source >= 1 && r.offset === 0n, e.key);
      assert.equal(r.length, BigInt(data[r.source - 1].length), e.key);
      assert.ok(r.source <= next, e.key); // a new source takes the next number
      if (r.source === next) next++;
    }
  }
  assert.equal(next, data.length + 1);
});

for (const [name, message] of [
  ["edge_reject_ndpi_wide_interval.ndpi", /65536 pixels wide/],
  ["edge_reject_ndpi_short_strip.ndpi", /3 bytes is shorter than 4/],
] as const) {
  test(`rejects ${name}`, async () => {
    await assert.rejects(zarrifyFixture(name), message);
  });
}

test("the values of an IFD that is not a level are not checked", async () => {
  const out = await zarrifyFixture("ndpi_unused_values.ndpi");
  assert.deepEqual((out.summary as { levels: number[][] }).levels, [[3, 48, 64]]);
});

function json(out: Awaited<ReturnType<typeof zarrifyFixture>>, key: string) {
  const e = out.entries.find((x) => x.key === key);
  assert.ok(e && "bytes" in e, key);
  return JSON.parse(new TextDecoder().decode(e.bytes));
}

test("a JPEG XR NDPI's levels have their streams' pixels, each tile a chunk as it is", async () => {
  // spec/zarrify/ndpi.md §3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#3-levels>:
  // 16-bit grayscale, whatever the tags say; a tile of byte count 0 has no chunk
  const out = await zarrifyFixture("ndpi_jpegxr_gray16.ndpi");
  assert.equal(out.format, "ndpi");
  assert.deepEqual((out.summary as { levels: number[][] }).levels, [[1, 100, 160], [1, 50, 80], [1, 25, 40]]);
  const level = json(out, "0/zarr.json");
  assert.equal(level.data_type, "uint16");
  assert.deepEqual(level.codecs, [{ name: "imagecodecs_jpegxr" }]);
  const chunks = out.entries.filter((e) => /^1\/c\//.test(e.key)).map((e) => e.key);
  assert.equal(chunks.length, 5);
  assert.ok(!chunks.includes("1/c/0/1/1"));
  for (const e of out.entries) {
    if (/^\d\/c\//.test(e.key)) assert.ok("ranges" in e && e.ranges.length === 1, e.key);
  }
  const root = json(out, "zarr.json").attributes;
  assert.deepEqual(root.refkv_zarrified.ndpi, { Fluorescence: "TxRed", Wavelength: 650, ExposureTime: 1024000 });
  assert.equal(root.ome.omero.channels[0].label, "TxRed");
});
