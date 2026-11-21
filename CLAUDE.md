# refkv

Run `just preflight` before every push. It fetches `origin/main` and runs what CI's `sections` job
runs: `just sections origin/main`, `just lint` and `just typecheck`. No hook runs it for you.

- When Python changes, also run `just test`.
- When Rust or Python changes, also run `just outputs-check`. If it reports an output you meant to
  change, run `just outputs-update` and commit the manifest (`conformance/outputs/manifests/<dir>.jsonl`)
  with the change.
- When JS changes, also run `just js::wasm`, then `just js::test`.
