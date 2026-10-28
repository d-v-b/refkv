// Builds dist/: the service worker (vzip-sw.js, a classic script so it works
// in every browser with service workers), the embeddable zarrifier
// (refkv_embed.js, an ES module: src/embed.ts), the demo page, the map page for
// Sentinel-2 products (map.html, with OpenLayers, and the JPEG 2000 decoder
// and its worker), and the Rust core's wasm32 build (refkv_ir.wasm, which
// `#irwasm` fetches from beside the script; `just js::wasm` builds it).
//
// The JPEG 2000 and JPEG XR decoders (jpeg2000_decoder.wasm: hayro-jpeg2000,
// Apache-2.0 OR MIT; jpegxr.wasm: jxrlib, BSD-2-Clause) are the Neuroglancer
// fork's (https://github.com/d-v-b/neuroglancer, branch vzip,
// src/sliceview/jpeg2000/ and src/sliceview/jpegxr/), read from
// $NEUROGLANCER, by default a clone next to this repository (../neuroglancer),
// as serve.mjs and pages.sh read the fork. Without them the map page cannot
// decode bands, and the demo cannot sample contrast from such chunks.

import fs from "node:fs";
import path from "node:path";
import * as esbuild from "esbuild";

const here = path.dirname(new URL(import.meta.url).pathname);

const dist = path.join(here, "dist");
const wasm = process.env.REFKV_IR_WASM || path.join(here, "../rust/refkv-ir/target/web/refkv_ir.wasm");
if (!fs.existsSync(wasm)) {
  console.error(`${wasm} is missing: build it with \`just js::wasm\``);
  process.exit(1);
}
const phkvWasm = process.env.REFKV_PHKV_WASM || path.join(here, "../rust/refkv-phkv/target/web/refkv_phkv.wasm");
if (!fs.existsSync(phkvWasm)) {
  console.error(`${phkvWasm} is missing: build it with \`just js::phkv-wasm\``);
  process.exit(1);
}
const neuroglancer = process.env.NEUROGLANCER ?? path.join(here, "../../neuroglancer");
const decoders = [
  path.join(neuroglancer, "src/sliceview/jpeg2000/jpeg2000_decoder.wasm"),
  path.join(neuroglancer, "src/sliceview/jpegxr/jpegxr.wasm"),
];
fs.rmSync(dist, { recursive: true, force: true });
// The profile registry, spec/zarrify/profiles/<name>.json, inlined as `#profiles`
// (src/zarrify/profiles_bundled.ts)
const registryDir = path.join(here, "../spec/zarrify/profiles");
const registry = fs.readdirSync(registryDir).filter((f) => f.endsWith(".json"))
  .map((f) => JSON.parse(fs.readFileSync(path.join(registryDir, f), "utf8")))
  .sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
// ../demo is outside this package: its imports resolve to this package's node_modules
const common = {
  bundle: true, target: "es2022", sourcemap: true, logLevel: "info", nodePaths: [path.join(here, "node_modules")],
  define: { __REFKV_PROFILES__: JSON.stringify(registry) },
};
const build = (entry, outfile, options = {}) =>
  esbuild.build({ ...common, entryPoints: [path.join(here, entry)], outfile: path.join(dist, outfile), ...options });
await build("src/sw.ts", "vzip-sw.js", { format: "iife" });
await build("../demo/demo.ts", "demo.js", { format: "esm" });
// The zarrifier for a host that reads the input itself (src/embed.ts): the Neuroglancer fork
// vendors it, with refkv_ir.wasm, for its `refkv:` kvstore (src/third_party/refkv/)
await build("src/embed.ts", "refkv_embed.js", { format: "esm", sourcemap: false });
// zarrita imports its blosc, lz4 and zstd codecs (numcodecs, 1.4 MB with their
// wasm inline) dynamically: split them out, to be loaded only when an array
// uses them.
await esbuild.build({
  ...common, entryPoints: { map: path.join(here, "../demo/map/map.ts") }, outdir: dist,
  format: "esm", minify: true, splitting: true, chunkNames: "map-chunks/[name]-[hash]",
});
await build("../demo/map/jpeg2k_worker.ts", "jpeg2k_worker.js", { format: "iife", minify: true });
// The demo page, its profile blocks filled from the registry and each profile's
// demo/examples/<name>.html (so that adding a format edits no shared file): the formats
// sentence, the examples, and their credits
const prose = (items) => (items.length === 1 ? items[0] : `${items.slice(0, -1).join(", ")} and ${items.at(-1)}`);
const formats = registry.filter((e) => e.kind !== "layout");
const sources = (kind) => formats.filter((e) => e.kind === kind).map((e) => e.source);
const both = sources("both");
const examples = [];
const credits = [];
for (const e of registry) {
  const file = path.join(here, "../demo/examples", `${e.name}.html`);
  if (!fs.existsSync(file)) continue;
  const text = fs.readFileSync(file, "utf8");
  const credit = text.match(/^<!-- credit: ([\s\S]*?) -->$/m);
  if (credit) credits.push(`${e.title}: ${credit[1]}`);
  for (const line of text.split("\n")) {
    if (line.trim() && !line.startsWith("<!-- credit:")) examples.push(line.startsWith("    ") ? line : `    ${line}`);
  }
}
const blocks = {
  "demo-lede": `${prose(sources("file"))} files, ${prose(sources("store"))} stores${both.length ? `, and ${prose(both)} products` : ""} are supported;`,
  "demo-examples": examples.join("\n").trimStart(),
  "demo-credits": `${credits.join("; ")}.`,
};
let page = fs.readFileSync(path.join(here, "../demo/index.html"), "utf8");
for (const [name, text] of Object.entries(blocks)) {
  const marker = new RegExp(`<!-- profiles:${name} -->[\\s\\S]*?<!-- /profiles -->`);
  if (!marker.test(page)) throw new Error(`demo/index.html has no block ${name}`);
  page = page.replace(marker, () => text);
}
fs.writeFileSync(path.join(dist, "index.html"), page);
fs.copyFileSync(path.join(here, "../demo/map/map.html"), path.join(dist, "map.html"));
fs.copyFileSync(wasm, path.join(dist, "refkv_ir.wasm"));
fs.copyFileSync(phkvWasm, path.join(dist, "refkv_phkv.wasm"));
for (const decoder of decoders) {
  if (fs.existsSync(decoder)) {
    fs.copyFileSync(decoder, path.join(dist, path.basename(decoder)));
  } else {
    console.warn(`${decoder} is missing: the pages will not decode its codec (set NEUROGLANCER)`);
  }
}
