// NDPI in the browser zarrifier, through the Rust core (spec/zarrify/ndpi.md §7
// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/ndpi.md#7-ndpi-profile>): the
// data sources that hold the strips' headers, and rejections. The JPEG header parser is
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
