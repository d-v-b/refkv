# NDPI on the IR: measurements

The scripts behind [design/IR_NOTES.md](../../IR_NOTES.md#ndpi-on-the-ir-virtualize-revision-26).
Run from the repository root.

- `cmp_corpus.py <out> --only ndpi-`: compare.py on the NDPI corpus, the caching
  proxy's upstream fetches limited to 6 at a time.
- `ndpi_mutants.py <out>/ndpi <count> <seed>`: mutants aimed at NDPI's structure
  (IFDs, high words, McuStarts, strip headers); then `compare.py <out dir>
  --fixtures <out> --only ndpi`.
- `sparse.py <dir>`: sparse copies of the corpus slides from the proxy cache's
  blocks (`/tmp/vzip-proxy-cache`).
- `virt_bench.py <sparse dir> <out.json> local,h1-20ms,openslide`: virtualization,
  the frozen reference against the IR, over `design/experiments/ir_round2/rangeserver.py`.
- `prepare_ir.py <sparse dir>` and `bench_ir.py [--byte-cost S_PER_MB]`: the
  NDPI access benchmark with the IR's read planner. They need `model.py` and
  `bench.py` of the closed PR d-v-b/vzip#28 beside them:
  `git show origin/research/ndpi-access:experiments/ndpi_access/model.py > design/experiments/ndpi_ir/model.py`
  (and `bench.py`).
