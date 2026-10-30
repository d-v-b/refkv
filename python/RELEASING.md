# Releasing refkv

Merge the release PR. [release-please](https://github.com/googleapis/release-please) keeps
one open on main (the [release-please workflow](../.github/workflows/release-please.yml)):
it is titled `chore(main): release X.Y.Z`, and it updates as commits land. Its version
follows the [conventional commits](https://www.conventionalcommits.org/) since the last
release: before 1.0, a breaking change (`feat!:`) or a feature (`feat:`) bumps the minor
version and a fix (`fix:`) the patch. It adds their subjects (the squash-merged PR titles) to
[CHANGELOG.md](../CHANGELOG.md), under the sections
[release-please-config.json](../release-please-config.json) lists, and records the version in
[.release-please-manifest.json](../.release-please-manifest.json). Edit the PR's
`CHANGELOG.md` before merging, if an entry needs rewording.

Merging it tags `vX.Y.Z` on the merge commit, creates the GitHub release with the changelog
entry, and runs the [release workflow](../.github/workflows/release.yml) at the tag. That
workflow builds the wheels (abi3, for Python 3.12 and later, on Linux x86_64 and aarch64 with
glibc and with musl, macOS Intel and arm, and Windows x64 and arm64) and the sdist, checks
that each installs and runs, checks that their names carry the tag's version, and publishes
them to PyPI through trusted publishing.

To release a version other than the one the commits give, put a `Release-As: X.Y.Z` line in
a commit message's footer on main; release-please then proposes that version.

### Release candidates

A release candidate is tagged by hand, on the commit to release, and is not in the changelog:

```bash
git tag v0.2.0-rc.1 && git push origin v0.2.0-rc.1
```

Pushing the tag runs the release workflow, which publishes the candidate to PyPI.

## Where the version comes from

refkv's version is git's, not a number in a file. [pyproject.toml](pyproject.toml) declares
`dynamic = ["version"]`, so maturin takes the version from the bindings' crate,
[rust/refkv-python/Cargo.toml](../rust/refkv-python/Cargo.toml), whose
committed version is a placeholder, `0.0.0`. Before each build, the release workflow runs
`tools/version.py write` ([tools/version.py](tools/version.py); `just python::version-write`
does the same), which writes the version `git describe --tags --match 'v*'` gives into
`Cargo.toml` and `Cargo.lock` (on a tag push, the pushed tag's, even when its commit also
carries another, such as a release candidate's):

| commit | semver (Cargo.toml) | PEP 440 (the wheel's name) |
|---|---|---|
| at tag `v1.2.3` | `1.2.3` | `1.2.3` |
| at tag `v1.2.3-rc.1` | `1.2.3-rc.1` | `1.2.3rc1` |
| 5 commits past `v1.2.3` | `1.2.4-dev.5` | `1.2.4.dev5` |
| 2 commits past `v1.2.3-rc.1` | `1.2.3-rc.2.dev.2` | `1.2.3rc2.dev2` |
| no `v*` tag, 7 commits | `0.0.0-dev.7` | `0.0.0.dev7` |

A tag that is not `vX.Y.Z` or `vX.Y.Z-rc.K`, or a version that is not semver (`v1.02.3`),
fails the build.

`version-write` changes two tracked files, so it runs only where the checkout is thrown
away (CI). Locally, `just python::build` (a wheel) and `just python::sdist` write the version
for the build and restore both files afterwards (`tools/version.py run`), leaving the tree as
it was; `just python::version` prints the version without writing it. An editable install
(`uv sync` at the root) carries the placeholder, so its `refkv.__version__` is `0.0.0`.
