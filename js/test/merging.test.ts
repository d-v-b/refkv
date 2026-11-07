import assert from "node:assert/strict";
import { test } from "node:test";
import { setTimeout as sleep } from "node:timers/promises";
import { Archive, coalescedRuns, MERGE_BUSY, MERGE_GAP, MERGE_MAX_WAIT, MERGE_WINDOW, mergedRuns, RefkvError } from "../src/archive.ts";
import { crc32c } from "../src/deflate.ts";
import { type ArchiveDesc, writeVzip } from "../src/writer.ts";

const BLOB = new Uint8Array(2 << 20).map((_, i) => i % 251);
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

/** Like `open`, but each request waits until `finish` ends it, the oldest first, failing if told to. */
async function openHeld(desc: ArchiveDesc) {
  const requests: [number, number][] = [];
  const held: { end: (fail?: boolean) => void }[] = [];
  const archive = await Archive.open(
    await writeVzip(desc),
    "https://data.test/archive.vzip",
    (_url, start, end) => {
      requests.push([start, end]);
      return new Promise((resolve, reject) => {
        held.push({
          end: (fail = false) => {
            if (fail) reject(new RefkvError("resolution", "the request failed"));
            else resolve({ data: BLOB.subarray(start, end), size: BLOB.length });
          },
        });
      });
    },
  );
  const settled = () => sleep(MERGE_WINDOW + 10);
  const finish = async (fail = false) => {
    held.shift()!.end(fail);
    await settled();
  };
  const finishAll = async () => {
    while (held.length > 0) await finish();
  };
  return { archive, requests, settled, finish, finishAll };
}

/** Entries that read far from each other, and from the first 200,000 bytes: requests of their own. */
const fillers = (n: number) =>
  Array.from({ length: n }, (_, i) => ({ key: `filler${i}`, ...range(200_000 + i * 2 * MERGE_GAP, 10) }));

const range = (offset: number, length: number) => ({ ranges: [{ source: 0, offset: BigInt(offset), length: BigInt(length) }] });
const spans = (reads: [number, number][], longest = 1 << 20, amplification = 4) =>
  mergedRuns(reads.map(([a, b]) => ({ offset: BigInt(a), end: BigInt(b) })), BigInt(longest), amplification).map((run) =>
    run.map((r) => [Number(r.offset), Number(r.end)]));

test("coalescedRuns closes the narrowest gaps within the limit while the runs fit the budget", () => {
  const g = Number(GAP);
  const runs = (reads: [number, number, number][], gap: number, budget: number) =>
    coalescedRuns(reads.map(([source, a, b]) => ({ source, offset: BigInt(a), end: BigInt(b) })), gap, budget).map(
      (r) => [r.source, Number(r.offset), Number(r.end)]);
  const cases: [[number, number, number][], number, number, number[][]][] = [
    [[[0, 0, 10], [0, 10, 20], [0, 5, 8]], g, 0, [[0, 0, 20]]],
    [[[0, 0, 1], [0, 100, 101]], g, 1 << 20, [[0, 0, 101]]],
    [[[0, 0, 1], [0, 100, 101]], g, 100, [[0, 0, 1], [0, 100, 101]]],
    [[[0, 0, 1], [0, 11, 12], [0, 17, 18]], g, 8, [[0, 0, 1], [0, 11, 18]]],
    [[[0, 0, 1], [0, 2 + g, 3 + g]], g, 2 ** 30, [[0, 0, 1], [0, 2 + g, 3 + g]]],
    [[[0, 0, 1], [1, 1, 2]], g, 2 ** 30, [[0, 0, 1], [1, 1, 2]]],
    [[[0, 0, 1], [0, 1, 2], [0, 3, 4]], 0, 2 ** 30, [[0, 0, 2], [0, 3, 4]]],
  ];
  for (const [reads, gap, budget, want] of cases) assert.deepEqual(runs(reads, gap, budget), want);
});

