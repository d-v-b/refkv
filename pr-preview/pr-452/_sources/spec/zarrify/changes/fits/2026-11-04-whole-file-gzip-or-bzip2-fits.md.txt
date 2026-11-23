Changed 2026-11-04: a whole file in gzip or bzip2 (`.fits.gz`, `.fits.bz2`) is accepted, where it
was rejected: each array is one chunk, the whole stream, with `refkv.skip` stopping at the array's
end ([§2.1](../../fits.md#21-a-compressed-file)); the root's source metadata has the member `stream`
([§5.1](../../fits.md#51-the-root)).
