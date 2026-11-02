"""The source and store classes, one per profile of the registry (spec/zarrify.md §1.7
<https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#17-named-inputs-and-layouts>):
written by spec/tools/profiles.py from spec/zarrify/profiles/ (`just profiles`; `just sections`
checks that it is current). Edit the registry, not this file."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from refkv.zarrify.sources import Source, Store


@dataclass(frozen=True)
class CziSource(Source):
    """Zeiss CZI: a file the `czi` profile reads, its first bytes untested (`Source` lists its
    read options)."""

    profile: ClassVar[str] = "czi"


@dataclass(frozen=True)
class DicomSource(Source):
    """DICOM: a file the `dicom` profile reads, its first bytes untested (`Source` lists its
    read options)."""

    profile: ClassVar[str] = "dicom"


@dataclass(frozen=True)
class FitsSource(Source):
    """FITS: a file the `fits` profile reads, its first bytes untested (`Source` lists its read
    options)."""

    profile: ClassVar[str] = "fits"


@dataclass(frozen=True)
class FlacSource(Source):
    """FLAC (Free Lossless Audio Codec): a file the `flac` profile reads, its first bytes
    untested (`Source` lists its read options)."""

    profile: ClassVar[str] = "flac"


@dataclass(frozen=True)
class GribSource(Source):
    """WMO FM 92 GRIB edition 2 (WMO-No. 306, Manual on Codes, Volume I.2): a file the `grib`
    profile reads, its first bytes untested (`Source` lists its read options)."""

    profile: ClassVar[str] = "grib"


@dataclass(frozen=True)
class Hdf5Source(Source):
    """HDF5, NWB: a file the `hdf5` profile reads, its first bytes untested (`Source` lists its
    read options)."""

    profile: ClassVar[str] = "hdf5"


@dataclass(frozen=True)
class ImsSource(Source):
    """Imaris IMS: a file the `ims` profile reads, its first bytes untested (`Source` lists its
    read options)."""

    profile: ClassVar[str] = "ims"


@dataclass(frozen=True)
class LifSource(Source):
    """Leica LIF: a file the `lif` profile reads, its first bytes untested (`Source` lists its
    read options)."""

    profile: ClassVar[str] = "lif"


@dataclass(frozen=True)
class Mp3Source(Source):
    """MP3 (MPEG Layer III audio): a file the `mp3` profile reads, its first bytes untested
    (`Source` lists its read options)."""

    profile: ClassVar[str] = "mp3"


@dataclass(frozen=True)
class Mp4Source(Source):
    """MP4 and QuickTime (H.264 and HEVC video, AAC and MP3 audio): a file the `mp4` profile
    reads, its first bytes untested (`Source` lists its read options)."""

    profile: ClassVar[str] = "mp4"


@dataclass(frozen=True)
class MrcSource(Source):
    """MRC2014: a file the `mrc` profile reads, its first bytes untested (`Source` lists its
    read options)."""

    profile: ClassVar[str] = "mrc"


@dataclass(frozen=True)
class N5Store(Store):
    """N5: a store the `n5` profile reads, its root's keys untested (`Store` lists its read
    options)."""

    profile: ClassVar[str] = "n5"


@dataclass(frozen=True)
class Nd2Source(Source):
    """Nikon ND2: a file the `nd2` profile reads, its first bytes untested (`Source` lists its
    read options)."""

    profile: ClassVar[str] = "nd2"


@dataclass(frozen=True)
class NdpiSource(Source):
    """Hamamatsu NDPI: a file the `ndpi` profile reads, its first bytes untested (`Source` lists
    its read options)."""

    profile: ClassVar[str] = "ndpi"


@dataclass(frozen=True)
class NiftiSource(Source):
    """NIfTI-1 and NIfTI-2: a file the `nifti` profile reads, its first bytes untested (`Source`
    lists its read options)."""

    profile: ClassVar[str] = "nifti"


@dataclass(frozen=True)
class NpySource(Source):
    """NumPy NPY and NPZ (NEP 1): a file the `npy` profile reads, its first bytes untested
    (`Source` lists its read options)."""

    profile: ClassVar[str] = "npy"


@dataclass(frozen=True)
class NrrdSource(Source):
    """NRRD (Teem): a file the `nrrd` profile reads, its first bytes untested (`Source` lists
    its read options)."""

    profile: ClassVar[str] = "nrrd"


@dataclass(frozen=True)
class OmeZarrStore(Store):
    """OME-Zarr 0.4: a store the `ome-zarr` profile reads, its root's keys untested (`Store`
    lists its read options)."""

    profile: ClassVar[str] = "ome-zarr"


@dataclass(frozen=True)
class QptiffSource(Source):
    """Akoya QPTIFF: a file the `qptiff` profile reads, its first bytes untested (`Source` lists
    its read options)."""

    profile: ClassVar[str] = "qptiff"


@dataclass(frozen=True)
class SafeSource(Source):
    """Sentinel-2 SAFE (Level-1C, Level-2A): a file the `safe` profile reads, its first bytes
    untested (`Source` lists its read options)."""

    profile: ClassVar[str] = "safe"


@dataclass(frozen=True)
class SafeStore(Store):
    """Sentinel-2 SAFE (Level-1C, Level-2A): a store the `safe` profile reads, its root's keys
    untested (`Store` lists its read options)."""

    profile: ClassVar[str] = "safe"


@dataclass(frozen=True)
class SafetensorsSource(Source):
    """safetensors (Hugging Face): a file the `safetensors` profile reads, its first bytes
    untested (`Source` lists its read options)."""

    profile: ClassVar[str] = "safetensors"


@dataclass(frozen=True)
class SafetensorsStore(Store):
    """safetensors (Hugging Face): a store the `safetensors` profile reads, its root's keys
    untested (`Store` lists its read options)."""

    profile: ClassVar[str] = "safetensors"


@dataclass(frozen=True)
class TiffSource(Source):
    """TIFF, OME-TIFF, Aperio SVS: a file the `tiff` profile reads, its first bytes untested
    (`Source` lists its read options)."""

    profile: ClassVar[str] = "tiff"


@dataclass(frozen=True)
class VsiSource(Source):
    """Olympus VSI: a file the `vsi` profile reads, its first bytes untested (`Source` lists its
    read options)."""

    profile: ClassVar[str] = "vsi"


@dataclass(frozen=True)
class Zarr2Store(Store):
    """Zarr v2: a store the `zarr2` profile reads, its root's keys untested (`Store` lists its
    read options)."""

    profile: ClassVar[str] = "zarr2"


# each class, by its name
CLASSES: dict[str, type[Source | Store]] = {
    "CziSource": CziSource,
    "DicomSource": DicomSource,
    "FitsSource": FitsSource,
    "FlacSource": FlacSource,
    "GribSource": GribSource,
    "Hdf5Source": Hdf5Source,
    "ImsSource": ImsSource,
    "LifSource": LifSource,
    "Mp3Source": Mp3Source,
    "Mp4Source": Mp4Source,
    "MrcSource": MrcSource,
    "N5Store": N5Store,
    "Nd2Source": Nd2Source,
    "NdpiSource": NdpiSource,
    "NiftiSource": NiftiSource,
    "NpySource": NpySource,
    "NrrdSource": NrrdSource,
    "OmeZarrStore": OmeZarrStore,
    "QptiffSource": QptiffSource,
    "SafeSource": SafeSource,
    "SafeStore": SafeStore,
    "SafetensorsSource": SafetensorsSource,
    "SafetensorsStore": SafetensorsStore,
    "TiffSource": TiffSource,
    "VsiSource": VsiSource,
    "Zarr2Store": Zarr2Store,
}

__all__ = [
    "CziSource",
    "DicomSource",
    "FitsSource",
    "FlacSource",
    "GribSource",
    "Hdf5Source",
    "ImsSource",
    "LifSource",
    "Mp3Source",
    "Mp4Source",
    "MrcSource",
    "N5Store",
    "Nd2Source",
    "NdpiSource",
    "NiftiSource",
    "NpySource",
    "NrrdSource",
    "OmeZarrStore",
    "QptiffSource",
    "SafeSource",
    "SafeStore",
    "SafetensorsSource",
    "SafetensorsStore",
    "TiffSource",
    "VsiSource",
    "Zarr2Store",
]