test("mergedRuns joins reads of one value only where they touch the run", () => {
  const x = {};
  const y = {};
  const runs = (reads: [number, number, object][]) =>
    mergedRuns(reads.map(([a, b, owner]) => ({ offset: BigInt(a), end: BigInt(b), owner })), 1n << 31n).map((run) =>
      run.map((r) => [Number(r.offset), Number(r.end)]));
  // interleaved pieces that touch: one run, each value's gaps filled by the other's pieces
  assert.deepEqual(runs([[0, 10, x], [10, 20, y], [20, 30, x], [30, 40, y]]).length, 1);
  // a read inside another value's run joins it
  assert.deepEqual(runs([[0, 1000, y], [500, 510, x], [600, 610, x]]).length, 1);
  // two reads of one value a gap apart stay apart, as its own coalescing left them
  assert.deepEqual(runs([[0, 10, x], [100, 110, x]]), [[[0, 10]], [[100, 110]]]);
  // a read within the gap of its value's last read, not touching the run, starts a run of its own
  assert.deepEqual(runs([[0, 10, x], [10, 20, y], [30, 40, x]]).length, 2);
  // one past the gap of its value's last read, which its value's coalescing could not combine, joins
  const far = Number(MERGE_GAP) + 1;
  assert.deepEqual(runs([[0, 10, x], [10, 10 + far, y], [20 + far, 30 + far, x]]).length, 1);
});

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
  // the policy's maxReadAmplification sets the cap: a larger one makes one run of those small reads
  assert.deepEqual(spans([[0, 4], [g, g + 4], [2 * g, 2 * g + 4]], 1 << 20, 10_000), [[[0, 4], [g, g + 4], [2 * g, 2 * g + 4]]]);
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

test("a read beside a request in flight waits, joins it or goes at once, by where it lies", async () => {
  const g = Number(GAP);
  const cases: { name: string; next: ArchiveDesc["entries"]; during: [number, number][]; after: [number, number][] }[] = [
    {
      name: "a read after the request, within the gap, waits and merges with the next",
      next: [{ key: "n0", ...range(200, 50) }, { key: "n1", ...range(300, 50) }],
      during: [],
      after: [[200, 350]],
    },
    { name: "a read before the request, within the gap, waits", next: [{ key: "n0", ...range(0, 50) }], during: [], after: [[0, 50]] },
    { name: "a read at the gap waits", next: [{ key: "n0", ...range(200 + g, 50) }], during: [], after: [[200 + g, 250 + g]] },
    { name: "a read past the gap goes at once", next: [{ key: "n0", ...range(201 + g, 50) }], during: [[201 + g, 251 + g]], after: [] },
    { name: "a read inside the request joins it", next: [{ key: "n0", ...range(120, 50) }], during: [], after: [] },
    { name: "a read of the same bytes joins it", next: [{ key: "n0", ...range(100, 100) }], during: [], after: [] },
    { name: "a read that overlaps the request waits", next: [{ key: "n0", ...range(150, 100) }], during: [], after: [[150, 250]] },
    {
      name: "a read of another source table entry goes at once",
      next: [{ key: "n0", ranges: [{ source: 1, offset: 120n, length: 50n }] }],
      during: [[120, 170]],
      after: [],
    },
  ];
  for (const { name, next, during, after } of cases) {
    const busy = fillers(MERGE_BUSY - 1);
    const { archive, requests, settled, finishAll } = await openHeld({
      sources: [{ url: "https://data.test/blob.bin" }, { url: "https://data.test/other.bin" }],
      entries: [{ key: "first", ...range(100, 100) }, ...busy, ...next],
    });
    const reading = ["first", ...busy.map((e) => e.key)].map((k) => archive.read(k));
    await settled();
    const started = requests.length;
    assert.equal(started, MERGE_BUSY, name);
    const later = next.map((e) => archive.read(e.key));
    await settled();
    assert.deepEqual(requests.slice(started), during, name);
    await finishAll();
    assert.deepEqual(requests.slice(started), [...during, ...after], name);
    assert.deepEqual((await Promise.all(reading))[0], BLOB.subarray(100, 200), name);
    const got = await Promise.all(later);
    next.forEach((e, i) => {
      const { offset, length } = (e as { ranges: { offset: bigint; length: bigint }[] }).ranges[0];
      assert.deepEqual(got[i], BLOB.subarray(Number(offset), Number(offset + length)), name);
    });
  }
});

