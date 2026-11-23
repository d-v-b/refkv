# refkv

Run `just preflight` before every push. It fetches `origin/main` and runs what CI's `sections` job
runs: `just sections origin/main`, `just lint` and `just typecheck`, then builds the documentation
site (`just docs`), failing on any warning. No hook runs it for you.

- When Python changes, also run `just test`.
- When Rust or Python changes, also run `just outputs-check`. If it reports an output you meant to
  change, run `just outputs-update` and commit the manifest (`conformance/outputs/manifests/<dir>.jsonl`)
  with the change.
- When JS changes, also run `just js::wasm`, then `just js::test`.

A format's PRs edit only that format's files. Never add a format to a list of formats, in prose or
in code: state a rule that names none, and let a test or the registry enumerate them. Preflight's
`just independence` lists each file of another format a branch changes, and each shared file to
which it adds a line naming a format (`spec/tools/independence.py`); it warns rather than fails, so
name each file it lists in the PR with why, or move the change into the format's own files.
