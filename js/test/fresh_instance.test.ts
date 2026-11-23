// The entry points that call the core's host rules instantiate the core first: in a process of
// its own (each test file is one), a scoped zarrifyToArchive is the first use of the core. And
// the browser's inputs are URL references: a relative one's directory is its URL's.

import assert from "node:assert/strict";
import fs from "node:fs";
import { test } from "node:test";
import { zarrifyToArchive } from "../src/embed.ts";
import { splitVsiUrl } from "../src/zarrify/ir/vsi.ts";

const FIXTURES = new URL("../../fixtures/", import.meta.url);

test("a scoped zarrifyToArchive instantiates the core before the scope rule", async () => {
  const bytes = new Uint8Array(fs.readFileSync(new URL("nifti/nifti_block_x_split.nii", FIXTURES)));
  const url = "https://data.test/nifti/nifti_block_x_split.nii";
  const z = await zarrifyToArchive({ url, size: bytes.length, read: async (o, n) => bytes.slice(o, o + n) }, { scope: [url] });
  assert.equal(z.format, "nifti");
  assert.deepEqual(splitVsiUrl("data/scan.nhdr", "NRRD"), ["data/", "scan.nhdr"]);
  assert.deepEqual(splitVsiUrl("/abs/a%20b.vsi"), ["/abs/", "a b.vsi"]);
});