test("a read near a request goes at once while its source is not busy", async () => {
  const busy = fillers(MERGE_BUSY - 2);
  const { archive, requests, settled, finishAll } = await openHeld({
    sources: [{ url: "https://data.test/blob.bin" }],
    entries: [{ key: "first", ...range(0, 10) }, { key: "beside", ...range(10, 10) }, ...busy],
  });
  const reading = ["first", ...busy.map((e) => e.key)].map((k) => archive.read(k));
  await settled();
  const beside = archive.read("beside");
  await settled();
  assert.deepEqual(requests.at(-1), [10, 20]);
  await finishAll();
  await Promise.all(reading);
  assert.deepEqual(await beside, BLOB.subarray(10, 20));
});

test("a read does not wait longer than MERGE_MAX_WAIT for a request near it", async () => {
  const busy = fillers(MERGE_BUSY - 1);
  const { archive, requests, settled, finishAll } = await openHeld({
    sources: [{ url: "https://data.test/blob.bin" }],
    entries: [{ key: "stalled", ...range(0, 10) }, { key: "beside", ...range(10, 10) }, ...busy],
  });
  const reading = ["stalled", ...busy.map((e) => e.key)].map((k) => archive.read(k));
  await settled();
  const beside = archive.read("beside");
  await sleep(MERGE_MAX_WAIT / 2);
  assert.equal(requests.length, MERGE_BUSY);
  await sleep(MERGE_MAX_WAIT / 2 + 100);
  assert.deepEqual(requests.at(-1), [10, 20]);
  await finishAll();
  await Promise.all(reading);
  assert.deepEqual(await beside, BLOB.subarray(10, 20));
});

test("a read that joined a request that fails is read on its own", async () => {
  const { archive, requests, settled, finish, finishAll } = await openHeld({
    sources: [{ url: "https://data.test/blob.bin" }],
    entries: [{ key: "whole", ...range(0, 100) }, { key: "inside", ...range(10, 10) }],
  });
  const whole = archive.read("whole");
  await settled();
  const inside = archive.read("inside");
  await settled();
  assert.deepEqual(requests, [[0, 100]]);
  await finish(true);
  assert.deepEqual(requests, [[0, 100], [0, 100], [10, 20]]);
  await finishAll();
  assert.deepEqual(await whole, BLOB.subarray(0, 100));
  assert.deepEqual(await inside, BLOB.subarray(10, 20));
});

test("a read that waited for a request that fails is still read, and only that request's read fails", async () => {
  const busy = fillers(MERGE_BUSY - 1);
  const { archive, requests, settled, finish, finishAll } = await openHeld({
    sources: [{ url: "https://data.test/blob.bin" }],
    entries: [{ key: "failing", ...range(0, 10) }, { key: "beside", ...range(10, 10) }, ...busy],
  });
  const failing = archive.read("failing");
  failing.catch(() => {});
  const others = busy.map((e) => archive.read(e.key));
  await settled();
  const beside = archive.read("beside");
  await settled();
  assert.equal(requests.length, MERGE_BUSY);
  await finish(true);
  assert.deepEqual(requests.at(-1), [10, 20]);
  await finishAll();
  await Promise.all(others);
  await assert.rejects(failing, /the request failed/);
  assert.deepEqual(await beside, BLOB.subarray(10, 20));
});

test("a read does not join a request that has failed", async () => {
  const { archive, requests, settled, finish, finishAll } = await openHeld({
    sources: [{ url: "https://data.test/blob.bin" }],
    entries: [
      { key: "a", ...range(0, 50) },
      { key: "b", ...range(50, 50) },
      { key: "inside", ...range(10, 80) },
    ],
  });
  const reads = [archive.read("a"), archive.read("b")];
  await settled();
  assert.deepEqual(requests, [[0, 100]]);
  await finish(true);
  // The request's reads are being made alone, none of which covers this one.
  reads.push(archive.read("inside"));
  await settled();
  assert.deepEqual(requests, [[0, 100], [0, 50], [50, 100], [10, 90]]);
  await finishAll();
  assert.deepEqual(await Promise.all(reads), [BLOB.subarray(0, 50), BLOB.subarray(50, 100), BLOB.subarray(10, 90)]);
});
