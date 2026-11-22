Changed 2026-11-04: the translation of a volume with IMOD's stamp follows IMOD's flag for the
origin's sign ([§4.2](../../mrc.md#42-scales-units-and-position)): a file with the stamp, bit 2 of IMOD's flags
clear and no standard `nversion` is placed at `-origin`. The layout took such a file's `origin` as
it stands, which put a subvolume on the wrong side of its parent's first pixel. The dataset
layout's coordinates change with it. `header` still holds `origin` as stored.
