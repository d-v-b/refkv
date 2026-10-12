"""The frozen Zarr v2 profile's output: `vzip.virtualize.store.StoreOutput`, its root declaring
the revision the profile is frozen at (25, the last before Zarr v2 stores moved onto the Rust
IR in revision 26), as `vzip_reference.revision` pins the TIFF, ND2 and CZI twins."""

from vzip.virtualize.common import declare
from vzip.virtualize.store import StoreOutput as _StoreOutput

REVISION = 25


class StoreOutput(_StoreOutput):
    def declare(self, profile: str, omes: dict[str, dict] | None = None) -> None:
        omes = omes or {}
        for key, doc in self.docs.items():
            path = key[: -len("zarr.json")].rstrip("/")
            target = {"ome": omes[path]} if path in omes else {}
            url = self.url if path == "" else None
            self.docs[key] = {**doc, "attributes": declare(target, profile, url, doc["attributes"], REVISION)}
