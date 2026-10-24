// The profile registry: generated from spec/virtualize/profiles.json by spec/tools/profiles.py; edit the registry, not this.

export type Profile = "tiff" | "ndpi" | "nd2" | "dicom" | "nifti" | "mrc" | "fits" | "mrc-dataset" | "ims" | "n5" | "zarr2" | "ome-zarr" | "safe" | "czi" | "vsi" | "qptiff" | "mp3" | "mp4";

export interface ProfileEntry {
  name: Profile;
  section: number | null;
  title: string;
  uuid: string;
  version: number;
  kind: "file" | "store" | "both" | "layout";
  suffixes: string[];
  reference: number | null;
  source: string;
  inputs: string;
  for: string;
}

export const REGISTRY: ProfileEntry[] = [
  {"name": "tiff", "section": 3, "title": "TIFF", "uuid": "48e9ac4e-1156-4a62-955e-20467d9c2700", "version": 0, "kind": "file", "suffixes": [".tif"], "reference": 20, "source": "TIFF, OME-TIFF, Aperio SVS", "inputs": "TIFF and BigTIFF, including OME-TIFF and JPEG-tiled slides such as Aperio SVS", "for": "OME-TIFF, plain tiled TIFF, and JPEG-tiled slides such as Aperio SVS"},
  {"name": "ndpi", "section": 4, "title": "NDPI", "uuid": "6cac71ef-dbb2-4acd-b60c-00389aa4238a", "version": 0, "kind": "file", "suffixes": [".ndpi"], "reference": 25, "source": "Hamamatsu NDPI", "inputs": "Hamamatsu NDPI slides, a TIFF variant with 64-bit offsets", "for": "Hamamatsu NDPI whole-slide images"},
  {"name": "nd2", "section": 5, "title": "ND2", "uuid": "59612f14-e314-4207-ba00-8f422ba71490", "version": 0, "kind": "file", "suffixes": [".nd2"], "reference": 20, "source": "Nikon ND2", "inputs": "Nikon ND2, format version 3 and later", "for": "Nikon NIS-Elements ND2 files"},
  {"name": "dicom", "section": 6, "title": "DICOM", "uuid": "acf17198-e5a5-48d3-8187-22ec4bb40ea5", "version": 0, "kind": "file", "suffixes": [".dcm"], "reference": 25, "source": "DICOM", "inputs": "DICOM Part 10 files with native or JPEG-encapsulated pixel data, including whole-slide images", "for": "DICOM files, including whole-slide images"},
  {"name": "nifti", "section": 7, "title": "NIfTI", "uuid": "06e5809d-4d54-4b72-afd0-6bf61a7b4c85", "version": 0, "kind": "file", "suffixes": [".nii"], "reference": 25, "source": "NIfTI-1 and NIfTI-2", "inputs": "NIfTI-1 and NIfTI-2 single files (`.nii`)", "for": "NIfTI-1 and NIfTI-2 volumes (`.nii`)"},
  {"name": "mrc", "section": 14, "title": "MRC", "uuid": "2aacd010-2ed1-4748-bc3c-5dd5e840bbce", "version": 0, "kind": "file", "suffixes": [".mrc"], "reference": null, "source": "MRC2014", "inputs": "MRC2014 files (CCP4 maps, image stacks, volumes and volume stacks)", "for": "MRC2014 maps, image stacks, volumes and volume stacks (`.mrc`, `.map`)"},
  {"name": "fits", "section": 18, "title": "FITS", "uuid": "4b0d3a7e-1c52-4f6a-9b8e-2d7f0c6a1e93", "version": 0, "kind": "file", "suffixes": [".fits", ".fz"], "reference": null, "source": "FITS", "inputs": "FITS files: images, multi-extension files, binary and ASCII tables", "for": "FITS images and tables (`.fits`, `.fz`)"},
  {"name": "mrc-dataset", "section": null, "title": "MRC dataset", "uuid": "f139bf4e-7bab-4c3c-9b2d-66ba747f9b93", "version": 0, "kind": "layout", "suffixes": [], "reference": null, "source": "MRC2014, as a dataset", "inputs": "", "for": ""},
  {"name": "ims", "section": 8, "title": "IMS", "uuid": "5067a535-8261-4b25-a93c-1985ed333bde", "version": 0, "kind": "file", "suffixes": [".ims"], "reference": 25, "source": "Imaris IMS", "inputs": "Imaris IMS files (HDF5)", "for": "Imaris `.ims` files"},
  {"name": "n5", "section": 9, "title": "N5", "uuid": "ad5d4c39-c69e-48f7-a3ef-4cc8c607d416", "version": 0, "kind": "store", "suffixes": [], "reference": 25, "source": "N5", "inputs": "N5 containers, default-mode blocks", "for": "N5 containers on S3"},
  {"name": "zarr2", "section": 10, "title": "Zarr v2", "uuid": "8e792619-d671-4687-ab51-752885dd3ee6", "version": 0, "kind": "store", "suffixes": [], "reference": 25, "source": "Zarr v2", "inputs": "Zarr v2 hierarchies", "for": "Zarr v2 hierarchies on S3"},
  {"name": "ome-zarr", "section": 11, "title": "OME-Zarr", "uuid": "b74ea302-65bb-49ae-b81f-f9bb52cd4eed", "version": 0, "kind": "store", "suffixes": [], "reference": 25, "source": "OME-Zarr 0.4", "inputs": "OME-Zarr 0.4 hierarchies on Zarr v2, migrated to OME-Zarr 0.5", "for": "OME-Zarr 0.4 hierarchies, migrated to OME-Zarr 0.5 in place"},
  {"name": "safe", "section": 12, "title": "Sentinel-2 SAFE", "uuid": "ef81346c-19e8-42ad-93b0-a279ccaf44c1", "version": 0, "kind": "both", "suffixes": [".zip"], "reference": 25, "source": "Sentinel-2 SAFE (Level-1C, Level-2A)", "inputs": "Sentinel-2 Level-1C and Level-2A products: a `.SAFE` directory (a store) or a `.SAFE.zip` file", "for": "Sentinel-2 Level-1C and Level-2A products, as a `.SAFE` directory or a `.SAFE.zip`"},
  {"name": "czi", "section": 13, "title": "CZI", "uuid": "7a0733c7-d4be-4482-a64f-6904d9354ea5", "version": 0, "kind": "file", "suffixes": [".czi"], "reference": 20, "source": "Zeiss CZI", "inputs": "Zeiss CZI files (ZISRAW, file version 1), with uncompressed, JPEG, JPEG XR and Zstd subblocks, mosaics and pyramids", "for": "Zeiss CZI files"},
  {"name": "vsi", "section": 15, "title": "VSI", "uuid": "c0a1d4e6-5b7f-4f3a-9c2d-8e1f6a7b3c45", "version": 0, "kind": "file", "suffixes": [".vsi"], "reference": null, "source": "Olympus VSI", "inputs": "Olympus cellSens VSI slides: a `.vsi` file and the tile files beside it, probed by key (uncompressed, JPEG and JPEG 2000 tiles)", "for": "Olympus cellSens whole-slide images (`.vsi` with its tile files)"},
  {"name": "qptiff", "section": 16, "title": "QPTIFF", "uuid": "329ea136-2f55-4c27-b6f4-8e60ca2109b8", "version": 0, "kind": "file", "suffixes": [".qptiff"], "reference": null, "source": "Akoya QPTIFF", "inputs": "Akoya (PerkinElmer) Vectra QPTIFF slides and inForm component data, a TIFF with an XML description per image, in LZW, JPEG or uncompressed tiles or strips", "for": "Akoya (PerkinElmer) Vectra multiplex scans and inForm component data"},
  {"name": "mp3", "section": 17, "title": "MP3", "uuid": "b6cb0047-9448-4fdc-b03e-10dd3d6aa829", "version": 0, "kind": "file", "suffixes": [".mp3"], "reference": null, "source": "MP3 (MPEG Layer III audio)", "inputs": "MPEG-1, MPEG-2 and MPEG-2.5 Layer III audio files (`.mp3`), with their ID3v2, ID3v1 and APE tags; one array of samples, not an image", "for": "MP3 audio files, as an array of samples (`.mp3`)"},
  {"name": "mp4", "section": 19, "title": "MP4", "uuid": "bbe3f6b9-488d-4d8d-85ce-60a1649dd27e", "version": 0, "kind": "file", "suffixes": [".mp4", ".mov"], "reference": null, "source": "MP4 and QuickTime (H.264 video, AAC and MP3 audio)", "inputs": "MP4 and QuickTime files (`.mp4`, `.m4a`, `.mov`) with H.264 video and AAC or MP3 audio tracks; a group per track, not an image", "for": "MP4 and QuickTime video and audio, as arrays of planes and samples (`.mp4`, `.mov`)"},
];
