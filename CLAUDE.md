# refkv

Run `just preflight` before every push. It fetches `origin/main` and runs what CI's `sections` job
runs: `just sections origin/main`, `just lint` and `just typecheck`. No hook runs it for you.

- When Python changes, also run `just test`.
- When JS changes, also run `just js::wasm`, then `just js::test`.
