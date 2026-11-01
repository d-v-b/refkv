# Changelog

## [0.3.0](https://github.com/d-v-b/refkv/compare/v0.2.0...v0.3.0) (2026-11-01)


### Features

* **demo:** highlight the Python script's syntax with highlight.js ([#176](https://github.com/d-v-b/refkv/issues/176)) ([7a7f8be](https://github.com/d-v-b/refkv/commit/7a7f8bee8e116678d5c0e204cc8e47459cde40f1))
* **demo:** show the result above the examples, and say why a read failed ([#181](https://github.com/d-v-b/refkv/issues/181)) ([20f46c9](https://github.com/d-v-b/refkv/commit/20f46c9580c7515f5a81899f1b4c06855b5a27fc))
* **js:** save the archives the demo's worker zarrifies in Cache Storage ([#180](https://github.com/d-v-b/refkv/issues/180)) ([ba8970e](https://github.com/d-v-b/refkv/commit/ba8970e1e59d4edd3f06bc45d056f954fa7beb67))
* **safetensors:** zarrify safetensors files, each tensor a Zarr array ([#186](https://github.com/d-v-b/refkv/issues/186)) ([743764c](https://github.com/d-v-b/refkv/commit/743764ca74bc6554dc0e74f4f3805e372d07e5ee))


### Bug fixes

* **js:** answer a zarrify that outlasts a minute as pending, so the page asks again ([#178](https://github.com/d-v-b/refkv/issues/178)) ([d2fb777](https://github.com/d-v-b/refkv/commit/d2fb777fa3bccfa8c8fe931a73e29741d76468df))
* **spec:** restore the npy schema's offset description in its source, and check schemas are current ([#187](https://github.com/d-v-b/refkv/issues/187)) ([54d72de](https://github.com/d-v-b/refkv/commit/54d72de6b61c2eadabe889f6590b881760b59514))
* **vzip:** check the source table before VZipWriter.close writes, and fill the API docs' gaps ([#184](https://github.com/d-v-b/refkv/issues/184)) ([04cec13](https://github.com/d-v-b/refkv/commit/04cec13fbb5aade5ff2e1b2661bd1ce948b177ed))


### Documentation

* install refkv from PyPI, and bring the docs up to the release and the worker ([#182](https://github.com/d-v-b/refkv/issues/182)) ([640dcc2](https://github.com/d-v-b/refkv/commit/640dcc26755fb4603b4c729a933e26a06c20603e))
* **readme:** lead with zarrifying a file, and read its result back ([#183](https://github.com/d-v-b/refkv/issues/183)) ([7fffa1c](https://github.com/d-v-b/refkv/commit/7fffa1c2ce4034dc86bc00bf3c6f8916aab647d7))
* **readme:** link the live demo under the introduction ([#179](https://github.com/d-v-b/refkv/issues/179)) ([7fb7973](https://github.com/d-v-b/refkv/commit/7fb79736da870a6b0aad0c74754a444c67cdb27d))

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
