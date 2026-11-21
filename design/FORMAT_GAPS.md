# Format coverage gaps

*Measured 2026-11-04, against refkv at `7617ce25` and the Neuroglancer fork's `vzip` branch at
`17bdc2de`. Nothing here is implemented: the ranking is for choosing what to do next.*

This note audits every profile for what it refuses, says how often that occurs in real data, and
names the smallest addition that would cover it. Six gaps stand out because each blocks a large public
archive and needs no new format work:

| gap | what it blocks | what it takes |
|---|---|---|
| [1. HDF5 Fletcher32](#tier-1-large-public-archives-small-changes) | all 153 chunked datasets of a Sentinel-5P product | trim 4 bytes from each reference |
| [2. TIFF Predictor 3](#tier-1-large-public-archives-small-changes) | Copernicus DEM and USGS 3DEP | an existing imagecodecs codec |
| [3. Gzipped single files](#tier-1-large-public-archives-small-changes) | 90% of the NIfTI files sampled from OpenNeuro, and the EMDB maps probed | `gzip` and `refkv.skip`, both already in use |
| [4. GRIB grids](#tier-1-large-public-archives-small-changes) | GFS native, GDAS, CFS, HRRR Alaska, NBM Alaska and Hawaii | grid templates, with no codec change |
| [5. Short compressed strips](#tier-1-large-public-archives-small-changes) | what tifffile writes by default, for many heights | the `rectilinear` grid, already in use |
| [6. HDF5 user blocks](#tier-1-large-public-archives-small-changes) | JPSS VIIRS products and MATLAB v7.3 files | look for the signature at 512, 1024, ... |

## How to read this

**Evidence.** Each entry says which kind it has:

- **run**: a real public file went through `refkv zarrify` at this commit, and the output is quoted;
- **tags**: the file's own headers were read, and the verdict follows from the spec, without a run;
- **documented**: a vendor or project document says so, with no measure of how much data is affected;
- **not measured**: an estimate from general knowledge, so treat its rank as soft.

The samples are small and were chosen by availability, not designed as surveys. They show that a variant
occurs in a major archive, not what share of all data it is.

**Decode tests.** "Tested" means a chunk went through the named codec chain in zarr-python 3.4 with
imagecodecs-zarr 2026.8.16 and came back equal. "Real tile" means the chunk came from the public file
and was compared with tifffile's or h5py's decoding. Otherwise the chunk was synthetic.

**Cost.** These are rough, and each includes the spec, fixtures and conformance cases.

| cost | meaning |
|---|---|
| **S** | a profile change, with no new decoder in any reader: a day or two |
| **M** | a parser extension, or a new decoder in the fork: up to a week |
| **L** | a new parser or convention, or a decoder ported to Rust: weeks |

**Readers.** The reader columns are about decoding, and these baselines apply:

- **zarr-python** finds imagecodecs-zarr's and refkv's codecs by name. `refkv.zarr` reads references.
- **The fork** decodes only what `src/datasource/zarr/codec/` registers: `bytes`, `transpose`, `gzip`,
  `numcodecs.zlib`, `numcodecs.shuffle`, `zstd`, `blosc`, `crc32c`, `sharding_indexed`, `n5_default`,
  `imagecodecs_jpeg`, `imagecodecs_jpeg2k`, `imagecodecs_jpegxr`, `imagecodecs_ljpeg`,
  `imagecodecs_delta`, and refkv's `tiff_lzw`, `fits_tiles`, `grib`, `precomputed`, `skip` and `stream`.
  It reads the `rectilinear` grid, but refuses `sharding_indexed` on it.
- **TensorStore** 0.1.86 opened `bytes`, `gzip`, `zstd` and `crc32c`, and refused `numcodecs.zlib`,
  `numcodecs.shuffle`, `imagecodecs_jpeg2k`, `imagecodecs_delta`, `refkv.skip`, `packbits` and the
  `rectilinear` grid. Its documentation also lists `blosc`, `transpose` and `sharding_indexed`. It has
  no way to read refkv references, so "yes" means it could decode a copy of the chunks.
- **Upstream Neuroglancer** reads no references. Everything below that says "fork" means the fork.

## The ranking

Rank weighs how much real data a gap blocks, how strong the evidence is, and the cost. A gap with
weak evidence ranks lower even if it is cheap.

### Tier 1: large public archives, small changes

| # | format, variant | evidence | smallest addition | cost | zarr-python / fork / TensorStore |
|---|---|---|---|---|---|
| 1 | **HDF5, Fletcher32** (filter 3), usually after shuffle and deflate. Refused with `filter` ([hdf5 §3.1](../spec/zarrify/hdf5.md#31-data-types)) | **Run:** a Sentinel-5P L2 NO2 product (`meeo-s5p` bucket) keeps 153 datasets out of its hierarchy, all with the pipeline shuffle, deflate, Fletcher32; only the 2 without filters are presented | **Profile change:** end each reference 4 bytes early, which drops the checksum, and keep the codecs as they are. The checksum stays in the source | **S** | yes, real chunk tested / yes, no change / no: needs `numcodecs.*` |
| 2 | **TIFF, Predictor 3** with Deflate, LZW or zstd ([tiff §4.3](../spec/zarrify/tiff.md#43-data-types-and-codecs)). QPTIFF inherits the refusal | **Run:** `rejected: unsupported predictor 3` on Copernicus DEM GLO-30 and USGS 3DEP 1/3 arc-second. **Tags:** GLO-90 too. **Documented:** GDAL's COG driver selects it for float data under `PREDICTOR=YES` | **Existing codec:** `imagecodecs_floatpred`, then the compressor. It is array-to-bytes, so it replaces `bytes` | **S**, plus M for the fork | yes, real tiles tested for Deflate and LZW; zstd synthetic / needs a short decoder / no |
| 3 | **Gzipped single-array files**: `.nii.gz`, `.map.gz`, `.mrc.gz`, and NRRD's gzip with a byte skip. Refused at the signature, or as `reject_gzip_byte_skip` | **Run:** `not a file of any supported format` on an OpenNeuro T1w and on EMDB's `emd_1234.map.gz`. **Sampled:** 60 OpenNeuro datasets held 9,773 `.nii.gz` and 1,056 `.nii` files; all 3 EMDB entries that answered held only `.map.gz` | **Profile change:** one chunk per array, the whole stream, with `bytes`, `refkv.skip` (the header's length), `gzip`. This is what NRRD's gzip and `.npz` members already do. Inflate only the header when zarrifying | **S–M** | yes, synthetic tested / yes, no change / no: needs `refkv.skip` |
| 4 | **GRIB, grid templates** 3.40 (Gaussian), 3.20 (polar stereographic), 3.10 (Mercator), refused with `grid` ([grib §2.6](../spec/zarrify/grib.md#26-what-is-not-presented)) | **Run:** `none of the 113 GRIB fields can be presented: grid` on GFS `sfluxgrb`, and 169 fields on HRRR Alaska. **Tags:** GDAS and CFS use 3.40, NBM Alaska 3.20, NBM Hawaii 3.10 | **Profile change:** more templates in the projection, with Gauss–Legendre latitudes for 3.40. `refkv.grib` decodes by packing template, so the codec does not change | **S–M** | yes / yes / no: needs `refkv.grib` |
| 5 | **TIFF, compressed strips with a short last strip** (Deflate, zstd, JPEG, JPEG 2000): `cannot be padded` ([tiff §4.5](../spec/zarrify/tiff.md#45-strips)) | **Run:** a 1000 × 1000 file from tifffile's defaults, with zlib or zstd, is rejected. **Measured:** tifffile uses 131 rows per strip there and 51 at 2160 × 2560, and Pillow 65 at 1000 × 1000; all leave a short strip. No real-world file was run | **Chunk grid:** `rectilinear`, one short last chunk, as CZI and MP4 already do | **S** | yes, with the flag `refkv.zarr` sets / yes / no: `rectilinear` refused |
| 6 | **HDF5 with a user block.** The profile tests for the signature at offset 0 only | **Run:** `not a file of any supported format` on a JPSS VIIRS SDR (signature at 1024) and on h5py files with user blocks of 512 and 1024. **Documented:** MATLAB v7.3 files have a 512-byte user block | **Profile change:** test 512, 1024, 2048, ... as the HDF5 format defines, with addresses relative to that base. The signature test must not take another format's file for one | **S** | as for any HDF5 file, by its filters |

Three things to watch in this tier:

- **Gap 3 costs reads.** A chunk is the whole stream, so a reader inflates all of it for any slice, in
  memory. That suits an anatomical volume and is poor for a multi-GB fMRI run. Random access would
  need a gzip index (checkpoints with 32 KiB windows), which means reading every byte at zarrify time:
  a separate, optional step, not part of this gap. The fork's `gzip` on multi-member streams (`bgzip`) is untested.
- **Gap 5 cannot use shards.** `shard.rs` and the fork both refuse `sharding_indexed` on a
  `rectilinear` grid, so such a level keeps a chunk per strip. This costs little: [#217](https://github.com/d-v-b/refkv/pull/217)
  matters most for one-row strips, and a one-row strip is never short.
- **Gap 2 is tested for single-sample images only.** An interleaved predictor needs `dist` set to the
  sample count, and that case has no test yet.

### Tier 2: real data, more work or less evidence

| # | format, variant | evidence | smallest addition | cost | zarr-python / fork / TensorStore |
|---|---|---|---|---|---|
| 7 | **DICOM, multi-file series and pyramids.** A profile reads one file ([dicom §7.7](../spec/zarrify/dicom.md#77-limits)) | **Sampled, IDC:** median files per series were CT 124 (n=49), MR 106 (n=17) and WSI 5 (n=24), against SEG 1 (n=30). **Run:** one CT file gives a `[512, 512]` image | **Store form:** list a prefix, group by series, sort by position, stack; pyramids become multiscales. `refkv.compose` already does the joining | **M–L** | unchanged: as the frames' codecs |
| 8 | **DICOM, Bits Allocated 1**: `unsupported Bits Allocated` ([dicom §3](../spec/zarrify/dicom.md#3-the-image)) | **Run:** rejected on an IDC SEG. **Sampled:** 64 of 387 IDC files had Bits Allocated 1, and 74 were SEG | **Existing codec:** `packbits` on `bool`, least significant bit first, as DICOM packs them; cut chunks where frames start on a byte, else one chunk (safetensors' rule) | **S–M** | `refkv.zarr`'s `packbits` refuses `bool` today: extend / needs a decoder / no |
| 9 | **Zarr v2, data types and filters.** One `\|S15` coordinate rejects the store ([zarr2 §3](../spec/zarrify/zarr2.md#3-arrays)) | **Tags:** both NOAA NWM retrospective stores checked have `\|S1` and `\|S15` arrays. **Documented:** AnnData must store strings with `VLenUTF8`, so an object dtype. Clean: MUR SST, HRRR Zarr and 3 CMIP6 stores | **Profile change:** leave out what cannot be presented instead of rejecting; reuse [NPY's data type table](../spec/zarrify/npy.md#3-data-types); add `vlen-utf8`, and compressors or filters through `numcodecs.lz4`, `.bz2`, `.lzma`, `.fixedscaleoffset`, `.quantize` | **M** | yes, by name, not run / strings need checking, `numcodecs.*` others need decoders / not tested |
| 10 | **TIFF, LERC (34887) and WebP (50001)**: `unsupported compression` | **Tags:** all 3 LINZ `nz-elevation` files sampled are LERC, and all 3 `nz-imagery` files WebP. **Documented:** both are GDAL COG options | **Existing codecs:** `imagecodecs_lerc`; `transpose`, `imagecodecs_webp` with `hasalpha` set from the sample count (RGBA tiles otherwise decode to 3 channels) | **S**, plus M for each in the fork | yes, real tiles tested / needs decoders / no |
| 11 | **HDF5, third-party filters**: Blosc (32001), zstd (32015), LZF (32000) | **Documented:** the Janelia `shroff-lab` files of [NATIVE_SHARD_INDEX.md](NATIVE_SHARD_INDEX.md) use 32001. Otherwise **not measured**: 14 sampled DANDI assets had only deflate and shuffle | **Existing codecs:** `blosc`, `zstd`, and `imagecodecs_lzf` with `header: false` | **S**, plus M for LZF in the fork | LZF tested, others not / Blosc and zstd yes, LZF needs a decoder / Blosc and zstd yes |
| 12 | **netCDF-4 dimension names.** Accepted, but with `Dimension names: none` | **Run:** OISST's `sst` has no `dimension_names`, only `_Netcdf4Dimid`. 6 of the 7 Earth-science samples were netCDF-4, by their `.nc` names and HDF5 signatures | **Layout:** name dimensions from `DIMENSION_LIST`, make scales coordinates; [design/plans/nwb.md](plans/nwb.md) already plans this as "later". VirtualiZarr is today's route | **M** | unchanged |
| 13 | **Multi-file microscopy**: OME-TIFF across files, CZI `name(1).czi`, both rejected | **Documented:** Micro-Manager splits at 4 GB by default; Bio-Formats and BigStitcher handle multi-file CZI. **Not measured** otherwise | **Profile change:** sources beside the input, read by name, as VSI and detached NRRD already do | **M** | unchanged |
| 14 | **NIfTI complex** (32, 1792) and **Zarr v2 complex**: refused, though MRC, CZI and NPY present `complex64` | **Not measured:** common in MRI phase and raw data | **Profile change:** table rows | **S** | yes / not checked / not tested |
| 15 | **NIfTI `.hdr`/`.img` pairs**: refused at the signature | **Not measured:** older SPM and FSL data | **Profile change:** a data file beside the header, as detached NRRD does | **S–M** | unchanged |
| 16 | **DICOM RLE Lossless** (`.1.2.5`) | **Sampled:** 0 of 387 in IDC, which may normalize its data. **Documented:** AWS HealthImaging accepts it. Clinical archives: **not measured** | **Existing codec:** `imagecodecs_dicomrle`, `dtype` from Bits Allocated; one stream per frame | **S**, plus M for the fork | yes, 16-bit synthetic tested / needs a short decoder / no |
| 17 | **DICOM photometric**: JPEG 2000 with `YBR_FULL_422`; native `PALETTE COLOR` and `YBR_*` | **Run:** rejected on 2 IDC WSI files and 1 SC. **Sampled:** 2 and 1 of 387 | **Profile change:** a table row for the first, after checking what the decoders return; indices with the LUT as its own array for palette; Y, Cb, Cr channels for native `YBR_FULL` | **S–M** | unchanged |
| 18 | **IMS, shuffle and LZ4** ([ims §7.7](../spec/zarrify/ims.md#77-rejection)) | **Documented:** ImarisWriter offers shuffle with gzip, LZ4 and shuffle with LZ4, with gzip level 2 as its default; a Zenodo record holds LZ4 demo images. Share **not measured** | **Existing codec:** `numcodecs.shuffle`, as the HDF5 profile uses it; `imagecodecs_lz4h5` for LZ4 | **S** for shuffle, **M** for LZ4 | yes, not run / shuffle yes, LZ4 needs a decoder / no |

### Tier 3: specialized, or a lot of work

| # | format, variant | evidence | smallest addition | cost | zarr-python / fork / TensorStore |
|---|---|---|---|---|---|
| 19 | **HDF5, bitshuffle (32008) and LZ4 (32004)**, as Dectris Eiger detectors write | **Documented:** one source, PSI's Jungfraujoch code and commits; check against Dectris's documents | `imagecodecs_lz4h5`, and a new `refkv.h5_bitshuffle` (`imagecodecs_bitshuffle` handles neither the block framing nor LZ4) | **M** | LZ4 yes, not run; bitshuffle new / new decoders / no |
| 20 | **HDF5, external links, external storage, virtual datasets** (`external_storage`, `layout`) | **Documented:** Eiger master files link to their data files. Refused for keeping data outside the file, which references can reach | Follow to sources beside the file; virtual datasets need a selection-to-chunk mapping | **M**, **L** for virtual | unchanged |
| 21 | **TIFF, EER** (65000–65002), Falcon 4 cryo-EM | **Documented:** Falcon 4's native format, with tifffile naming the codes `EER_V0` to `EER_V2`. No file run | `imagecodecs_eer`, its parameters from the compression code | **M** | not run / needs a decoder / no |
| 22 | **MRC, mode 101** (4-bit), and mode 3 | **Documented:** SerialEM offers it for K2 and K3 frames, warning that not all software reads it | `packbits` on `uint8`, `last_bit: 3`, low nibble first; mode 3 needs a two-`int16` struct | **S–M** | `refkv.zarr`'s `packbits` refuses `last_bit` today: extend / needs a decoder / no |
| 23 | **TIFF, 1 and 12 bits, PackBits, CCITT**. A bilevel file has no tag 258, and fails with `IFD at 8 has no tag 258` | **Run:** on tifffile output. Real-world share **not measured** | `imagecodecs_packints` (MSB first, `runlen`), `imagecodecs_packbits`, `imagecodecs_ccittfax4` | **S–M** each | packints and PackBits tested / need decoders / no |
| 24 | **DICOM, HTJ2K** (`.4.201`–`.203`) and **JPEG-LS** (`.4.80`, `.81`) | **Sampled:** 0 of 387 in IDC. **Documented:** HTJ2K is AWS HealthImaging's default storage format | `imagecodecs_htj2k`, `imagecodecs_jpegls` | **S**, plus M–L for the fork | yes, synthetic tested / hayro-jpeg2000 returns `Marker(Unsupported)` on an HTJ2K stream: needs OpenJPH or similar / no |
| 25 | **GRIB, reduced grids** (octet 11), other templates (3.1, 3.101), and **edition 1** | **Documented:** ERA5's native fields are on a reduced Gaussian grid, partly GRIB1; COSMO uses 3.1. **Tags:** DWD's global ICON is `.grib2.bz2`, so whole-file compression comes first there | Reduced: a 1-D array of points. Edition 1: a parser and a `refkv.grib` decoder | **M** reduced, **L** edition 1 | yes, by extension / yes, it vendors the Rust core / no |
| 26 | **LIF family**: LOF, XLEF, LIFEXT ([lif §7.3](../spec/zarrify/lif.md#73-rejection)) | **Documented:** LAS X writes them; Bio-Formats added LOF and XLEF in 6.9.0, with Leica's help; `liffile` reads them | Extend the parser; XLEF links many files, so it also needs gap 13's sources | **M–L** | unchanged |
| 27 | **FITS tables as arrays.** Columns are described, not presented ([fits §8](../spec/zarrify/fits.md#8-what-the-layout-does-not-express)) | **Not measured:** catalogs are a large part of FITS | A `struct` array over the rows, as the HDF5 profile does for compounds; variable-length columns stay out | **M** | `struct` through `refkv.zarr` / not checked / not tested |
| 28 | **FITS, HCOMPRESS_1, PLIO_1, null masks, `UNCOMPRESSED_DATA`** | **Documented:** fpack offers both; a compression study finds PLIO best for masks. Which surveys use them: **not measured** | Extend `refkv.fits_tiles`. These must be in Rust because the core decodes it, though `imagecodecs_hcomp` and `_plio` exist for comparison | **M–L** | yes, by extension / yes, it vendors the core / no |
| 29 | **`.fits.gz`, `.fits.bz2`** | **Documented:** SDSS frames are `.fits.bz2` | Like gap 3, for one-image files. Others need `refkv.skip` to also stop before the next HDU. bz2 is `numcodecs.bz2` | **M** | yes / gzip yes, bz2 no / no |
| 30 | **MP4, fragmented files; AV1, VP9, MJPEG, PCM, HE-AAC; 10-bit H.264; 4:2:2 and 12-bit HEVC** ([mp4 §8.2](../spec/zarrify/mp4.md#82-tracks)) | **Not measured** | Fragments: parse `moof`. Others: more `refkv.stream` formats, or `imagecodecs_jpeg` per frame for MJPEG, `bytes` for PCM | **M** each | through PyAV, by extension / through WebCodecs where the browser has it / no |
| 31 | **Precomputed**: `compresso`, `crackle` (the fork has decoders), `jxl`, `fpzip`, `zfpc`, `kempressed` | **Tags:** 7 public volumes (hemibrain, FAFB, H01, MICrONS, FlyWire, male CNS) are all `raw`, `jpeg` or `compressed_segmentation` | Add to `refkv.precomputed`, and `ENCODINGS` in the fork | **S–M** | needs optional packages / compresso, crackle and jxl have decoders / no |
| 32 | **HDF5, `filter_mask`**: a chunk an optional filter skipped | **Sampled:** none in the 14 DANDI assets | Per chunk, a literal stored-deflate wrapper around the raw bytes, which needs reading those chunks for Adler-32 | **M** | yes / yes / no |

## Format by format

Every profile, with what it refuses that is a real variant rather than a malformed file. The fixtures
in `conformance/zarrify/rejects/<format>.json` hold one case per rejection; a number points to the
ranking. "Rare" and "uncommon" are general knowledge, not measured.

- **TIFF.** Predictor 3 (2), short compressed strips (5), LERC and WebP (10), multi-file OME-TIFF (13),
  EER (21), 1 and 12 bits, PackBits and CCITT (23). JPEG XL (50002) and LZMA are also refused: no sample here used them, and each is one existing imagecodecs codec. Rare: Predictor 2 on floats, old-style
  JPEG (6), 16-bit or planar JPEG, FillOrder 2, subsampled YCbCr outside JPEG. Mask IFDs are not refused:
  a tifffile test with one was accepted.
- **QPTIFF.** Inherits Predictor 3 (2). Several RGB channels are refused: uncommon.
- **NDPI.** Focal planes, two levels of one magnification, are refused. A `z` axis read from the file's
  focal offset would cover them. Share not measured.
- **DICOM.** Series and pyramids (7), 1-bit (8), RLE (16), photometric (17), HTJ2K and JPEG-LS (24).
  Also refused: extended JPEG (`.4.51`), Deflated Explicit VR, MPEG video, float pixel data (as `no Pixel Data element`), `TILED_SPARSE`, several focal planes or optical paths. All 50 sampled IDC WSI files were `TILED_FULL`,
  and the 44 whose optical path count was found had 1. Two of 6 WSI runs passed a 170 s limit. That may be the cost [dicom §7.7](../spec/zarrify/dicom.md#77-limits) warns of for files without an Extended Offset Table; it was not investigated.
- **CZI** ([§3.1](../spec/zarrify/czi.md#31-pixel-types-and-codecs)). Multi-file (13). Lossless JPEG, LZW,
  camera raw and JPEG with Gray16 are refused: rare in what ZEN writes, not measured.
- **ND2.** Tiled frames, lossy frames and legacy JPEG 2000 files (which match no signature). Old files,
  not measured. Legacy ones would need a parser of their own.
- **LIF.** LOF, XLEF, LIFEXT (26). `BitInc` other than 0 is refused: rare.
- **VSI.** ETS compressions PNG and BMP are refused, and a `tile_depth` other than 1. A macro image of
  the wrong kind rejects the whole file, where leaving it out would keep the slide: **S**, with no
  decoder. Share not measured.
- **IMS.** Shuffle and LZ4 (18), Fletcher32 (the trim of gap 1 applies), contiguous datasets, `float64`.
- **HDF5.** Fletcher32 (1), user blocks (6), Blosc, zstd and LZF (11), dimension names (12), bitshuffle
  and LZ4 (19), external data (20), `filter_mask` (32). The Szip, N-bit and scale-offset filters are also left out: `imagecodecs_szip` exists for the first, and no sample showed any of them. Datatypes refused: time, opaque, array, variable-length
  sequences, compounds with gaps. Clean here: 14 DANDI NWB assets, and GOES ABI and GLM, NWM, OISST,
  NEX-GDDP netCDF-4 files all had no dataset left out for a filter.
- **GRIB.** Grids (4), reduced grids, other templates and edition 1 (25). Whole-file `.bz2` and `.gz`.
  Spectral packing (5.50, 5.51) and logarithmic packing (5.61): uncommon outside ECMWF's archive.
- **FITS.** Tables as arrays (27), HCOMPRESS and PLIO (28), `.gz` and `.bz2` (29). Random groups (radio
  `uvfits`) and tile-compressed tables are refused: specialized.
- **MRC.** Mode 101 and mode 3 (22), `.map.gz` (3).
- **NIfTI.** `.nii.gz` (3), complex (14), pairs (15). Dimensions 6 and 7 (CIFTI-2) are refused: common
  in HCP-style data, but they need a meaning for the axes, not a codec.
- **NRRD.** Gzip with a byte skip (3). bzip2 is refused because the browser has no decoder
  ([nrrd §4.2](../spec/zarrify/nrrd.md#42-the-gzip-encoding)); `numcodecs.bz2` serves zarr-python. ASCII
  and hex cannot be references. `LIST` data files could follow gap 13.
- **NumPy.** Almost nothing to add. Object arrays are pickles and stay out. Other zip methods are rare,
  as NumPy writes only stored and deflated.
- **safetensors.** Nothing to add: every data type the format defines is presented.
- **Zarr v2, OME-Zarr 0.4.** Data types, filters, compressors (9), and complex (14). OME-Zarr inherits
  them, and also a store-wide rejection on one bad array.
- **N5.** bzip2, lz4, xz, and JPEG are refused. They would extend `n5_default`, with no sample showing them.
- **Precomputed.** Encodings (31).
- **SAFE.** Products of the format before compact naming, from before December 2016, are refused. Not
  measured how many remain in that form.
- **MP3, FLAC.** Layers I and II, free-format MP3, sample-rate changes, and FLAC with a variable block
  size. All uncommon. FLAC's would suit `rectilinear`, but FLAC is sharded, which that grid refuses.
- **MP4.** Gap 30.

## What recurs

**Mechanisms already in the repo cover most of this.**

| mechanism | gaps |
|---|---|
| profile only, no decoder | 1, 3, 4, 5, 6, 7, 12, 13, 14, 15, 17, 20, 26 |
| existing imagecodecs codec | 2, 10, 11, 16, 18, 21, 23, 24 |
| `rectilinear` grid | 5 (and FLAC's variable block size, if unsharded) |
| `packbits` | 8, 22 |
| `gzip` with `refkv.skip` | 3, 29 |
| a new `refkv.<codec>` | 19 only; 25, 28 and 31 extend existing ones |

**Sharding fills none of these gaps.** It reduces keys and requests for what is already covered.

**Sources beside the input** (gaps 7, 13, 15, 20, 26) would all share one design: how a profile names
sibling files, pins them and bounds their reads. It is worth settling once rather than five times.

**Rejecting a whole input for one part** is a separate pattern: Zarr v2 for one array (9), VSI for
one macro image. HDF5, GRIB and precomputed show the alternative: leave it out and record why.

**The name `packbits` means two things.** TIFF's PackBits is run-length coding,
`imagecodecs_packbits`. The zarr-extensions `packbits` is bit packing. A spec entry for either
should say which.

**Most new codecs cost the fork more than the Python side.** imagecodecs-zarr already registers nearly
every codec named here, so zarr-python often needs nothing, while the fork needs a decoder for each.
Shipping a profile that the fork cannot show is the choice each such gap has to make.

## How this was measured

Sampling:

- **IDC:** the first file after each of 400 random prefixes of `idc-open-data`, so about one per series;
  387 were DICOM. Headers came from the first 32 KiB, and WSI layout from the first 256 KiB. CT files per
  series are capped at the listing's 1,000.
- **OpenNeuro:** 60 datasets at random from the first 1,000 prefixes of `openneuro.org`, the first 1,000
  keys of each.
- **HDF5:** GOES-16 ABI and GLM, NWM, OISST, NEX-GDDP, Sentinel-5P, JPSS VIIRS, and the first NWB asset
  of 14 recently modified DANDI datasets under 6 GB.

To repeat a run, use `refkv zarrify <url> out.vzip` with these, for example:

```text
https://copernicus-dem-30m.s3.amazonaws.com/Copernicus_DSM_COG_10_N47_00_E008_00_DEM/Copernicus_DSM_COG_10_N47_00_E008_00_DEM.tif
https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/current/n40w106/USGS_13_n40w106.tif
https://ftp.ebi.ac.uk/pub/databases/emdb/structures/EMD-1234/map/emd_1234.map.gz
https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.20261001/00/atmos/gfs.t00z.sfluxgrbf006.grib2
https://noaa-hrrr-bdp-pds.s3.amazonaws.com/hrrr.20261001/alaska/hrrr.t00z.wrfsfcf01.ak.grib2
```

The scripts were throwaway and are not in the repo.

Not done: no gap was implemented, and no chain was run in the fork, apart from one HTJ2K stream through
its JPEG 2000 wasm. Open issues on d-v-b/refkv name no codec or variant gap. The closest is
[#93](https://github.com/d-v-b/refkv/issues/93), on new bioinformatics formats, which is out of scope here.

## Sources

- GDAL: [COG driver](https://gdal.org/en/stable/drivers/raster/cog.html)
- AWS: [HealthImaging transfer syntaxes](https://docs.aws.amazon.com/healthimaging/latest/devguide/supported-transfer-syntaxes.html)
- Cryo-EM: [SerialEM frame options](https://bio3d.colorado.edu/SerialEM/hlp/html/hidd_k2_save_options.htm), [RELION movie compression](https://relion.readthedocs.io/en/latest/Reference/MovieCompression.html), [Falcon detectors](https://www.thermofisher.com/mx/es/home/electron-microscopy/products/accessories-em/falcon-detector.html)
- Microscopy: [Micro-Manager file formats](https://micro-manager.org/Micro-Manager_File_Formats), [Bio-Formats CZI ticket](https://trac.openmicroscopy.org/ome/ticket/12189?version=3), [BigStitcher loaders](https://imagej.net/plugins/bigstitcher/specialloaders), [Bio-Formats 6.9.0 notes](https://docs.openmicroscopy.org/bio-formats/6.9.0/about/whats-new.html), [liffile](https://pypi.org/project/liffile/)
- Imaris: [ImarisWriter's options](https://github.com/imaris/ImarisWriter/blob/master/interface/bpConverterTypes.h), [file format](https://imaris.oxinst.com/support/imaris-file-format), [LZ4 demo images](https://zenodo.org/records/14197622)
- HDF5: [Jungfraujoch](https://gitea.psi.ch/mx/Jungfraujoch/src/branch/rc166/compression), [MATLAB v7.3 layout, in hdf5storage](https://hdf5storage.readthedocs.io/en/stable/storage_format.html)
- GRIB: [ERA5 complete](https://cds.climate.copernicus.eu/datasets/reanalysis-era5-complete), [COSMO GRIB2 section 3](https://www.cosmo-model.org/content/model/cosmo/grib/grib2keys_3.htm)
- FITS: [Pence et al. on lossless compression](https://arxiv.org/pdf/0903.2140), [fpack](https://manpages.org/fpack), [SDSS frame data model](https://data.sdss.org/datamodel/files/BOSS_PHOTOOBJ/frames/RERUN/RUN/CAMCOL/frame.html)
- Zarr v2: [AnnData's file format](https://anndata.readthedocs.io/en/latest/fileformat-prose.html)
- Codec: [zarr-extensions `packbits`](https://github.com/zarr-developers/zarr-extensions/tree/main/codecs/packbits)
