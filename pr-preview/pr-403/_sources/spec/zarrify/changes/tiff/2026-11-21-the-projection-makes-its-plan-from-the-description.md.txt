Changed 2026-11-21: the profile's parser returns its description of the file
([§7.6](../../tiff.md#76-the-description)) in place of facts that held the image's levels, planes,
axes, joins, pads, shards, scales and position; the projection makes those, and the checks of the
image ([§7.2](../../tiff.md#72-rejection)), from the description, as refkv's plan of the file. The
IR of an OME-TIFF no longer holds `ome/planes/<p>` aliases ([§5.1](../../tiff.md#51-elements)), so
its mirror changes; an input the image's checks reject is read, and rejected where the convention's
projection makes its plan.
