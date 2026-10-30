#!/bin/bash
# Publishes the old demo site (https://d-v-b.github.io/vzip-demo/, the gh-pages
# branch of d-v-b/vzip-demo), which only forwards to the current one
# (https://d-v-b.github.io/refkv-demo/, published by demo/pages.sh):
#   index.html, 404.html     demo/moved.html, which sends every old URL to the
#                            same path on the new site, with its query and fragment
#   vzip-sw.js, tiff-to-zarr/vzip-sw.js, image-to-zarr/vzip-sw.js
#                            demo/retired-sw.js, which unregisters the workers
#                            the old site installed
#
# Usage: demo/pages_moved.sh [remote url]
#   (default: https://github.com/d-v-b/vzip-demo.git)

set -euo pipefail
cd "$(dirname "$0")/.."
remote_url="${1:-https://github.com/d-v-b/vzip-demo.git}"
commit="$(git rev-parse --short HEAD)"

site="$(mktemp -d)"
trap 'rm -rf "$site"' EXIT
git -C "$site" init --quiet --initial-branch gh-pages
cp demo/moved.html "$site/index.html"
cp demo/moved.html "$site/404.html"
for dir in . tiff-to-zarr image-to-zarr; do
  mkdir -p "$site/$dir"
  cp demo/retired-sw.js "$site/$dir/vzip-sw.js"
done
touch "$site/.nojekyll"
cat > "$site/README.md" <<'README'
# vzip-demo (moved)

The refkv demos moved to **https://d-v-b.github.io/refkv-demo/**
([d-v-b/refkv-demo](https://github.com/d-v-b/refkv-demo)). This site only
redirects old links there. Its source is `demo/pages_moved.sh` in
[d-v-b/refkv](https://github.com/d-v-b/refkv).
README

git -C "$site" add -A
git -C "$site" -c user.name="$(git config user.name)" -c user.email="$(git config user.email)" \
  commit --quiet -m "chore(pages): redirect to refkv-demo, from d-v-b/refkv@${commit}"
git -C "$site" push --quiet --force "$remote_url" gh-pages
echo "published the redirect site (from ${commit})"
