"""The revision of spec/virtualize.md the frozen reference implements, which its roots record:
20, the last before the IR mirror became TIFF's, ND2's and CZI's `vzip_source`
(revision 21). The reference is frozen at it; the shipped code records
`vzip.virtualize.common.REVISION`, and compare.py reads a reference root that records
20 as recording the current revision (HARNESS.md)."""

REVISION = 20

# The revision the frozen DICOM profile records: 25, the last before the IR mirror became
# DICOM's `vzip_source` (revision 26).
DICOM_REVISION = 25

# The revision the frozen IMS profile records: 25, the last before the IR mirror became
# IMS's `vzip_source` (revision 26).
IMS_REVISION = 25
