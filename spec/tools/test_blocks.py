"""The cross-format blocks the documentation site renders (blocks.py)."""

import blocks
import profiles
import pytest


def test_every_document_renders():
    """Every block of every document has a renderer, which renders it over the registry, and no
    document commits what the site renders in its place."""
    assert blocks.check() == []
    entries = profiles.load()
    for path, text in profiles.documents().items():
        for m in blocks.BLOCK.finditer(text):
            assert m.group(3) != blocks.RENDERERS[m.group(2)](entries, blocks.linker(path)), path


def test_blocks_render_from_where_they_are():
    """A block's links are made from the page it is rendered on, a page's own included text's are
    rebased, and links in code stay as they are."""
    entries = profiles.load()
    doc = "Before.\n\n<!-- profiles:{} -->\nOn GitHub.\n<!-- /profiles -->\n\nAfter."
    for block, page, want in (
        ("spec-table", "spec/zarrify.md", "| [TIFF](zarrify/tiff.md#part-2-the-profile) | file |"),
        ("spec-table", "docs/a.md", "| [TIFF](../spec/zarrify/tiff.md#part-2-the-profile) | file |"),
        ("conventions-table", "spec/conventions.md", "([§9](#9-dataset-layouts))"),
        ("conventions-table", "docs/a.md", "([§9](../spec/conventions.md#9-dataset-layouts))"),
        ("beside-table", "spec/zarrify.md", "| TIFF file | [tiff.md §7.4](zarrify/tiff.md#74-several-files) |"),
        ("docs-table", "docs/zarrify/README.md", "| [tiff](tiff/README.md) |"),
        ("stored-arrays", "spec/stored-array.md", "### TIFF: stored arrays"),
        ("stored-arrays", "spec/stored-array.md", "([stored-array.md](stored-array.md))"),
        ("stored-arrays", "spec/stored-array.md", "[the TIFF profile](zarrify/tiff.md)"),
        ("descriptions", "docs/build.md", "[tiff/description.json](../spec/zarrify/tiff/description.json)"),
    ):
        out = blocks.render(doc.format(block), page, entries)
        assert want in out, (block, page)
        assert out.startswith(f"Before.\n\n<!-- profiles:{block} -->\n")
        assert out.endswith("\n<!-- /profiles -->\n\nAfter.")
        assert "On GitHub." not in out
    text = "[a](x.md) `[b](y.md)` [c]: z.md\n\n[d]: w.md#h\n\n```\n[e](v.md)\n```\n"
    want = "[a](X.MD) `[b](y.md)` [c]: z.md\n\n[d]: W.MD#H\n\n```\n[e](v.md)\n```\n"
    assert blocks.map_links(text, str.upper) == want


def test_an_unknown_block_is_an_error():
    with pytest.raises(ValueError, match="no renderer of the block 'nope'"):
        blocks.render("<!-- profiles:nope -->\nx\n<!-- /profiles -->", "spec/a.md", profiles.load())


def test_a_missing_heading_is_an_error():
    entries = profiles.load()
    tiff = next(e for e in entries if e["name"] == "tiff")
    tiff["store_run"] = {**tiff["store_run"], "section": "99-nowhere"}
    with pytest.raises(ValueError, match="no heading with the anchor '99-nowhere'"):
        blocks.beside_table(entries, blocks.linker("spec/zarrify.md"))


def test_a_block_no_document_marks_is_an_error():
    texts = profiles.documents()
    texts["spec/zarrify.md"] = texts["spec/zarrify.md"].replace("<!-- profiles:spec-table -->\n", "")
    assert blocks.check(texts) == ["no document marks the block 'spec-table'"]
