# Releasing refkv

Push a tag `vX.Y.Z` (or a release candidate, `vX.Y.Z-rc.K`) on the commit to release:

```bash
git tag v0.2.0 && git push origin v0.2.0
```

That is all. The [release workflow](../.github/workflows/release.yml) builds the wheels
(abi3, for Python 3.12 and later, on Linux x86_64 and aarch64 with glibc and with musl,
macOS Intel and arm, and Windows x64 and arm64) and the sdist, checks that each installs and runs, checks that their names carry
the tag's version, and publishes them to PyPI through trusted publishing.

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
