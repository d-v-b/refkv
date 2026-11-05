# Fixtures a profile changed after the freeze

Each `<format>.txt` here names, by file or store name, the fixtures of `fixtures/<format>/` whose
layout or verdict their profile changed after the frozen reference
([`conformance/zarrify/reference`](../reference)) froze, which it does not give.

- `compare.py` compares their outputs, and those of their mutants (`mutate.py`), `py` with `web`
  alone.
- The tests that compare the frozen profiles with the Rust core
  (`python/tests/ir/ir_helpers.py`, `js/test/ir/ir.test.ts`) leave them out.

A file holds one format's lines only, so that PRs on different formats do not touch the same file:

- `#` comment lines at its top, saying why: what the profile changed, and what the frozen reference
  does instead, with a link to the section.
- One name per line, sorted by `LC_ALL=C sort`. `spec/tools/test_revised.py` checks the order, and
  that each name is in `fixtures/<format>/`.

Python reads the list with `conformance/zarrify/revised_fixtures.py`, and the Node tests with
`js/test/revised.ts`. A name is unique across the formats, so both give one set.
