import assert from "node:assert/strict";
import { test } from "node:test";
import { Archive, MERGE_GAP, mergedRuns, RefkvError } from "../src/archive.ts";
import { crc32c } from "../src/deflate.ts";
import { type ArchiveDesc, writeVzip } from "../src/writer.ts";

const BLOB = new Uint8Array(1 << 20).map((_, i) => i % 251);
const GAP = BigInt(MERGE_GAP);

/** An archive whose `fetchRange` serves `body` and records each request's range. */
async function open(desc: ArchiveDesc, body = BLOB) {
  const requests: [number, number][] = [];
  const archive = await Archive.open(
    await writeVzip(desc),
    "https://data.test/archive.vzip",
    async (_url, start, end) => {
      requests.push([start, end]);
      if (end > body.length) throw new RefkvError("resolution", "the object is shorter than the range");
      return { data: body.subarray(start, end), size: body.length };
    },
  );
  return { archive, requests };
}

const range = (offset: number, length: number) => ({ ranges: [{ source: 0, offset: BigInt(offset), length: BigInt(length) }] });
const spans = (reads: [number, number][], longest = 1 << 20) =>
  mergedRuns(reads.map(([a, b]) => ({ offset: BigInt(a), end: BigInt(b) })), BigInt(longest)).map((run) =>
    run.map((r) => [Number(r.offset), Number(r.end)]));

test("mergedRuns groups reads by the gap, the amplification cap and the run's length", () => {
  const g = Number(GAP);
  const cases: [[number, number][], number[][][]][] = [
    [[], []],
    [[[0, 10]], [[[0, 10]]]],
    [[[20, 30], [0, 10], [10, 20]], [[[0, 10], [10, 20], [20, 30]]]],
    [[[0, 1], [1 + g, 2 + g]], [[[0, 1], [1 + g, 2 + g]]]],
    [[[0, 1], [2 + g, 3 + g]], [[[0, 1]], [[2 + g, 3 + g]]]],
    [[[0, 10], [0, 10], [5, 20]], [[[0, 10], [0, 10], [5, 20]]]],
    [[[0, 4], [g, g + 4], [2 * g, 2 * g + 4]], [[[0, 4], [g, g + 4]], [[2 * g, 2 * g + 4]]]],
    [[[0, 600_000], [600_000, 1_200_000]], [[[0, 600_000]], [[600_000, 1_200_000]]]],
    [[[0, 2 << 20], [2 << 20, (2 << 20) + 1]], [[[0, 2 << 20]], [[2 << 20, (2 << 20) + 1]]]],
  ];
  for (const [reads, runs] of cases) assert.deepEqual(spans(reads), runs);
});

test("values read at once share requests, each with its own bytes", async () => {
  const { archive, requests } = await open({
    sources: [{ url: "https://data.test/blob.bin" }],
    entries: [
      { key: "a", ...range(0, 100) },
      { key: "b", ...range(100, 100) },
      { key: "same", ...range(0, 100) },
      { key: "gap", ...range(Number(GAP), 100) },
      { key: "crc", ranges: [{ source: 0, offset: 300n, length: 100n, crc32c: crc32c(BLOB.subarray(300, 400)) }] },
      { key: "far", ...range(900_000, 10) },
    ],
  });
  const got = await Promise.all(["a", "b", "same", "gap", "crc", "far"].map((k) => archive.read(k)));
  assert.deepEqual(got, [
    BLOB.subarray(0, 100), BLOB.subarray(100, 200), BLOB.subarray(0, 100),
    BLOB.subarray(Number(GAP), Number(GAP) + 100), BLOB.subarray(300, 400), BLOB.subarray(900_000, 900_010),
  ]);
  assert.deepEqual(requests.sort((x, y) => x[0] - y[0]), [[0, Number(GAP) + 100], [900_000, 900_010]]);
});

test("reads one at a time are not merged", async () => {
  const { archive, requests } = await open({
    sources: [{ url: "https://data.test/blob.bin" }],
    entries: [{ key: "a", ...range(0, 10) }, { key: "b", ...range(10, 10) }],
  });
  assert.deepEqual(await archive.read("a"), BLOB.subarray(0, 10));
  assert.deepEqual(await archive.read("b"), BLOB.subarray(10, 20));
  assert.deepEqual(requests, [[0, 10], [10, 20]]);
});

test("a read past the end fails only its own value", async () => {
  const { archive, requests } = await open(
    {
      sources: [{ url: "https://data.test/blob.bin" }],
      entries: [{ key: "good", ...range(900, 50) }, { key: "short", ...range(950, 100) }],
    },
    BLOB.subarray(0, 1000),
  );
  const [good, short] = await Promise.allSettled([archive.read("good"), archive.read("short")]);
  assert.deepEqual(good, { status: "fulfilled", value: BLOB.subarray(900, 950) });
  assert.equal(short.status, "rejected");
  assert.deepEqual(requests[0], [900, 1050]);
  assert.deepEqual(requests.slice(1).sort((x, y) => x[0] - y[0]), [[900, 950], [950, 1050]]);
});

test("a size pin that fails fails every value", async () => {
  const { archive } = await open({
    sources: [{ url: "https://data.test/blob.bin", size: 7n }],
    entries: [{ key: "a", ...range(0, 10) }, { key: "b", ...range(10, 10) }],
  });
  const got = await Promise.allSettled([archive.read("a"), archive.read("b")]);
  for (const r of got) {
    assert.equal(r.status, "rejected");
    assert.match(String((r as PromiseRejectedResult).reason), /pins/);
  }
});

test("a checksum that fails fails only its value", async () => {
  const { archive, requests } = await open({
    sources: [{ url: "https://data.test/blob.bin" }],
    entries: [
      { key: "good", ...range(0, 10) },
      { key: "bad", ranges: [{ source: 0, offset: 10n, length: 10n, crc32c: 0 }] },
    ],
  });
  const [good, bad] = await Promise.allSettled([archive.read("good"), archive.read("bad")]);
  assert.deepEqual(good, { status: "fulfilled", value: BLOB.subarray(0, 10) });
  assert.equal(bad.status, "rejected");
  assert.match(String((bad as PromiseRejectedResult).reason), /crc32c/);
  assert.deepEqual(requests, [[0, 20]]);
});

test("too many reads for one value is refused before any read", async () => {
  const bytes = await writeVzip({
    sources: [{ url: "https://data.test/blob.bin" }],
    entries: [{
      key: "many",
      ranges: [0, 200_000, 400_000].map((o) => ({ source: 0, offset: BigInt(o), length: 1n })),
    }],
  });
  let requests = 0;
  const archive = await Archive.open(bytes, "https://data.test/archive.vzip", async (_u, a, b) => {
    requests++;
    return { data: BLOB.subarray(a, b), size: BLOB.length };
  }, { maxReads: 2 });
  await assert.rejects(archive.read("many"), /3 reads/);
  assert.equal(requests, 0);
});
