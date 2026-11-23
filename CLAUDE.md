# refkv

Run every recipe in pixi's environment (`pixi run just <recipe>`, or inside `pixi shell`): it is
the toolchain CI uses (README.md, "Development"). Change a tool's version in `pixi.toml`, then
`pixi lock`, never in a workflow.

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
`just independence` lists each file a branch changes outside its formats' paths and the files
shared on purpose (`spec/tools/independence.py`); it warns rather than fails, so name each file it
lists in the PR with why, or move the change into the format's own files.

## Pull requests

- One PR per step. Its description names every output change `just outputs-check` reports, and
  each file `just independence` lists, with why. roborev reviews each commit.
- **A PR stacked on an unmerged PR targets that PR's branch**, not `main`
  (`gh pr create --base <predecessor-branch>`), and names the dependency. GitHub then shows only
  the PR's own diff, and once the predecessor merges and its branch is deleted (automatic here),
  moves the PR onto `main`.
- **Merge only PRs whose base is `main`, from the bottom of a stack up.** Merging a stacked PR
  merges it into its predecessor's branch, and the predecessor's squash commit then carries both
  under one title. Squash-merging the top of a stack first carries the lower PRs into `main` and
  leaves them open as conflicting duplicates.
- **A PR being revised is a draft.** Before changing an open PR, on your own findings or on a
  request, run `gh pr ready --undo <n>`; once the revision is pushed and its checks have run, run
  `gh pr ready <n>`. A PR that isn't a draft is ready to merge, and may be merged at any time.
- **Don't merge `main` into a branch just because `main` moved.** Every push starts CI on a shared
  runner queue, so merge `main` in only when the PR conflicts, or needs something new on `main` to
  build.
- `main` requires CI's `ci-ok` check, but not that a branch be up to date with `main`, so that
  merges don't wait behind one another. A PR runs CI's light tier, and each push to `main` runs the
  full suite. Two PRs that each pass can still break `main` together; fix that forward at once.
