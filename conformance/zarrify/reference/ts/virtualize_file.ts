// Virtualizes a local image file, or a local directory as a store (§1.4,
// §1.5), with the browser code as it was before the Rust core (the frozen TIFF,
// ND2 and CZI virtualizers of this directory; the shipped code for the other
// profiles), under Node, for comparisons with js/conformance/zarrify_file.ts:
//   node conformance/zarrify/reference/ts/virtualize_file.ts <file or directory> <out.vzip> <source url>
// For a directory, <source url> is the store URL it is served from (ending in
// "/"). Prints the summary as JSON; exits 1 if the input is rejected, 2 on a
// crash.

import fs from "node:fs";
import { blockReader, ImageError } from "./legacy.ts";
import { DicomError } from "./dicom/virtualize.ts";
import { N5Error } from "./n5/virtualize.ts";
import { StoreError } from "./legacy.ts";
import { Zarr2Error } from "./zarr2/virtualize.ts";
import { OmeZarrError } from "./ome-zarr/virtualize.ts";
import { NiftiError } from "./nifti/virtualize.ts";
import { ImsError } from "./ims/virtualize.ts";
import { SafeError } from "./safe/virtualize.ts";
import { writeVzip } from "../../../../js/src/writer.ts";
import { directoryStore } from "../../../../js/conformance/directory_store.ts";
import { CziError } from "./czi/virtualize.ts";
import { virtualizeImage, virtualizeStore } from "./index.ts";
import { LVError } from "./nd2/lv.ts";
import { Nd2Error } from "./nd2/virtualize.ts";
import { TiffError } from "./tiff/ifd.ts";

const [inPath, outPath, url] = process.argv.slice(2);

try {
  let virtual;
  if (fs.statSync(inPath).isDirectory()) {
    virtual = await virtualizeStore(directoryStore(inPath, url));
  } else {
    const bytes = new Uint8Array(fs.readFileSync(inPath));
    const read = blockReader(async (o, n) => bytes.subarray(o, o + n), bytes.length);
    virtual = await virtualizeImage(url, read, bytes.length);
  }
  fs.writeFileSync(outPath, await writeVzip(virtual));
  console.log(JSON.stringify(virtual.summary));
} catch (e) {
  if (!(e instanceof TiffError || e instanceof Nd2Error || e instanceof LVError || e instanceof DicomError ||
    e instanceof NiftiError || e instanceof ImsError || e instanceof ImageError || e instanceof StoreError || e instanceof N5Error || e instanceof Zarr2Error || e instanceof OmeZarrError || e instanceof SafeError || e instanceof CziError)) {
    console.error(e);
    process.exit(2); // a crash, not a refusal
  }
  console.error(`unsupported: ${e.message}`);
  process.exitCode = 1;
}
