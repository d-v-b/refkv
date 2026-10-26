"""Each convention's source-metadata schema, one module per profile of the registry, named for it
(`-` as `_`), for spec/generate_schemas.py: `SCHEMA`, the schema of the profile's member of
`refkv_zarrified`; and optionally `DEFS` (more `$defs` the schema refers to), `ATTRIBUTES`
(the attributes a node may hold besides the convention's) and `URL` (the root's `source.url`,
when it is not the URL of a source file or, for a store profile, a store URL)."""

# An integer of the source past 2^53 - 1 in magnitude, as spec/conventions.md §6
# <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#6-source-metadata-as-json> writes it:
# the string of its decimal digits, with a leading - if negative. The patterns bound it by its
# digits only: at least 16 (2^53 has 16), and, for a 64-bit unsigned field, at most 20 (2^64 - 1
# has 20); a FITS card's integer has no bound.
BIG_INTEGER = {"type": "string", "pattern": "^-?[1-9][0-9]{15,}$"}
BIG_UNSIGNED = {"type": "string", "pattern": "^[1-9][0-9]{15,19}$"}
