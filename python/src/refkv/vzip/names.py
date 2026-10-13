"""The names the ZIP binding reserves (spec/archive.md §11.4
<https://github.com/d-v-b/refkv/blob/main/spec/archive.md#114-hidden-entries>): its
source table and page index entries, and the prefix of every hidden entry."""

SOURCES_KEY = "__vz__/sources"
INDEX_KEY = "__vz__/index"
RESERVED_PREFIX = "__vz__/"
