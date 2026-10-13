"""Helpers shared by the IR test modules (a plain module, named apart from python/tests/helpers.py:
both directories are on sys.path)."""

import json
import sys
from pathlib import Path

FIXTURES = Path(__file__).parents[3] / "fixtures"
ROOT = Path(__file__).parents[3]
# the frozen reference twins the IR is compared against (vzip_reference)
sys.path.insert(0, str(ROOT / "conformance" / "virtualize" / "reference"))


# The members of a frozen root's source metadata the IR's mirror holds instead, for the
# profiles frozen at revision 25 (conformance/virtualize/compare.py's MIRRORED).
MIRRORED = {"nifti": ("header_rest", "extender", "extensions", "extensions_truncated", "unparsed", "trailing")}


def same_hierarchy(a, b) -> None:
    """The frozen reference's output `a` and the IR's `b` hold the same hierarchy but
    vzip_source: documents, references and data sources. The reference's root records
    the revision it implements (vzip_reference.revision: 20, or 25 for DICOM, NIfTI, IMS and NDPI),
    read as the current one."""
    from vzip.virtualize.common import REVISION
    from vzip_reference.revision import DICOM_REVISION, IMS_REVISION, REVISION_25
    from vzip_reference.revision import REVISION as FROZEN

    def docs(o):
        d = {k: json.loads(v) if k.endswith(".json") else v for k, v in o.bytes_entries.items()
             if not k.startswith("vzip_source")}
        prop = d.get("zarr.json", {}).get("attributes", {}).get("vzip_virtualized", {})
        moved = MIRRORED.get(prop.get("profile"))
        frozen = REVISION_25 if moved else {"dicom": DICOM_REVISION, "ims": IMS_REVISION, "ndpi": 25}.get(prop.get("profile"), FROZEN)
        if o is a and prop.get("revision") == frozen:
            prop["revision"] = REVISION
            for k in moved or ():
                prop[prop["profile"]].pop(k, None)
        return d

    def refs(o):
        return {k: v for k, v in o.refs.items() if not k.startswith("vzip_source")}

    assert docs(a) == docs(b)
    assert refs(a) == refs(b)
    assert list(a.data) == list(b.data)
