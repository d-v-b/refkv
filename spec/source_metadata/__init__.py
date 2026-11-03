"""Each convention's source-metadata schema, one module per profile of the registry, named for it
(`-` as `_`), for spec/generate_schemas.py: `SCHEMA`, the schema of the profile's member of
`refkv_zarrified`; and optionally `DEFS` (more `$defs` the schema refers to), `ATTRIBUTES`
(the attributes a node may hold besides the convention's), `OPEN_ATTRIBUTES` (true when a node
holds the source's own attributes besides, as the HDF5 convention's do) and `URL` (the root's
`source.url`, when it is not the URL of a source file or, for a store profile, a store URL)."""

# An integer of the source past 2^53 - 1 in magnitude, as spec/conventions.md §6
# <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#6-source-metadata-as-json> writes it:
# the string of its decimal digits, with a leading - if negative. The patterns bound it by its
# digits only: at least 16 (2^53 has 16), and at most 20 for a 64-bit unsigned field (2^64 - 1 has
# 20) and 19 for a 64-bit signed one (2^63 has 19); a FITS card's integer has no bound.
BIG_INTEGER = {"type": "string", "pattern": "^-?[1-9][0-9]{15,}$"}
BIG_UNSIGNED = {"type": "string", "pattern": "^[1-9][0-9]{15,19}$"}
BIG_SIGNED = {"type": "string", "pattern": "^-?[1-9][0-9]{15,18}$"}
