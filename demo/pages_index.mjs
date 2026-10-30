// Writes <site>/index.html and <site>/README.md: a list of the demos in
// <site>'s subdirectories, from each one's index.html <title> and
// <meta name="description">. Directories that only redirect (a page with
// <meta http-equiv="refresh">) are left out.
// Usage: node demo/pages_index.mjs <site dir>

import fs from "node:fs";
import path from "node:path";

const site = process.argv[2];
const escape = (s) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const demos = fs
  .readdirSync(site, { withFileTypes: true })
  .filter((d) => d.isDirectory() && !d.name.startsWith("."))
  .map((d) => {
    const file = path.join(site, d.name, "index.html");
    if (!fs.existsSync(file)) return undefined;
    const html = fs.readFileSync(file, "utf8");
    if (/http-equiv="refresh"/.test(html)) return undefined;
    const title = html.match(/<title>([^<]*)<\/title>/)?.[1] ?? d.name;
    const description = html.match(/<meta name="description" content="([^"]*)"/)?.[1] ?? "";
    return { dir: d.name, title, description };
  })
  .filter(Boolean)
  .sort((a, b) => a.dir.localeCompare(b.dir));

fs.writeFileSync(
  path.join(site, "index.html"),
  `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>refkv demos</title>
<style>
  :root { --bg: #fafaf8; --fg: #1d1d1b; --muted: #6b6b66; --line: #deddd8; --accent: #2d5bd7;
    font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #141413; --fg: #ecebe6; --muted: #9b9a94; --line: #34332f; --accent: #8aa8ff; }
  }
  body { background: var(--bg); color: var(--fg); margin: 0; line-height: 1.5; }
  main { max-width: 760px; margin: 0 auto; padding: 32px 16px 64px; }
  h1 { font-size: 1.6rem; margin: 0 0 4px; }
  p { color: var(--muted); margin: 0 0 24px; }
  ul { list-style: none; padding: 0; margin: 0; }
  li { border-top: 1px solid var(--line); padding: 12px 0; }
  a { color: var(--accent); font-weight: 600; text-decoration: none; }
  span { display: block; color: var(--muted); font-size: 0.95rem; }
</style>
</head>
<body>
<main>
  <h1>refkv demos</h1>
  <p>Demos of <a href="https://github.com/d-v-b/refkv">refkv</a>, reference semantics for key-value storage.</p>
  <ul>
${demos.map((d) => `    <li><a href="${escape(d.dir)}/">${escape(d.title)}</a><span>${escape(d.description)}</span></li>`).join("\n")}
  </ul>
</main>
</body>
</html>
`,
);
fs.writeFileSync(
  path.join(site, "README.md"),
  `# refkv demos

Live demos of [refkv](https://github.com/d-v-b/refkv), reference semantics for key-value storage: **https://d-v-b.github.io/refkv-demo/**

| demo | what it does |
|---|---|
${demos.map((d) => `| [${d.title}](https://d-v-b.github.io/refkv-demo/${d.dir}/) | ${d.description} |`).join("\n")}

This repository holds only the built sites, one directory per demo, published
with GitHub Pages from the \`gh-pages\` branch. The source is in
[d-v-b/refkv](https://github.com/d-v-b/refkv) (the demos are in \`demo/\`, built from \`js/\`, and are
published by \`demo/pages.sh\`) and in the vzip-enabled Neuroglancer fork,
[d-v-b/neuroglancer](https://github.com/d-v-b/neuroglancer/tree/vzip).
`,
);
console.log(`index: ${demos.map((d) => d.dir).join(", ")}`);
