# Changelog

## [0.3.0](https://github.com/d-v-b/refkv/compare/v0.2.0...v0.3.0) (2026-11-13)


### ⚠ BREAKING CHANGES

* **conventions:** outputs with integers past 2^53 - 1 in magnitude change from strings of digits to JSON integers.
* **policy:** refkv.open and the other readers refuse file: sources by default, for local stores too.
* **reader:** in a browser, a store whose sources on the page's origin need the user's cookies or HTTP authentication fails to read them unless the reader policy has `allowSourceCredentials: true`.

### Features

* **build:** add refkv.build, to read an input and seal an output built from its IR ([#283](https://github.com/d-v-b/refkv/issues/283)) ([0a62324](https://github.com/d-v-b/refkv/commit/0a623248e2aafe36cc095a775893a1bcac49d49a))
* **compose:** combine reference stores by the keys each answers for ([#197](https://github.com/d-v-b/refkv/issues/197)) ([0fda325](https://github.com/d-v-b/refkv/commit/0fda32570db5094fbf190f505b6d0a1290442af4))
* **conformance:** a conformance suite for the phkv binding ([63ed599](https://github.com/d-v-b/refkv/commit/63ed5992af69828cdb08e929b4eea0d6f4b227b7))
* **conformance:** a conformance suite for the phkv binding ([8dcd756](https://github.com/d-v-b/refkv/commit/8dcd756374cdb8f2c51824bec79ec94b4be8858a))
* **czi:** read an image across its numbered files ([#251](https://github.com/d-v-b/refkv/issues/251)) ([86e3c9e](https://github.com/d-v-b/refkv/commit/86e3c9e756416a8fb5da79a47196283f7cfd6dfb))
* **czi:** read LZW, lossless JPEG and 12-bit JPEG subblocks ([#241](https://github.com/d-v-b/refkv/issues/241)) ([62e3cc4](https://github.com/d-v-b/refkv/commit/62e3cc48818c0c8218318411f6d8891ffabf7990))
* **demo:** add a phkv variant of the image-to-Zarr demo ([#194](https://github.com/d-v-b/refkv/issues/194)) ([3d736d6](https://github.com/d-v-b/refkv/commit/3d736d6d48f9bf4ebb0357d36fae6404f0195826))
* **demo:** highlight the Python script's syntax with highlight.js ([#176](https://github.com/d-v-b/refkv/issues/176)) ([7a7f8be](https://github.com/d-v-b/refkv/commit/7a7f8bee8e116678d5c0e204cc8e47459cde40f1))
* **demo:** show the result above the examples, and say why a read failed ([#181](https://github.com/d-v-b/refkv/issues/181)) ([20f46c9](https://github.com/d-v-b/refkv/commit/20f46c9580c7515f5a81899f1b4c06855b5a27fc))
* **dicom,mrc:** zarrify 1-bit DICOM images and MRC modes 3 and 101 ([#228](https://github.com/d-v-b/refkv/issues/228)) ([00f6f9f](https://github.com/d-v-b/refkv/commit/00f6f9fe96625ef5e15eb604db25c89e92e45dbf))
* **dicom:** join a store of DICOM files into one group per series ([#236](https://github.com/d-v-b/refkv/issues/236)) ([8c81e07](https://github.com/d-v-b/refkv/commit/8c81e07ef2a46a0a3e9bc6ce41f2260be2df9930))
* **dicom:** zarrify RLE, JPEG-LS and HTJ2K frames, and palette and YBR images ([#237](https://github.com/d-v-b/refkv/issues/237)) ([e7f0b94](https://github.com/d-v-b/refkv/commit/e7f0b949ff2e18dfccae4d24f69cfcfa6db8a171))
* **fits:** present binary tables as struct arrays over their rows ([#227](https://github.com/d-v-b/refkv/issues/227)) ([1950214](https://github.com/d-v-b/refkv/commit/19502146229735a631cab6a262582d22f38ff213))
* **fits:** zarrify whole files in gzip and bzip2 ([#242](https://github.com/d-v-b/refkv/issues/242)) ([9b067ef](https://github.com/d-v-b/refkv/commit/9b067ef43b928ec452f8b6be83e7ff0c829f3dcc))
* **grib:** present Gaussian, rotated, polar, Mercator, reduced and unstructured grids ([#248](https://github.com/d-v-b/refkv/issues/248)) ([e7d2c8f](https://github.com/d-v-b/refkv/commit/e7d2c8f0decf5b2eb29a1a61534e31ccff78acb7))
* **hdf5:** follow external links and external storage to files beside the input ([#258](https://github.com/d-v-b/refkv/issues/258)) ([484a76a](https://github.com/d-v-b/refkv/commit/484a76a86494a39e11b58bf3c35ea46c1fb2e805))
* **hdf5:** name dimensions from netCDF-4's dimension scales ([#252](https://github.com/d-v-b/refkv/issues/252)) ([c57eb25](https://github.com/d-v-b/refkv/commit/c57eb254bc5fae5342fcb815058a3cf7038c259a))
* **hdf5:** name the axes of datasets without dimension scales ([#276](https://github.com/d-v-b/refkv/issues/276)) ([395fabd](https://github.com/d-v-b/refkv/commit/395fabdcc4f57ce5574e9175e4abe3f1b8f3feb1))
* **hdf5:** present Fletcher32, third-party filters and skipped chunks ([#243](https://github.com/d-v-b/refkv/issues/243)) ([d148d3f](https://github.com/d-v-b/refkv/commit/d148d3f806c1de9cbbe9eb3db513997fea7a787d))
* **hdf5:** read files after a user block ([#249](https://github.com/d-v-b/refkv/issues/249)) ([8243214](https://github.com/d-v-b/refkv/commit/82432147d6b736bd0168ea90bd86cf01f3bbf2f5))
* **ir:** describe CZI subblocks as stored arrays, checked against czifile ([#313](https://github.com/d-v-b/refkv/issues/313)) ([2cb3b72](https://github.com/d-v-b/refkv/commit/2cb3b723c8761d6bb815a9b1d2d8840b8546f261))
* **ir:** describe DICOM pixel data as stored arrays, checked against pydicom ([#316](https://github.com/d-v-b/refkv/issues/316)) ([fd569b7](https://github.com/d-v-b/refkv/commit/fd569b74f492436b28236686bab08c8450273359))
* **ir:** describe FITS images, tables and tile-compressed images as stored arrays, checked against astropy ([#311](https://github.com/d-v-b/refkv/issues/311)) ([a5c8add](https://github.com/d-v-b/refkv/commit/a5c8add37e9f3d528a694023e5d00ce5b6165436))
* **ir:** describe GRIB bitmaps and fields as stored arrays, checked against ecCodes ([#317](https://github.com/d-v-b/refkv/issues/317)) ([ef3cc91](https://github.com/d-v-b/refkv/commit/ef3cc9127ea625fe5f24e4fe213b9b6502b872d3))
* **ir:** describe HDF5 and IMS datasets as stored arrays, checked against h5py ([#296](https://github.com/d-v-b/refkv/issues/296)) ([0302f09](https://github.com/d-v-b/refkv/commit/0302f097e47a18e8cb0258739d1e52544bfe8128))
* **ir:** describe LIF images as stored arrays, checked against liffile ([#315](https://github.com/d-v-b/refkv/issues/315)) ([bad7fef](https://github.com/d-v-b/refkv/commit/bad7fef19bacafddb20e3af475b48cf664d21696))
* **ir:** describe MP4 tracks as stored arrays, checked against ffmpeg ([#303](https://github.com/d-v-b/refkv/issues/303)) ([b75eb89](https://github.com/d-v-b/refkv/commit/b75eb897bd17be236b1f067709e55e7f02f7246e))
* **ir:** describe MRC data as stored arrays, checked against mrcfile ([#308](https://github.com/d-v-b/refkv/issues/308)) ([6a14d13](https://github.com/d-v-b/refkv/commit/6a14d134fdcecbdddc93d6f5ffc4a5e8b34be5be))
* **ir:** describe ND2 frames as stored arrays, checked against the nd2 library ([#309](https://github.com/d-v-b/refkv/issues/309)) ([68c930c](https://github.com/d-v-b/refkv/commit/68c930c84eff0c0c2dcc0c7eb50067231a7fc510))
* **ir:** describe NDPI images as stored arrays, checked against tifffile and imagecodecs ([#300](https://github.com/d-v-b/refkv/issues/300)) ([b3f87cb](https://github.com/d-v-b/refkv/commit/b3f87cb0ebb03c1c4d3af97d8cec20c77391bc3a))
* **ir:** describe NIfTI voxels as stored arrays, checked against nibabel ([#307](https://github.com/d-v-b/refkv/issues/307)) ([08db67b](https://github.com/d-v-b/refkv/commit/08db67bcb3bb5bd3bf1f815ad2233ee71fb92730))
* **ir:** describe NRRD arrays as stored arrays, checked against pynrrd ([#306](https://github.com/d-v-b/refkv/issues/306)) ([fc9cc44](https://github.com/d-v-b/refkv/commit/fc9cc441fe87e38d654d334a206a9ee6d95ae7d3))
* **ir:** describe SAFE band images as stored arrays, checked against rasterio ([#312](https://github.com/d-v-b/refkv/issues/312)) ([17d3a02](https://github.com/d-v-b/refkv/commit/17d3a02fa19d6e4459024f320b68cb1e15f8b381))
* **ir:** describe safetensors tensors as stored arrays, checked against the safetensors library ([#305](https://github.com/d-v-b/refkv/issues/305)) ([8a907b9](https://github.com/d-v-b/refkv/commit/8a907b95036a60c278d67dfe24202097e236fc27))
* **ir:** describe stored arrays at the byte level, with NumPy as the first user ([#290](https://github.com/d-v-b/refkv/issues/290)) ([c86ae06](https://github.com/d-v-b/refkv/commit/c86ae0633920ae3b7b1037b48c8e321ebb32ba6f))
* **ir:** describe TIFF and QPTIFF images as stored arrays, checked against tifffile ([#297](https://github.com/d-v-b/refkv/issues/297)) ([4d37211](https://github.com/d-v-b/refkv/commit/4d37211a7c74f289531f514da2357de7136dd4d9))
* **ir:** describe VSI tile levels and IFD images as stored arrays, checked against Bio-Formats, tifffile and imagecodecs ([#314](https://github.com/d-v-b/refkv/issues/314)) ([02a77c8](https://github.com/d-v-b/refkv/commit/02a77c8f776df2ab6a5a735908ef0f729797b10a))
* **ir:** fall back at the descriptions' limit and mark a source that keeps no descriptions ([#295](https://github.com/d-v-b/refkv/issues/295)) ([0d2db67](https://github.com/d-v-b/refkv/commit/0d2db67b54870d6b0994cd3d61286fb7600b2b38))
* **ir:** widen the jpeg codec to samples of up to 16 bits ([#319](https://github.com/d-v-b/refkv/issues/319)) ([ae75618](https://github.com/d-v-b/refkv/commit/ae75618beebad09ad871f6cc1acffabba774aabe))
* **js:** save the archives the demo's worker zarrifies in Cache Storage ([#180](https://github.com/d-v-b/refkv/issues/180)) ([ba8970e](https://github.com/d-v-b/refkv/commit/ba8970e1e59d4edd3f06bc45d056f954fa7beb67))
* **lif:** read LAS X projects as stores ([#253](https://github.com/d-v-b/refkv/issues/253)) ([4844c03](https://github.com/d-v-b/refkv/commit/4844c03bb19a0422b9383418bc47ec0331e634ae))
* **mp4:** zarrify AV1, VP9, 10-bit H.264 and 4:2:2 and 12-bit HEVC ([#262](https://github.com/d-v-b/refkv/issues/262)) ([ade6af8](https://github.com/d-v-b/refkv/commit/ade6af89fd4aaca8b5d9c3c0e9e86ec64c18afe4))
* **mp4:** zarrify fragmented files by parsing moof boxes ([#231](https://github.com/d-v-b/refkv/issues/231)) ([54b8f14](https://github.com/d-v-b/refkv/commit/54b8f144e88e16e9b4ac4a4078688968087813ec))
* **mp4:** zarrify HE-AAC and HE-AAC v2 tracks ([#264](https://github.com/d-v-b/refkv/issues/264)) ([b5eae81](https://github.com/d-v-b/refkv/commit/b5eae81bfbfc421d2c974122945404c232cc1d34))
* **mp4:** zarrify Motion JPEG tracks through imagecodecs_jpeg ([#261](https://github.com/d-v-b/refkv/issues/261)) ([e8a1721](https://github.com/d-v-b/refkv/commit/e8a17215bb9e14fb88231be477b61d914874c258))
* **mp4:** zarrify PCM audio tracks through the bytes codec ([#260](https://github.com/d-v-b/refkv/issues/260)) ([d0fea2b](https://github.com/d-v-b/refkv/commit/d0fea2b7ba5d554a0bebc6860f1107b9174dbae1))
* **ndpi:** present focal planes as a z axis ([#229](https://github.com/d-v-b/refkv/issues/229)) ([91a6b45](https://github.com/d-v-b/refkv/commit/91a6b45479b18cbca9331a12b281a19bd6b3aeb8))
* **nifti,mrc,nrrd:** read gzipped single-array files as one chunk of the whole stream ([#247](https://github.com/d-v-b/refkv/issues/247)) ([784e5e4](https://github.com/d-v-b/refkv/commit/784e5e462190b9e429bc87c099085068dee6c735))
* **nifti:** present COMPLEX64 and COMPLEX128 voxels as complex arrays ([#232](https://github.com/d-v-b/refkv/issues/232)) ([7ce91ca](https://github.com/d-v-b/refkv/commit/7ce91cadb8b5d8c21d1e3ba46f55f7a4dc903dc6))
* **nifti:** read header-and-image pairs, the image file found beside the header ([#250](https://github.com/d-v-b/refkv/issues/250)) ([95dbd6f](https://github.com/d-v-b/refkv/commit/95dbd6feb19c63a2e723db50075b960de432f276))
* **policy:** bound a get's bytes, coalesce within them, make read amplification a reader setting, and leave read limits to readers ([#286](https://github.com/d-v-b/refkv/issues/286)) ([7d59745](https://github.com/d-v-b/refkv/commit/7d597458aa2c2176d11448ff03dcaacc80a9a222))
* **policy:** refuse every file: source unless the application allows it ([#266](https://github.com/d-v-b/refkv/issues/266)) ([089823c](https://github.com/d-v-b/refkv/commit/089823ced6f0cc17d5f2dbd5ec158ac44450c67f))
* **precomputed:** add compresso, crackle, jxl, fpzip, zfpc and kempressed ([#230](https://github.com/d-v-b/refkv/issues/230)) ([08ef7ca](https://github.com/d-v-b/refkv/commit/08ef7ca76b5d24c743633b9ac7820110f68cfa4f))
* **python:** add ReferenceStore.save_phkv, which calls refkv_phkv.save ([fa617e1](https://github.com/d-v-b/refkv/commit/fa617e1aa1ee668e364aed51a07d8e60af6e7c44))
* **python:** add refkv-phkv, the perfect-hash binding as a distribution of its own ([aa12fed](https://github.com/d-v-b/refkv/commit/aa12fed40d0fa98147adc9d0a97f8101b9d759fa))
* **python:** convert a store between the vzip and phkv bindings ([#269](https://github.com/d-v-b/refkv/issues/269)) ([91e4ba9](https://github.com/d-v-b/refkv/commit/91e4ba944ccf2c2ac6a09afe1f957db27ee09d63))
* **python:** phkv as accessible as vzip: --format phkv on refkv zarrify and refkv.ir, refkv.open infers the binding from a .phkv name ([a313117](https://github.com/d-v-b/refkv/commit/a3131170fe77aa1c6b5b32d3e024065521da2a3c))
* **python:** read and write phkv stores (refkv.phkv, ReferenceStore.save_phkv) ([#141](https://github.com/d-v-b/refkv/issues/141)) ([1f0b598](https://github.com/d-v-b/refkv/commit/1f0b5989a852e992d9d7900c49f89d4135f5f415))
* **python:** save_phkv leaves out the hidden __vz__/ values (phkv stays in refkv) ([b43bd78](https://github.com/d-v-b/refkv/commit/b43bd783d65b7dafffbc834ca1bb58c343331b01))
* **python:** save_phkv skips the hidden __vz__/ values unless a key source reads one ([aca5de9](https://github.com/d-v-b/refkv/commit/aca5de94d98bf32194a99608cffc9e210e3b5a9e))
* **reader:** merge nearby reads of different keys into one request ([#213](https://github.com/d-v-b/refkv/issues/213)) ([0710ae3](https://github.com/d-v-b/refkv/commit/0710ae39a517b15b39f2971bd146b47c6c3aa638))
* **reader:** read a store's sources in a browser as an anonymous client ([#285](https://github.com/d-v-b/refkv/issues/285)) ([ca72fd3](https://github.com/d-v-b/refkv/commit/ca72fd37fc6a03cc8307fc405ca223e511d36ac9))
* **reader:** wait for a request in flight when a source is busy ([#225](https://github.com/d-v-b/refkv/issues/225)) ([138d269](https://github.com/d-v-b/refkv/commit/138d269253e8a640b13050d9f1fe1b8cd19b21fe))
* **refstore:** deprecate `save` in favor of `save_vzip` ([#199](https://github.com/d-v-b/refkv/issues/199)) ([5d1a84e](https://github.com/d-v-b/refkv/commit/5d1a84e42ac571993e8a89e9d6ad191905301c5f))
* **safetensors:** zarrify a store of safetensors files as one group, a group per shard ([#189](https://github.com/d-v-b/refkv/issues/189)) ([5d4de4b](https://github.com/d-v-b/refkv/commit/5d4de4b65e7f799445ebf2bb4943a2eea6f1046b))
* **safetensors:** zarrify safetensors files, each tensor a Zarr array ([#186](https://github.com/d-v-b/refkv/issues/186)) ([743764c](https://github.com/d-v-b/refkv/commit/743764ca74bc6554dc0e74f4f3805e372d07e5ee))
* **seal:** move seal into the core and seal every built-in output with it ([#284](https://github.com/d-v-b/refkv/issues/284)) ([e6c9ed1](https://github.com/d-v-b/refkv/commit/e6c9ed1f15ebff7c413a43dfcdc3f70ceed10598))
* **store:** list a Hugging Face Hub repository at a pinned commit ([#188](https://github.com/d-v-b/refkv/issues/188)) ([240cc19](https://github.com/d-v-b/refkv/commit/240cc19429863a256eef2b4c56ff47be829f8996))
* **tiff:** give a short last compressed strip a rectilinear grid ([#233](https://github.com/d-v-b/refkv/issues/233)) ([d48834c](https://github.com/d-v-b/refkv/commit/d48834c79458f615b071fc45b104b72ad0caede2))
* **tiff:** join uncompressed strips into larger chunks ([#214](https://github.com/d-v-b/refkv/issues/214)) ([dec9a3a](https://github.com/d-v-b/refkv/commit/dec9a3ac0330fee36f1aea6c67ab23499ea75458))
* **tiff:** read an OME-TIFF across files and Micro-Manager stacks ([#244](https://github.com/d-v-b/refkv/issues/244)) ([e02cd52](https://github.com/d-v-b/refkv/commit/e02cd52eb0eb7dc203b0ee3840389e4b4c5a5e8e))
* **tiff:** read ImageJ hyperstacks as their planes, one IFD each or in one IFD ([#304](https://github.com/d-v-b/refkv/issues/304)) ([3d6a563](https://github.com/d-v-b/refkv/commit/3d6a563a42c0529ad13a76666ab59af5a2b88711))
* **tiff:** shard LZW, Deflate and zstd strips ([#217](https://github.com/d-v-b/refkv/issues/217)) ([7617ce2](https://github.com/d-v-b/refkv/commit/7617ce25232395bf0cfc76c02da6294a4d108a37))
* **tiff:** zarrify LERC, WebP, JPEG XL and LZMA through imagecodecs ([#234](https://github.com/d-v-b/refkv/issues/234)) ([bd7d041](https://github.com/d-v-b/refkv/commit/bd7d041066de9d1bf58290f6f1f7456b453da8d9))
* **tiff:** zarrify packed samples, PackBits, CCITT Group 4 and EER ([#235](https://github.com/d-v-b/refkv/issues/235)) ([a0021da](https://github.com/d-v-b/refkv/commit/a0021daae51c7617eae26d8be4aeca7fb9f7b14b))
* **tiff:** zarrify Predictor 3 images through imagecodecs_floatpred ([#240](https://github.com/d-v-b/refkv/issues/240)) ([91ee04e](https://github.com/d-v-b/refkv/commit/91ee04e0180c44de0de85bf4b556c6cb82725146))
* **vsi:** leave out a macro image of the wrong kind, and read PNG and BMP tiles ([#226](https://github.com/d-v-b/refkv/issues/226)) ([9606274](https://github.com/d-v-b/refkv/commit/960627486215527ce563b4050778dd23218ef747))
* **zarr2:** leave out arrays that cannot be presented, and read NPY's data types and more codecs ([#238](https://github.com/d-v-b/refkv/issues/238)) ([bf6f788](https://github.com/d-v-b/refkv/commit/bf6f7889c0bbdeed6d05d79e553d92569004be82))
* **zarr3:** zarrify Zarr v3 stores, listed or zipped ([#277](https://github.com/d-v-b/refkv/issues/277)) ([fde5f47](https://github.com/d-v-b/refkv/commit/fde5f4724f0b25af9f3a9a6dd69eb2b16dbd07a4))
* **zarrify:** give a file input an explicit scope of files it may read ([#280](https://github.com/d-v-b/refkv/issues/280)) ([3b73f3c](https://github.com/d-v-b/refkv/commit/3b73f3cb5e79f693c919a14e82551e9532ed1c7a))
* **zarrify:** read inputs through the application's object stores ([#275](https://github.com/d-v-b/refkv/issues/275)) ([25018f9](https://github.com/d-v-b/refkv/commit/25018f980844e957cdea2f7634ed641b3596eac0)), closes [#271](https://github.com/d-v-b/refkv/issues/271)
* **zarrify:** zarrify Neuroglancer precomputed volumes as one reference per object ([#203](https://github.com/d-v-b/refkv/issues/203)) ([e10e93e](https://github.com/d-v-b/refkv/commit/e10e93eda1e2ec93857533e66c3d1665ea844ff7))


### Bug fixes

* **build:** stop a Parsed read with checksums from keeping its input file mapped, and run Windows pytest on PRs ([#287](https://github.com/d-v-b/refkv/issues/287)) ([c109d5e](https://github.com/d-v-b/refkv/commit/c109d5e8c53b1a4c4ba31db2197f51b274087ea6))
* **conformance:** address the review of the phkv conformance suite ([#268](https://github.com/d-v-b/refkv/issues/268)) ([da25ab9](https://github.com/d-v-b/refkv/commit/da25ab98acae3f471a895b5b8497f023794ed1a9))
* **conformance:** make the phkv suite pass just preflight ([f7e70f2](https://github.com/d-v-b/refkv/commit/f7e70f2fb08000483a2302acc8981afdfe4ed32a))
* **conformance:** stop printing a traceback for each client that resets its connection ([#320](https://github.com/d-v-b/refkv/issues/320)) ([384bf6c](https://github.com/d-v-b/refkv/commit/384bf6c462ef775cc6c640d06c0bda780615ee1d))
* **conventions:** write every integer as an exact JSON integer ([#288](https://github.com/d-v-b/refkv/issues/288)) ([29e6fe7](https://github.com/d-v-b/refkv/commit/29e6fe7fe1aceb66af2e4aab21b3e7fa451c389d))
* **demo:** list every codec the Neuroglancer fork decodes ([#218](https://github.com/d-v-b/refkv/issues/218)) ([9dcbc49](https://github.com/d-v-b/refkv/commit/9dcbc4956e6e04679f3bf348d1450a0306668996))
* **ir:** accept left-out derived spaces and alias targets in a stored table ([#202](https://github.com/d-v-b/refkv/issues/202)) ([7851649](https://github.com/d-v-b/refkv/commit/785164946e30d737550d4557de047960fc862f3b))
* **ir:** give the skip codec one entry, its length a value or a place ([#310](https://github.com/d-v-b/refkv/issues/310)) ([4920132](https://github.com/d-v-b/refkv/commit/4920132c10bb2959c1a69649c76410a58d2e5007))
* **ir:** refuse a null-length src part past the element's end as an identity recipe ([#293](https://github.com/d-v-b/refkv/issues/293)) ([112af7b](https://github.com/d-v-b/refkv/commit/112af7b4e2c6d266fcedc9735eb338982ca92261))
* **js:** answer a zarrify that outlasts a minute as pending, so the page asks again ([#178](https://github.com/d-v-b/refkv/issues/178)) ([d2fb777](https://github.com/d-v-b/refkv/commit/d2fb777fa3bccfa8c8fe931a73e29741d76468df))
* **mrc:** place IMOD's negated origins at the first voxel's position ([#263](https://github.com/d-v-b/refkv/issues/263)) ([f32a612](https://github.com/d-v-b/refkv/commit/f32a61260b5e1aa4452e9982a22e4b0e53197159))
* **mrc:** read IMOD's flag bit 0 as signed bytes, and a stamped file without it as unsigned ([#254](https://github.com/d-v-b/refkv/issues/254)) ([b7816e9](https://github.com/d-v-b/refkv/commit/b7816e9998e862d0daae7690b3ec521fa231fccd))
* **python:** keep the existing file when an archive write fails ([#212](https://github.com/d-v-b/refkv/issues/212)) ([faf8d57](https://github.com/d-v-b/refkv/commit/faf8d5710a91901d8d7c57c2d378612706e79708))
* **reader:** open a store once for concurrent first reads ([#211](https://github.com/d-v-b/refkv/issues/211)) ([1068a66](https://github.com/d-v-b/refkv/commit/1068a661fa79be8ab0902b81d40632b5409d956a))
* **refs:** read recipes by the grammar, refusing malformed parts and reading rows ([#292](https://github.com/d-v-b/refkv/issues/292)) ([cd9f60c](https://github.com/d-v-b/refkv/commit/cd9f60c127f2aba5eafe7de8cea51d95ac1f42e0))
* **refstore:** keep the existing file when save_vzip fails ([#210](https://github.com/d-v-b/refkv/issues/210)) ([dc1ffbc](https://github.com/d-v-b/refkv/commit/dc1ffbc3b2eb46fd558128be59402a256fa0e49c))
* **spec:** restore the npy schema's offset description in its source, and check schemas are current ([#187](https://github.com/d-v-b/refkv/issues/187)) ([54d72de](https://github.com/d-v-b/refkv/commit/54d72de6b61c2eadabe889f6590b881760b59514))
* **tiff:** keep every IFD's tile and strip tables in the IR ([#281](https://github.com/d-v-b/refkv/issues/281)) ([3b09168](https://github.com/d-v-b/refkv/commit/3b09168fc373b461c050417a31223bb175d06313))
* **tiff:** read a tiled image whose tiles are listed in the strips' tables ([#301](https://github.com/d-v-b/refkv/issues/301)) ([c5f2d5d](https://github.com/d-v-b/refkv/commit/c5f2d5de9e29f10d2644ad81a841e1d8457f5fb1))
* **vzip:** check the source table before VZipWriter.close writes, and fill the API docs' gaps ([#184](https://github.com/d-v-b/refkv/issues/184)) ([04cec13](https://github.com/d-v-b/refkv/commit/04cec13fbb5aade5ff2e1b2661bd1ce948b177ed))
* **zarrify:** find a sibling absent when a symbolic link leads outside the input's directory ([#265](https://github.com/d-v-b/refkv/issues/265)) ([e76fc55](https://github.com/d-v-b/refkv/commit/e76fc55c15120250bf604cf1a958c6a8074d3e29))


### Performance

* **phkv:** list reads the pages a prefix needs in one request ([#190](https://github.com/d-v-b/refkv/issues/190)) ([d4c759d](https://github.com/d-v-b/refkv/commit/d4c759de78058d94a85f77930a26a6f7dcb483f4))


### Documentation

* **design:** keep both bindings in the refkv package ([c3d1541](https://github.com/d-v-b/refkv/commit/c3d1541d67855fd585e2234cc51178b749fc45bc))
* **design:** measure the shard index an HDF5 file already holds ([#144](https://github.com/d-v-b/refkv/issues/144)) ([d213a92](https://github.com/d-v-b/refkv/commit/d213a92372ff5bfd972ad3122d801c2e03662c22))
* **design:** plan vzip and phkv as separate packages ([#150](https://github.com/d-v-b/refkv/issues/150)) ([abb673f](https://github.com/d-v-b/refkv/commit/abb673fc89866b81ea9008ea059ab5dcbb38ebc3))
* **design:** propose a byte-level description of stored arrays in the IR ([#289](https://github.com/d-v-b/refkv/issues/289)) ([95ca8dc](https://github.com/d-v-b/refkv/commit/95ca8dc11ef805ce0646bd2bcf6dbfff33d53c69))
* **design:** propose a describe-only PR for every format's stored arrays ([#294](https://github.com/d-v-b/refkv/issues/294)) ([52b42cd](https://github.com/d-v-b/refkv/commit/52b42cd3281bf0b9c65f5560fa432dae1c87e95b))
* **design:** propose public tools to build an output from the IR ([#278](https://github.com/d-v-b/refkv/issues/278)) ([ee095ae](https://github.com/d-v-b/refkv/commit/ee095aebb8401f510a614e0fee6dc5ab3f9e3934))
* **design:** propose store operators and key bounds ([#193](https://github.com/d-v-b/refkv/issues/193)) ([7b6ec6c](https://github.com/d-v-b/refkv/commit/7b6ec6ccb924f15f7c2a9297203dcf9f73317c77))
* install refkv from PyPI, and bring the docs up to the release and the worker ([#182](https://github.com/d-v-b/refkv/issues/182)) ([640dcc2](https://github.com/d-v-b/refkv/commit/640dcc26755fb4603b4c729a933e26a06c20603e))
* **ir:** bring the IR design and contributor docs up to date with the code ([#205](https://github.com/d-v-b/refkv/issues/205)) ([0960d5f](https://github.com/d-v-b/refkv/commit/0960d5f793a8f147b01fd12bb7ef498fefdb632b))
* **readme:** lead with zarrifying a file, and read its result back ([#183](https://github.com/d-v-b/refkv/issues/183)) ([7fffa1c](https://github.com/d-v-b/refkv/commit/7fffa1c2ce4034dc86bc00bf3c6f8916aab647d7))
* **readme:** link the live demo under the introduction ([#179](https://github.com/d-v-b/refkv/issues/179)) ([7fb7973](https://github.com/d-v-b/refkv/commit/7fb79736da870a6b0aad0c74754a444c67cdb27d))
* **spec:** correct the IR spec to what the producers write ([#204](https://github.com/d-v-b/refkv/issues/204)) ([e0f15b4](https://github.com/d-v-b/refkv/commit/e0f15b42e824d13befa8712aa2a09851df4357f5))
* **spec:** draft a provenance convention that names the program that wrote a hierarchy ([#216](https://github.com/d-v-b/refkv/issues/216)) ([f33a3a8](https://github.com/d-v-b/refkv/commit/f33a3a82fda71bf33689273c82f8b23cb8a50faa))
* **spec:** list codec names in a table, one row per codec ([#274](https://github.com/d-v-b/refkv/issues/274)) ([962e19d](https://github.com/d-v-b/refkv/commit/962e19d500f69eba66cbdc5ce5f1dcb30d56455a))
* **spec:** move the vzip page index out of the core protobuf schema ([#302](https://github.com/d-v-b/refkv/issues/302)) ([db19e5a](https://github.com/d-v-b/refkv/commit/db19e5a782ec21fabd55890724524b7e6a859e11))
* stack a directory of TIFF files as one array in a vzip sidecar ([#192](https://github.com/d-v-b/refkv/issues/192)) ([689902e](https://github.com/d-v-b/refkv/commit/689902efa33ca9c5be6451f4fb78ce625b015b5a))
* **zarrify:** correct the specs and READMEs to what the examples produce ([#200](https://github.com/d-v-b/refkv/issues/200)) ([c307b0f](https://github.com/d-v-b/refkv/commit/c307b0f0618aad4a9770d2e98d3a2b50d067da3f))
* **zarrify:** describe the MP3 browser path as float32 and refkv.stream ([#198](https://github.com/d-v-b/refkv/issues/198)) ([e26dc27](https://github.com/d-v-b/refkv/commit/e26dc273f63d1b8e855e0da5d616651fe07de99f))
* **zarrify:** specify what only the code defined, and give every profile changelog one form ([#185](https://github.com/d-v-b/refkv/issues/185)) ([d7abf64](https://github.com/d-v-b/refkv/commit/d7abf64b93023ba427f8793d51f1af6241546ba1))

## [0.2.0](https://github.com/d-v-b/refkv/compare/v0.1.0...v0.2.0) (2026-10-31)


### ⚠ BREAKING CHANGES

* **zarr:** archives name numcodecs.zlib for zlib streams and refkv.tiff_lzw for QPTIFF LZW tiles; refkv no longer registers zlib, imagecodecs_lzw, imagecodecs_jpeg, imagecodecs_jpeg2k or imagecodecs_jpegxr (the last three come from imagecodecs-zarr, in the images extra).
* say zarrify for virtualize, in the code, the CLI, the spec and the docs ([#112](https://github.com/d-v-b/refkv/issues/112))
* **mp4:** cut video chunks at keyframes and decode a chunk's planes once, as a stream ([#107](https://github.com/d-v-b/refkv/issues/107))
* **ir:** add the refkv.stream codec, port MP3 onto it, and virtualize MP4 files as the MP4 profile (§18) ([#99](https://github.com/d-v-b/refkv/issues/99))
* **reader:** read http(s) archives with refkv's own HTTP client, and make obstore an extra ([#72](https://github.com/d-v-b/refkv/issues/72))
* **python:** make python/ a self-contained package versioned from git tags ([#71](https://github.com/d-v-b/refkv/issues/71))

### Features

* **cli:** add `vzip convert`, a Rust port of the converter that writes identical archives ([#78](https://github.com/d-v-b/refkv/issues/78)) ([a6fb670](https://github.com/d-v-b/refkv/commit/a6fb67054228d345eeccb61a154a2fe0c1591983))
* **demo:** add a QuickTime .mov from DANDI 001538 to the MP4 examples ([#154](https://github.com/d-v-b/refkv/issues/154)) ([3fabeb2](https://github.com/d-v-b/refkv/commit/3fabeb2b960499e55175d2acb9135027db420960))
* **demo:** add examples of LIF, QPTIFF, HDF5, NumPy, MP3 and FLAC files ([#160](https://github.com/d-v-b/refkv/issues/160)) ([099d5b2](https://github.com/d-v-b/refkv/commit/099d5b267981d8f8ebb97469a023fa423e3987bd))
* **demo:** add NOAA HRRR and GFS forecasts as GRIB examples ([#170](https://github.com/d-v-b/refkv/issues/170)) ([4b3d193](https://github.com/d-v-b/refkv/commit/4b3d193e2583e330da661dcf1fc2e29d860ab1bd))
* **demo:** link a hosted .vzip archive as a plain vzip: Neuroglancer source ([#126](https://github.com/d-v-b/refkv/issues/126)) ([1f5ffbc](https://github.com/d-v-b/refkv/commit/1f5ffbc756c4f2c998ed229de13bd526b90454a1))
* **demo:** make a DANDI behavior video the MP4 example ([#152](https://github.com/d-v-b/refkv/issues/152)) ([2fa1ea1](https://github.com/d-v-b/refkv/commit/2fa1ea13ac744a330cdad35dd052afd27d186204))
* **demo:** show a Python script that builds the same Zarr hierarchy ([#169](https://github.com/d-v-b/refkv/issues/169)) ([cbe7ad0](https://github.com/d-v-b/refkv/commit/cbe7ad0c71bdd91f3f5922646ed55cc2e151fea8))
* **demo:** show an MP4 file's video in Neuroglancer, with a SLEAP example ([#108](https://github.com/d-v-b/refkv/issues/108)) ([463c316](https://github.com/d-v-b/refkv/commit/463c3162c0548a06bade84eff5ae81834efe059b))
* **dicom:** zarrify JPEG Lossless frames as imagecodecs_ljpeg chunks ([#163](https://github.com/d-v-b/refkv/issues/163)) ([97c4ef0](https://github.com/d-v-b/refkv/commit/97c4ef09cdb8858a0ad31d55727bbb45f687d865))
* **flac:** zarrify FLAC files as virtual shards of frames, with a shared shard pass ([#121](https://github.com/d-v-b/refkv/issues/121)) ([df14a47](https://github.com/d-v-b/refkv/commit/df14a47a4433c26d0d8bba234cbfb47365b49f4e))
* **grib:** zarrify GRIB2 files, each message a chunk decoded by refkv.grib ([#166](https://github.com/d-v-b/refkv/issues/166)) ([0dd9063](https://github.com/d-v-b/refkv/commit/0dd9063e7fc3debfee8c9f44a0daf8b0e47c89ad))
* **hdf5:** zarrify HDF5 files, NWB first among them, in hdmf-zarr's Zarr v3 form ([#143](https://github.com/d-v-b/refkv/issues/143)) ([8a56953](https://github.com/d-v-b/refkv/commit/8a5695327744ec9a07ff241e2bab1a5b6c200fac))
* **ir:** add the refkv.stream codec, port MP3 onto it, and virtualize MP4 files as the MP4 profile (§18) ([#99](https://github.com/d-v-b/refkv/issues/99)) ([8f79104](https://github.com/d-v-b/refkv/commit/8f791040e966861afa64ee8382a94c53a6ae8261))
* **ir:** virtualize MP3 files through the Rust IR, as the MP3 profile (§16) ([#91](https://github.com/d-v-b/refkv/issues/91)) ([0b127f3](https://github.com/d-v-b/refkv/commit/0b127f3f181ab32c3430c81c0b1f27f88c21c91a))
* **ir:** virtualize MRC2014 files through the Rust IR, as the MRC profile (revision 28) ([#83](https://github.com/d-v-b/refkv/issues/83)) ([5b43073](https://github.com/d-v-b/refkv/commit/5b4307322d1fcb27d0ead91023343c23896903e9))
* **ir:** virtualize Olympus VSI slides through the Rust IR, as the VSI profile (revision 29) ([#87](https://github.com/d-v-b/refkv/issues/87)) ([8ab760f](https://github.com/d-v-b/refkv/commit/8ab760f881bd3dff84c3eef86a7e1f761c519225))
* **js:** build refkv_embed.js, the virtualizer and FITS tile decoder for a host that reads the input itself ([#109](https://github.com/d-v-b/refkv/issues/109)) ([62af0f1](https://github.com/d-v-b/refkv/commit/62af0f1706f7c0ce1c95699f8e4529b4be530bc4))
* **lif:** zarrify Leica LIF files, as the LIF profile ([#118](https://github.com/d-v-b/refkv/issues/118)) ([4a14bec](https://github.com/d-v-b/refkv/commit/4a14bec642cd8fc1c2c45b49d87000456280fffe))
* **mp4:** cut video chunks at keyframes and decode a chunk's planes once, as a stream ([#107](https://github.com/d-v-b/refkv/issues/107)) ([fd78ebe](https://github.com/d-v-b/refkv/commit/fd78ebe55517144cb37cd3c1513a5fbfc33b3754))
* **mp4:** zarrify H.264 tracks whose pictures are presented before their sync sample ([#110](https://github.com/d-v-b/refkv/issues/110)) ([8fec438](https://github.com/d-v-b/refkv/commit/8fec4387a428412072ec959b13b7e1129b009578))
* **mp4:** zarrify HEVC video tracks, 8- and 10-bit 4:2:0 ([#151](https://github.com/d-v-b/refkv/issues/151)) ([99eda76](https://github.com/d-v-b/refkv/commit/99eda76bba6b8991a363769c62f8d528009d2e06))
* **ndpi:** zarrify JPEG XR levels, with the pixels their streams hold ([#125](https://github.com/d-v-b/refkv/issues/125)) ([3e3cdc1](https://github.com/d-v-b/refkv/commit/3e3cdc1d7c8667b3cc598766de573f550ed1d8f7))
* **npy:** present the deflated members of .npz archives, as np.savez_compressed writes them ([#158](https://github.com/d-v-b/refkv/issues/158)) ([9d409d9](https://github.com/d-v-b/refkv/commit/9d409d909ad28c71daa7ec90e0e26e35b34c144f))
* **npy:** zarrify NumPy .npy files and the stored arrays of .npz archives ([#156](https://github.com/d-v-b/refkv/issues/156)) ([2780e2e](https://github.com/d-v-b/refkv/commit/2780e2ec665ebe1922da8a371dc4a998a4a7d4d1))
* **nrrd:** zarrify NRRD files, attached or detached, raw or gzip, as the NRRD profile ([#119](https://github.com/d-v-b/refkv/issues/119)) ([465d575](https://github.com/d-v-b/refkv/commit/465d57582797c301a3904713748cbab2289d18c0))
* **phkv:** add the perfect-hash binding (Part 3, draft), standalone and hybrid with vzip ([#79](https://github.com/d-v-b/refkv/issues/79)) ([6e7776e](https://github.com/d-v-b/refkv/commit/6e7776eca756d81b0f381363deb577861c73d16a))
* **python:** build free-threaded wheels for CPython 3.14t and 3.15t ([#174](https://github.com/d-v-b/refkv/issues/174)) ([1f5b7d1](https://github.com/d-v-b/refkv/commit/1f5b7d195bfec9eea21b39df0d979b6a4fc9dd52))
* **reader:** read http(s) archives with refkv's own HTTP client, and make obstore an extra ([#72](https://github.com/d-v-b/refkv/issues/72)) ([a165682](https://github.com/d-v-b/refkv/commit/a16568299290a89577d657d132a0d3aca1eeb87a))
* **tiff:** zarrify images in strips and LZW, as the QPTIFF profile does ([#137](https://github.com/d-v-b/refkv/issues/137)) ([3b4cbd8](https://github.com/d-v-b/refkv/commit/3b4cbd8363055fac4ad4685a5c1f2c2e1dd0ce0e))
* **tiff:** zarrify images with a horizontal predictor through imagecodecs_delta ([#139](https://github.com/d-v-b/refkv/issues/139)) ([242ae27](https://github.com/d-v-b/refkv/commit/242ae274c70da22cda0e9768aa1b17420bda1370))
* **types:** type-check the repository with pyrefly's strict preset ([#116](https://github.com/d-v-b/refkv/issues/116)) ([eefa6bd](https://github.com/d-v-b/refkv/commit/eefa6bd493049ecb3d34b3720ec23107c98f8a16))
* **virtualize:** add an omero block to TIFF and NDPI images, with channel labels and colors ([#95](https://github.com/d-v-b/refkv/issues/95)) ([7d051e3](https://github.com/d-v-b/refkv/commit/7d051e397e25207e79d478787e9840b032f9230d))
* **virtualize:** add the QPTIFF profile, with LZW tiles and strips ([#84](https://github.com/d-v-b/refkv/issues/84)) ([850969d](https://github.com/d-v-b/refkv/commit/850969d09399bae44ae65fec421b5226e0123fa4))
* **virtualize:** sample the omero windows of TIFF and QPTIFF images from the coarsest level ([#98](https://github.com/d-v-b/refkv/issues/98)) ([01c4200](https://github.com/d-v-b/refkv/commit/01c42006ea4d592c70b2a22bad50fca54106ad52))
* **vsi:** zarrify lossless JPEG tiles, and ETS headers that list no dimension sizes ([#162](https://github.com/d-v-b/refkv/issues/162)) ([52304c5](https://github.com/d-v-b/refkv/commit/52304c57bd9bfd9b2a641f6169652b4be2778a8e))
* **zarr:** expose refkv's codecs to zarr through entry points, and rename the clashing ones ([#136](https://github.com/d-v-b/refkv/issues/136)) ([1bdab30](https://github.com/d-v-b/refkv/commit/1bdab3012b137fb64a27dbb0fc393b41c4420ace))
* **zarrify:** add zarrify_async and zarrify_store_async, which keep the event loop free ([#175](https://github.com/d-v-b/refkv/issues/175)) ([ca01733](https://github.com/d-v-b/refkv/commit/ca0173385cd6bb510d40804f8172f653f0a0100f))
* **zarrify:** name an input's profile with a source or store class, and choose its layouts ([#114](https://github.com/d-v-b/refkv/issues/114)) ([db24287](https://github.com/d-v-b/refkv/commit/db2428713af508979eba48060f50a7220be3eedf))
* **zarrify:** pad NIfTI, MRC and FITS edge chunks with one shared zero data source ([#120](https://github.com/d-v-b/refkv/issues/120)) ([b5f0eda](https://github.com/d-v-b/refkv/commit/b5f0eda2a8c7496038cd5bd11c7887b539f9f215))


### Bug fixes

* **demo:** make two-finger scroll step along the data's first non-x/y axis ([#168](https://github.com/d-v-b/refkv/issues/168)) ([2ff14a3](https://github.com/d-v-b/refkv/commit/2ff14a3d46ac70d60909b260088aee7af0f59ee4))
* **demo:** point the N5 example at an OpenOrganelle store that still exists ([#129](https://github.com/d-v-b/refkv/issues/129)) ([5aeb5a3](https://github.com/d-v-b/refkv/commit/5aeb5a39e0234d58a15d1458229bcebfc3513418))
* **js:** say that a failed fetch in a browser may be a server that does not allow cross-origin reads ([#111](https://github.com/d-v-b/refkv/issues/111)) ([4bf3955](https://github.com/d-v-b/refkv/commit/4bf395515bf8602bff0e7e851f4b26c23fb59e5e))
* **mp4:** read a version 0 ctts box's offsets as signed ([#153](https://github.com/d-v-b/refkv/issues/153)) ([7ad0409](https://github.com/d-v-b/refkv/commit/7ad0409681a0ba47779366c1e3f3d13b994705a2))
* **mp4:** reject an avcC SPS whose RBSP is too short for its profile and level ([#133](https://github.com/d-v-b/refkv/issues/133)) ([7d13d85](https://github.com/d-v-b/refkv/commit/7d13d8590c6608f8b1c3a655516aa8aadf25c7bd))
* **mp4:** reject an ftyp box too short for its fields instead of panicking ([#132](https://github.com/d-v-b/refkv/issues/132)) ([513f947](https://github.com/d-v-b/refkv/commit/513f9477e8244c33dd8a7f31e4028fc840b47402))
* **ndpi:** give a strip's frame header its level's size when the two differ ([#101](https://github.com/d-v-b/refkv/issues/101)) ([4a7b754](https://github.com/d-v-b/refkv/commit/4a7b754fd3bc2438b8813f26349b6db275b78bc1))
* **ndpi:** read ASCII values out of line, and add no McuStartsHighBytes to an in-line McuStarts ([#130](https://github.com/d-v-b/refkv/issues/130)) ([2aa5b20](https://github.com/d-v-b/refkv/commit/2aa5b20cbc9ff760f0d2f3a03924b225f17aad0f))
* **nightly:** report inputs their server did not serve as unavailable, and compare mutants as their fixtures ([#159](https://github.com/d-v-b/refkv/issues/159)) ([7a7e572](https://github.com/d-v-b/refkv/commit/7a7e5721d26e7560bedf7c34ac4d9312b1806f79))
* **reader:** send a User-Agent with every range request ([#92](https://github.com/d-v-b/refkv/issues/92)) ([dc35c76](https://github.com/d-v-b/refkv/commit/dc35c7642be8a85b376cd8f819601ea96e93c1e1))
* **sections:** leave CHANGELOG.md's section references as release-please writes them ([#173](https://github.com/d-v-b/refkv/issues/173)) ([af8f923](https://github.com/d-v-b/refkv/commit/af8f92373508ada5ca69a5043e1bc17e909cfc71))
* **uri:** percent-encode a local path when it names a source ([#124](https://github.com/d-v-b/refkv/issues/124)) ([a944426](https://github.com/d-v-b/refkv/commit/a944426cd3ddb37c3c8bfc618ba4edc841fc9a99)), closes [#122](https://github.com/d-v-b/refkv/issues/122)
* **uri:** support Windows paths in file: URIs, and run CI on Linux, macOS and Windows ([#70](https://github.com/d-v-b/refkv/issues/70)) ([8ba6323](https://github.com/d-v-b/refkv/commit/8ba63237bbb8ef4d0f6a94e18874e137f8a80f15))
* **vsi:** probe a stack's frame_t_0.ets only when its frame_t.ets is absent ([#161](https://github.com/d-v-b/refkv/issues/161)) ([6f33355](https://github.com/d-v-b/refkv/commit/6f33355d58301d34ef4b59d00ca5692b83003dac))
* **zarr:** decode zarrified HDF5 files' float attributes, so NWBZarrIO reads a rate of NaN ([#149](https://github.com/d-v-b/refkv/issues/149)) ([782ec27](https://github.com/d-v-b/refkv/commit/782ec273810bad396de76ef0002bb7143959c012))


### Documentation

* add a guide and a project skill for adding a file format ([#155](https://github.com/d-v-b/refkv/issues/155)) ([276ebad](https://github.com/d-v-b/refkv/commit/276ebad70e3f1bddf616f5d4e346d8a1be709ed1))
* **demo:** move the demo site to d-v-b.github.io/refkv-demo ([#164](https://github.com/d-v-b/refkv/issues/164)) ([89beb0f](https://github.com/d-v-b/refkv/commit/89beb0fb8b5f62dce48a536d454fe170776a14f0))
* **design:** center PITCH.md on refkv as the model and vzip as its ZIP binding ([#76](https://github.com/d-v-b/refkv/issues/76)) ([994a08d](https://github.com/d-v-b/refkv/commit/994a08dd9058682592cff8510bfd78413cf4e855))
* **design:** drop the process history from COMPARISON.md and PITCH.md ([#75](https://github.com/d-v-b/refkv/issues/75)) ([0f7b66d](https://github.com/d-v-b/refkv/commit/0f7b66dd6a6d21578b9c28095e2aaf49b0bcc22a))
* **design:** frame vzip as an experiment and point readers to Icechunk and kerchunk ([#74](https://github.com/d-v-b/refkv/issues/74)) ([de1e1c5](https://github.com/d-v-b/refkv/commit/de1e1c5ed4945aa8d9a50f91838813d0fbffba05))
* **design:** measure third-party readers against the Rust parsers, and keep the parsers ([#135](https://github.com/d-v-b/refkv/issues/135)) ([65b5131](https://github.com/d-v-b/refkv/commit/65b513141b88d35873ca949acff9a8abdea4bc0e))
* **design:** plan the FITS profile, a single-file virtualizer with a tile codec ([#88](https://github.com/d-v-b/refkv/issues/88)) ([5b56a47](https://github.com/d-v-b/refkv/commit/5b56a4712c7ce95836ae90643a8448d1f74dd8db))
* give each format a runnable example that zarrifies public data and plots it ([#113](https://github.com/d-v-b/refkv/issues/113)) ([94e473f](https://github.com/d-v-b/refkv/commit/94e473f539564b7186e39bb6b88f4e8fae487a82))
* **readme:** present refkv as the model and vzip as its ZIP binding ([#77](https://github.com/d-v-b/refkv/issues/77)) ([c00546b](https://github.com/d-v-b/refkv/commit/c00546b0668065f3766d319113371d40f7e14a9f))
* **spec:** state the format documents' shared paragraphs once, and check every CMO copy ([#104](https://github.com/d-v-b/refkv/issues/104)) ([44054be](https://github.com/d-v-b/refkv/commit/44054bea66806a411a86b1003784550748f370b4))
* **spec:** tabulate every zarrification profile and reconcile the specs with refkv ([#131](https://github.com/d-v-b/refkv/issues/131)) ([209b0ef](https://github.com/d-v-b/refkv/commit/209b0efcee7b28839bd0f89dbd626a5744fc5adf))


### Refactoring

* say zarrify for virtualize, in the code, the CLI, the spec and the docs ([#112](https://github.com/d-v-b/refkv/issues/112)) ([adb44e9](https://github.com/d-v-b/refkv/commit/adb44e9223b8cc37a50b16a203cfa0692572f6cf))


### Build

* **python:** make python/ a self-contained package versioned from git tags ([#71](https://github.com/d-v-b/refkv/issues/71)) ([1d02f13](https://github.com/d-v-b/refkv/commit/1d02f1332790d9e3c7a55cbb87729a63d0c1f023))
