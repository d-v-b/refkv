"""The documentation's examples: each one a file that runs, quoted verbatim by its page."""

import re
from pathlib import Path

DOCS = Path(__file__).resolve().parents[2] / "docs"
INCLUDE = re.compile(r"<!-- include: (\S+) -->\n```\w*\n(.*?)^```$", re.DOTALL | re.MULTILINE)


def test_pages_quote_their_examples_verbatim():
    """Every `<!-- include: path -->` marker is followed by a code block that is the
    file at `path` (relative to the page), byte for byte, and the file compiles."""
    found = {}
    for page in DOCS.rglob("*.md"):
        for path, block in INCLUDE.findall(page.read_text()):
            example = (page.parent / path).resolve()
            source = example.read_text()
            assert block == source, f"{page.name} quotes {path}, which has changed"
            compile(source, str(example), "exec")
            found[example.relative_to(DOCS).as_posix()] = page.name
    assert found.get("examples/oisst.py") == "virtual-datasets.md"
