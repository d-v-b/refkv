Changed 2026-11-04: samples of 1 and 12 bits, PackBits (Compression 32773), CCITT Group 4 (4) and EER
(65000 to 65002) have a layout ([§4.3](../../tiff.md#43-data-types-and-codecs)); BitsPerSample takes TIFF 6.0's
default of 1, so that a bilevel file without tag 258 has one too ([§2](../../tiff.md#2-the-source)); an EER file's
IFDs are planes along `t` ([§4.1](../../tiff.md#41-planes)); a level scan's candidate and a later page need not
have BitsPerSample; no output of a file accepted before changed.
