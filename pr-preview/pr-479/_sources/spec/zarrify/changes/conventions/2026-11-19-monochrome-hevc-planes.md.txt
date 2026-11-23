Changed 2026-11-19: `refkv.stream` decodes HEVC pictures of 4:0:0 (monochrome) to their `y` plane
alone, and a `plane` of `"u"` or `"v"` of such a stream is an error, not a plane of the fill value
([§10](../../../conventions.md#10-coded-streams)). Janelia's H5J files hold such streams.
