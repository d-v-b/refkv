// A run's error in the browser host, by the core's variant (rust/refkv-ir/src/error.rs, read
// through `vz_failed`): a rejection of the file is an ImageError, and a failure of refkv's (here
// a host's misuse of the core, a format that is no file profile's) a plain Error, which is no
// rejection.

import assert from "node:assert/strict";
import { test } from "node:test";
import { ImageError } from "../../src/zarrify/common.ts";
import { type IrFormat, zarrifyIr } from "../../src/zarrify/ir/run.ts";
import { bytesSource } from "../../src/zarrify/ir/source.ts";

test("a run's rejection is an ImageError, and its failure a plain Error", async () => {
  const source = bytesSource(new Uint8Array(8));
  await assert.rejects(zarrifyIr("tiff", "https://data.test/a.tif", source), (e) => e instanceof ImageError);
  await assert.rejects(zarrifyIr("no-such-format" as IrFormat, "https://data.test/a.bin", source), (e) => {
    assert.ok(e instanceof Error && !(e instanceof ImageError), String(e));
    assert.equal((e as Error).message, 'no format "no-such-format"');
    return true;
  });
});
