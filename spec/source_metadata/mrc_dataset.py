"""The MRC dataset convention's source metadata (spec/zarrify/mrc-dataset.md), for
spec/generate_schemas.py."""

from source_metadata.mrc import SCHEMA as MRC

# The dataset layout of an MRC file (spec/zarrify/mrc-dataset.md §4
# <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mrc-dataset.md#4-source-metadata>): the
# MRC convention's root metadata, member for member, on the group `dataset`.
SCHEMA = {"anyOf": [{
    **MRC["anyOf"][0],
    "description": "The dataset group's: the MRC convention's root source metadata, member for member (spec/zarrify/mrc-dataset.md §4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mrc-dataset.md#4-source-metadata>)",
}]}
