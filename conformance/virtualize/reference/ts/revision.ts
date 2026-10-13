// The revision of spec/virtualize.md the frozen reference implements, which its roots
// record: 20, the last before the IR mirror became TIFF's, ND2's and CZI's
// `vzip_source` (revision 21). The shipped code records `REVISION` of
// src/virtualize/common.ts; compare.py reads a reference root that records 20 as
// recording the current revision (conformance/virtualize/HARNESS.md).

export const REVISION = 20;

// The revision the frozen NIfTI twin records: 25, the last before the IR mirror
// became NIfTI's `vzip_source` (revision 26).
export const REVISION_25 = 25;

// The revision the frozen DICOM twin records: 25, the last before the IR mirror
// became DICOM's `vzip_source` (revision 26).
export const DICOM_REVISION = 25;

// The revision the frozen IMS virtualizer records: 25, the last before the IR mirror
// became IMS's `vzip_source` (revision 26).
export const IMS_REVISION = 25;
// The revision the frozen SAFE, Zarr v2 and N5 twins implement (and the other store profiles that
// move onto the IR in revision 26): 25, the last before the IR mirror became their `vzip_source`.
export const STORES_REVISION = 25;
