// The embeddable zarrifier (src/embed.ts): a file the host reads itself is zarrified as
// zarrifySource does, its VSI tile files opened through the host, with or without its
// sources' size pins; a refkv.fits_tiles chunk decodes as refkv.zarr's codec decodes it (the
// same Rust core); and one test per way either fails.

import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import fs from "node:fs";
import { test } from "node:test";
import { Archive } from "../src/archive.ts";
import { decodeFitsTiles, type FileInput, zarrifyToArchive } from "../src/embed.ts";
import { ImageError } from "../src/zarrify/common.ts";
import { zarrifySource } from "../src/zarrify/index.ts";
import { bytesSource } from "../src/zarrify/ir/source.ts";
import { writeVzip } from "../src/writer.ts";

const FIXTURES = new URL("../../fixtures/", import.meta.url);
const fixture = (path: string) => new Uint8Array(fs.readFileSync(new URL(path, FIXTURES)));

/** The fixture `path` as the host reads it, at https://data.test/<path>. */
function input(path: string): FileInput {
  const bytes = fixture(path);
  const dir = path.slice(0, path.lastIndexOf("/") + 1);
  return {
    url: `https://data.test/${path}`,
    size: bytes.length,
    read: async (o, n) => bytes.slice(o, o + n),
    open: async (key) => {
      const at = new URL(`${dir}${key}`, FIXTURES);
      if (!fs.existsSync(at)) return undefined;
      const b = new Uint8Array(fs.readFileSync(at));
      return { size: b.length, read: async (o, n) => b.slice(o, o + n) };
    },
  };
}

test("zarrifies a file the host reads, with or without its size pins", async () => {
  for (const [path, format, key] of [
    ["mp4/mp4_h264_aac.mp4", "mp4", "tracks/1/y/zarr.json"],
    ["tiff/edge_aperio_position.tif", "tiff", "zarr.json"],
    ["nifti/nifti_block_x_split.nii", "nifti", "zarr.json"],
    ["vsi/vsi_raw_gray_pyramid.vsi", "vsi", "zarr.json"],
  ] as const) {
    const pinned = await zarrifyToArchive(input(path));
    assert.equal(pinned.format, format, path);
    const archive = await Archive.open(pinned.archive, `https://data.test/${path}`);
    assert.ok(archive.lookup(key), `${path}: ${key}`);
    const root = JSON.parse(new TextDecoder().decode(await archive.read("zarr.json")));
    assert.equal(root.attributes.refkv_zarrified.profile, format, path);
    // every url source pins its size (literal data sources have none)
    const urls = archive.sources.filter((s) => s.url !== undefined);
    assert.ok(urls.length > 0 && urls.every((s) => s.size !== undefined), path);
    if (format !== "vsi") {
      // what zarrifySource gives for the same bytes, byte for byte
      const desc = await zarrifySource(`https://data.test/${path}`, bytesSource(fixture(path)));
      assert.deepEqual(pinned.archive, await writeVzip(desc), path);
    } else {
      // the tile files, opened through the host, are sources of their own
      assert.ok(archive.sources.some((s) => s.url?.endsWith(".ets")), path);
    }
    const bare = await zarrifyToArchive(input(path), { pins: false });
    const unpinned = await Archive.open(bare.archive, `https://data.test/${path}`);
    assert.ok(unpinned.sources.every((s) => s.size === undefined && s.etag === undefined), path);
    assert.deepEqual(unpinned.keys(), archive.keys(), path);
  }
});

test("rejects a file of no supported format", async () => {
  const bytes = new TextEncoder().encode("not an image, or anything else");
  await assert.rejects(
    zarrifyToArchive({ url: "https://data.test/notes.txt", size: bytes.length, read: async (o, n) => bytes.slice(o, o + n) }),
    (e: Error) => e instanceof ImageError && e.message === "not a file of any supported format",
  );
});

test("rejects a store URL on a private host by default", async () => {
  await assert.rejects(zarrifyToArchive("http://127.0.0.1:9/store/"), /private|special|refus/i);
});

test("decodes refkv.fits_tiles chunks as refkv.zarr's codec does", async () => {
  // the first chunk of a RICE_1 int16 image and of a dithered float32 one, and the sha256 of
  // the pixels refkv.zarr's codec decodes it to (refkv._core.fits_tiles_decode)
  for (const [path, want] of [
    ["fits/fits_comp_rice_int16.fits", "0713fe744f834c15"],
    ["fits/fits_comp_rice_float_dither1.fits", "f5f1150b7c3dea4f"],
  ]) {
    const bytes = fixture(path);
    const { archive: written } = await zarrifyToArchive(input(path));
    const archive = await Archive.open(written, `https://data.test/${path}`, async (_url, start, end) => ({
      data: bytes.slice(start, end),
      size: bytes.length,
    }));
    const meta = JSON.parse(new TextDecoder().decode(await archive.read("1/zarr.json")));
    const chunk = (await archive.read("1/c/0/0"))!;
    const pixels = await decodeFitsTiles(chunk, meta.codecs[0].configuration, meta.chunk_grid.configuration.chunk_shape);
    assert.equal(createHash("sha256").update(pixels).digest("hex").slice(0, 16), want, path);
  }
});

test("rejects a refkv.fits_tiles configuration it cannot read", async () => {
  await assert.rejects(decodeFitsTiles(new Uint8Array(8), { compression: "HCOMPRESS_1" }, [2, 2]), /^Error: refkv\.fits_tiles: /);
});
