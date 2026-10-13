"""The frozen Zarr v2 profile's output: `refkv.virtualize.store.StoreOutput`, its root declaring
the revision the profile is frozen at (`refkv_reference.revision.STORES_REVISION`, 25, the last
before Zarr v2 stores moved onto the Rust IR in revision 26), as that module pins the other
twins."""

from refkv.virtualize.common import declare
from refkv.virtualize.store import StoreOutput as _StoreOutput
from refkv_reference.revision import STORES_REVISION


class StoreOutput(_StoreOutput):
    def declare(self, profile: str, omes: dict[str, dict] | None = None) -> None:
        omes = omes or {}
        for key, doc in self.docs.items():
            path = key[: -len("zarr.json")].rstrip("/")
            target = {"ome": omes[path]} if path in omes else {}
            url = self.url if path == "" else None
            self.docs[key] = {**doc, "attributes": declare(target, profile, url, doc["attributes"], STORES_REVISION)}
