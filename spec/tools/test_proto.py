"""The protobuf schema files of spec/proto/: each one is, below its header comment, the schema block
of the section that defines it, and no message is in two of them. refkv.proto holds the messages
every store uses (spec/refkv.md, Appendix A
<https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#appendix-a-complete-schema>); vzip.proto
those of the ZIP binding alone (spec/bindings/vzip.md §12
<https://github.com/d-v-b/refkv/blob/main/spec/bindings/vzip.md#12-page-index-optional>)."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROTO = ROOT / "spec" / "proto"
# each schema file, the document that defines it, and the heading whose first proto block it is
SCHEMAS = [
    ("refkv.proto", "spec/refkv.md", "## Appendix A: Complete schema"),
    ("vzip.proto", "spec/bindings/vzip.md", "## 12. Page index (optional)"),
]


def schema_block(document: str, heading: str) -> str:
    """The first proto block after `heading` in `document`."""
    text = (ROOT / document).read_text(encoding="utf-8")
    section = text[text.index(f"\n{heading}\n"):]
    block = section[section.index("```proto\n") + len("```proto\n"):]
    return block[:block.index("```\n")]


def schema_file(name: str) -> str:
    """The schema file `name`, without its header comment."""
    lines = (PROTO / name).read_text(encoding="utf-8").splitlines(keepends=True)
    return "".join(lines[next(i for i, line in enumerate(lines) if not line.startswith("//")):])


@pytest.mark.parametrize(("name", "document", "heading"), SCHEMAS)
def test_each_schema_file_is_its_sections_block(name: str, document: str, heading: str):
    assert schema_file(name) == schema_block(document, heading)


def test_the_schema_files_are_every_file_and_share_no_message():
    assert sorted(p.name for p in PROTO.glob("*.proto")) == sorted(name for name, _, _ in SCHEMAS)
    messages = [set(re.findall(r"^message (\w+)", schema_file(name), re.MULTILINE)) for name, _, _ in SCHEMAS]
    assert all(messages)
    assert not set.intersection(*messages)
