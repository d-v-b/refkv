// The fixtures whose layout or verdict their profile changed after the freeze, which the frozen
// reference does not give (conformance/zarrify/revised/README.md).

import fs from "node:fs";

const DIRECTORY = new URL("../../conformance/zarrify/revised/", import.meta.url);

export const REVISED = new Set<string>(fs.readdirSync(DIRECTORY).filter((f) => f.endsWith(".txt"))
  .flatMap((f) => fs.readFileSync(new URL(f, DIRECTORY), "utf8").split("\n"))
  .filter((line) => line && !line.startsWith("#")));
