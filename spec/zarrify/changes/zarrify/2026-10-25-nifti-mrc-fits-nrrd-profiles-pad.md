Changed 2026-10-25: the NIfTI, MRC, FITS and NRRD profiles pad an edge chunk's reference with a range
of the output's one **zero data source**, a data source a reference names a prefix of, in place
of a literal range per reference ([§1.2](../../../zarrify.md#12-input)); an output that still pads with literal
ranges is not equivalent to theirs ([§1.1](../../../zarrify.md#11-output-and-equivalence)).
