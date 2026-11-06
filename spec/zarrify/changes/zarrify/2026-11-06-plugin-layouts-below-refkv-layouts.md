Changed 2026-11-06: a zarrifier of a file input may be told the layouts plugins write, by name,
options and an optional path, each below the root's child `refkv_layouts` ([§1.7](../../../zarrify.md#17-named-inputs-and-layouts),
[spec/layout.md](../../../layout.md)); a plugin layout's references are not counted against the
budget ([§1.2](../../../zarrify.md#12-input)) and its entries are not walked to number sources
([§1.9](../../../zarrify.md#19-sources-beside-the-input)). An output written without one is
unchanged.
