Changed 2026-11-04: RLE Lossless, JPEG-LS and HTJ2K are admitted ([§2.1](../../dicom.md#21-transfer-syntax)), their
frames chunks of `imagecodecs_dicomrle`, `imagecodecs_jpegls` and `imagecodecs_htj2k`
([§4](../../dicom.md#4-output)), an RLE frame's header and a JPEG-LS frame's markers read and checked
([§7.5](../../dicom.md#75-frames)). `PALETTE COLOR` is admitted, the indices with the lookup tables as an array of
their own ([§4.1](../../dicom.md#41-the-palette)), as are native `YBR_FULL`, its channels `Y`, `Cb` and `Cr` as
stored ([§3](../../dicom.md#3-the-image)), and JPEG 2000 frames of `YBR_FULL_422`, which decoders convert to RGB
([§7.5](../../dicom.md#75-frames)).
