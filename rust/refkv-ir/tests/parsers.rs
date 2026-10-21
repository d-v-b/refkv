//! The file parsers (every file profile of the registry, spec/virtualize/profiles.json): every
//! fixture they accept is a valid IR that rebuilds its source, and the fixtures named as rejected
//! are; one test per rejected fixture (each the role the schema names); and the round-3 probes
//! built here (IFD loops, huge counts, too many IFDs).

use std::path::Path;
use refkv_ir::check;
use refkv_ir::czi::Czi;
use refkv_ir::dicom::Dicom;
use refkv_ir::nifti::Nifti;
use refkv_ir::ims::Ims;
use refkv_ir::mrc::Mrc;
use refkv_ir::parse_bytes;
use refkv_ir::run::{run_bytes, Format};
use refkv_ir::tiff::Tiff;

fn fixtures() -> std::path::PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../fixtures")
}

fn rebuilt(ir: &refkv_ir::ir::Ir, data: &[u8]) -> Vec<u8> {
    let mut out = Vec::new();
    for (o, n) in check::leaves(ir).unwrap() {
        out.extend_from_slice(&data[o as usize..(o + n) as usize]);
    }
    out
}

/// The files under `dir` (its subdirectories' too) with one of `suffixes`, sorted.
fn files(dir: &Path, suffixes: &[&str]) -> Vec<std::path::PathBuf> {
    let mut out = Vec::new();
    for e in std::fs::read_dir(dir).unwrap() {
        let path = e.unwrap().path();
        if path.is_dir() {
            out.extend(files(&path, suffixes));
        } else if suffixes.iter().any(|s| path.to_string_lossy().ends_with(s)) {
            out.push(path);
        }
    }
    out.sort();
    out
}

/// Every file profile's fixtures (fixtures/<p>, the files of the registry's suffixes): one
/// named as rejected (`unsupported_*`, `*_reject_*`; conformance/virtualize/HARNESS.md) is
/// rejected, and every other is accepted as a valid IR that rebuilds its source.
#[test]
fn accepted_fixtures_are_valid_irs_that_rebuild() {
    for e in refkv_ir::out::registry().iter().filter(|e| e["kind"] == "file") {
        let name = e["name"].as_str().unwrap();
        let f = Format::from_name(name).unwrap_or_else(|| panic!("{name}: no parser"));
        let suffixes: Vec<&str> = e["suffixes"].as_array().unwrap().iter().map(|s| s.as_str().unwrap()).collect();
        let mut accepted = 0;
        for path in files(&fixtures().join(name), &suffixes) {
            let file = path.file_name().unwrap().to_string_lossy().into_owned();
            let rejected = file.contains("reject") || file.starts_with("unsupported");
            let data = std::fs::read(&path).unwrap();
            match run_bytes(f, &data) {
                Ok((ir, _, _)) => {
                    assert!(!rejected, "{path:?} was accepted");
                    check::check(&ir).unwrap_or_else(|m| panic!("{path:?}: {m}"));
                    assert!(rebuilt(&ir, &data) == data, "{path:?} does not rebuild");
                    accepted += 1;
                }
                Err(m) => assert!(rejected, "{path:?} was rejected: {m}"),
            }
        }
        assert!(accepted > 0, "{name}: no fixture accepted");
    }
}

/// The TIFF parser reads an NDPI as NDPI (spec/virtualize/ndpi.md §4
/// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/ndpi.md#4-ndpi-profile>): each
/// accepted NDPI fixture's facts name the profile, and a TIFF's do not.
#[test]
fn the_tiff_parser_finds_ndpi() {
    for (file, ndpi) in [("ndpi/ndpi_levels.ndpi", true), ("ndpi/ndpi_pointers.ndpi", true), ("tiff/jpeg_gray.tif", false)] {
        let data = std::fs::read(fixtures().join(file)).unwrap();
        let (_, facts) = parse_bytes::<Tiff>(&data).unwrap();
        assert_eq!(facts["profile"] == "ndpi", ndpi, "{file}");
    }
}

macro_rules! reject {
    ($name:ident, $p:ident, $file:expr, $msg:expr) => {
        #[test]
        fn $name() {
            let data = std::fs::read(fixtures().join($file)).unwrap();
            match parse_bytes::<$p>(&data) {
                Err(m) => assert!(m.starts_with($msg), "{m}"),
                Ok(_) => panic!("{} was accepted", $file),
            }
        }
    };
}

reject!(tiff_edge_reject_big_offset, Tiff, "tiff/edge_reject_big_offset.tif", "a value of tag 324 is more than 2^53 - 1");
reject!(tiff_edge_reject_bigtiff_reserved, Tiff, "tiff/edge_reject_bigtiff_reserved.tif", "invalid BigTIFF header");
reject!(tiff_edge_reject_candidate_planar3, Tiff, "tiff/edge_reject_candidate_planar3.tif", "PlanarConfiguration 3");
reject!(tiff_edge_reject_cycle, Tiff, "tiff/edge_reject_cycle.tif", "IFD offset 4136 read twice");
reject!(tiff_edge_reject_dimension_order, Tiff, "tiff/edge_reject_dimension_order.tif", "DimensionOrder 'XYZZT'");
reject!(tiff_edge_reject_fill_order, Tiff, "tiff/edge_reject_fill_order.tif", "the image at 4136 has FillOrder 2");
reject!(tiff_edge_reject_first_z, Tiff, "tiff/edge_reject_first_z.tif", "TiffData starts outside the planes");
reject!(tiff_edge_reject_jpeg_16bit, Tiff, "tiff/edge_reject_jpeg_16bit.tif", "unsupported JPEG: 16-bit, 3 samples, planar 1, p");
reject!(tiff_edge_reject_jpeg_no_photometric, Tiff, "tiff/edge_reject_jpeg_no_photometric.tif", "unsupported JPEG: 8-bit, 3 samples, planar 1, ph");
reject!(tiff_edge_reject_jpeg_planar, Tiff, "tiff/edge_reject_jpeg_planar.tif", "unsupported JPEG: 8-bit, 3 samples, planar 2, ph");
reject!(tiff_edge_reject_jpeg_short_tile, Tiff, "tiff/edge_reject_jpeg_short_tile.tif", "JPEG tile 0 of the IFD at 16 is too short");
reject!(tiff_edge_reject_jpeg_tables, Tiff, "tiff/edge_reject_jpeg_tables.tif", "the IFD at 834 has malformed JPEGTables");
reject!(tiff_edge_reject_multifile, Tiff, "tiff/edge_reject_multifile.tif", "multi-file OME-TIFF is not supported");
reject!(tiff_edge_reject_multifile_uuid, Tiff, "tiff/edge_reject_multifile_uuid.tif", "multi-file OME-TIFF is not supported");
reject!(tiff_edge_reject_nbsp_size, Tiff, "tiff/edge_reject_nbsp_size.tif", "SizeZ=' 2' is not an integer");
reject!(tiff_edge_reject_no_ifds, Tiff, "tiff/edge_reject_no_ifds.tif", "no images");
reject!(tiff_edge_reject_no_width, Tiff, "tiff/edge_reject_no_width.tif", "IFD at 4136 has no tag 256");
reject!(tiff_edge_reject_planar3, Tiff, "tiff/edge_reject_planar3.tif", "PlanarConfiguration 3");
reject!(tiff_edge_reject_plane_limit, Tiff, "tiff/edge_reject_plane_limit.tif", "100001 planes is more than 100000");
reject!(tiff_edge_reject_plane_size, Tiff, "tiff/edge_reject_plane_size.tif", "planes of one pyramid level differ in size, tili");
reject!(tiff_edge_reject_sample_format, Tiff, "tiff/edge_reject_sample_format.tif", "SampleFormat values are missing or differ");
reject!(tiff_edge_reject_scalar_count0, Tiff, "tiff/edge_reject_scalar_count0.tif", "tag 259 has no value");
reject!(tiff_edge_reject_shared_subifd, Tiff, "tiff/edge_reject_shared_subifd.tif", "IFD offset 1032 read twice");
reject!(tiff_edge_reject_signed_width, Tiff, "tiff/edge_reject_signed_width.tif", "tag 256 has field type 8");
reject!(tiff_edge_reject_size_sign, Tiff, "tiff/edge_reject_size_sign.tif", "SizeZ='+2' is not an integer");
reject!(tiff_edge_reject_size_zero, Tiff, "tiff/edge_reject_size_zero.tif", "SizeZ='0' is less than 1");
reject!(tiff_edge_reject_subifd_count, Tiff, "tiff/edge_reject_subifd_count.tif", "the IFD at 9832 has fewer SubIFDs than IFD 0");
reject!(tiff_edge_reject_tiffdata_steps, Tiff, "tiff/edge_reject_tiffdata_steps.tif", "the TiffData elements cover more than 1008 plane");
reject!(tiff_edge_reject_tile_count, Tiff, "tiff/edge_reject_tile_count.tif", "IFD at 32 has 3 tiles, expected 4");
reject!(tiff_edge_reject_tile_outside, Tiff, "tiff/edge_reject_tile_outside.tif", "tile 0 of the IFD at 40 is outside the file");
reject!(tiff_edge_reject_unmapped, Tiff, "tiff/edge_reject_unmapped.tif", "OME-XML planes do not match the TIFF's images");
reject!(tiff_edge_reject_unused_ifd_type, Tiff, "tiff/edge_reject_unused_ifd_type.tif", "tag 259 has field type 99");
reject!(tiff_edge_reject_unused_value_outside, Tiff, "tiff/edge_reject_unused_value_outside.tif", "the value of tag 324 is outside the file");
reject!(tiff_edge_reject_uuid_self_closing, Tiff, "tiff/edge_reject_uuid_self_closing.tif", "multi-file OME-TIFF is not supported");
reject!(tiff_edge_reject_ycbcr_default, Tiff, "tiff/edge_reject_ycbcr_default.tif", "the image at 12334 is YCbCr with subsampling [2,");
reject!(tiff_edge_reject_ycbcr_subsampled, Tiff, "tiff/edge_reject_ycbcr_subsampled.tif", "the image at 12334 is YCbCr with subsampling [2,");
reject!(tiff_edge_reject_zero_tile, Tiff, "tiff/edge_reject_zero_tile.tif", "the image at 8 has an empty tile size");
reject!(tiff_unsupported_lzw, Tiff, "tiff/unsupported_lzw.tif", "unsupported compression 5");
reject!(tiff_unsupported_predictor, Tiff, "tiff/unsupported_predictor.tif", "unsupported predictor 2");
reject!(tiff_unsupported_strips, Tiff, "tiff/unsupported_strips.tif", "only tiled TIFFs are supported; the image at 8 i");
reject!(dicom_reject_bigendian_ow8, Dicom, "dicom/dicom_reject_bigendian_ow8.dcm", "8-bit Pixel Data of VR OW in big endian");
reject!(dicom_reject_bits_allocated, Dicom, "dicom/dicom_reject_bits_allocated.dcm", "unsupported Bits Allocated 12");
reject!(dicom_reject_bot_offset, Dicom, "dicom/dicom_reject_bot_offset.dcm", "a Basic Offset Table offset is not a fragment's");
reject!(dicom_reject_depth, Dicom, "dicom/dicom_reject_depth.dcm", "items nested more than 64 deep");
reject!(dicom_reject_eot_overlap, Dicom, "dicom/dicom_reject_eot_overlap.dcm", "the Extended Offset Table's frames overlap");
reject!(dicom_reject_eot_with_bot, Dicom, "dicom/dicom_reject_eot_with_bot.dcm", "an Extended Offset Table with a Basic Offset Table");
reject!(dicom_reject_focal_planes, Dicom, "dicom/dicom_reject_focal_planes.dcm", "2 focal planes");
reject!(dicom_reject_fragments_without_offsets, Dicom, "dicom/dicom_reject_fragments_without_offsets.dcm", "4 fragments for 2 frames without an offset table");
reject!(dicom_reject_high_bit, Dicom, "dicom/dicom_reject_high_bit.dcm", "High Bit 3 is not Bits Stored - 1");
reject!(dicom_reject_increment_length, Dicom, "dicom/dicom_reject_increment_length.dcm", "(0028,0009) has a length that is not a multiple of 4");
reject!(dicom_reject_jpeg_soi, Dicom, "dicom/dicom_reject_jpeg_soi.dcm", "frame 0's first fragment does not start with a JPEG SOI marker (FF D8)");
reject!(dicom_reject_jpegls, Dicom, "dicom/dicom_reject_jpegls.dcm", "unsupported transfer syntax 1.2.840.10008.1.2.4.80");
reject!(dicom_reject_misplaced_delimiter, Dicom, "dicom/dicom_reject_misplaced_delimiter.dcm", "expected an item at 498, found (FFFE,E00D)");
reject!(dicom_reject_native_ybr, Dicom, "dicom/dicom_reject_native_ybr.dcm", "unsupported Photometric Interpretation 'YBR_FULL' with 3 samples per pixel");
reject!(dicom_reject_no_pixel_data, Dicom, "dicom/dicom_reject_no_pixel_data.dcm", "no Pixel Data element");
reject!(dicom_reject_palette, Dicom, "dicom/dicom_reject_palette.dcm", "unsupported Photometric Interpretation 'PALETTE COLOR' with 1 samples per pixel");
reject!(dicom_reject_pixel_past_eof, Dicom, "dicom/dicom_reject_pixel_past_eof.dcm", "Pixel Data's 104 bytes run past the end of the 514-byte file");
reject!(dicom_reject_rle, Dicom, "dicom/dicom_reject_rle.dcm", "unsupported transfer syntax 1.2.840.10008.1.2.5");
reject!(dicom_reject_rows_vr, Dicom, "dicom/dicom_reject_rows_vr.dcm", "(0028,0010) has VR SS, not US");
reject!(dicom_reject_short_pixel_data, Dicom, "dicom/dicom_reject_short_pixel_data.dcm", "Pixel Data has 32 bytes, less than 3 frames of 16");
reject!(dicom_reject_tiled_sparse, Dicom, "dicom/dicom_reject_tiled_sparse.dcm", "a whole-slide image whose Dimension Organization Type is not TILED_FULL");
reject!(dicom_reject_truncated_sequence, Dicom, "dicom/dicom_reject_truncated_sequence.dcm", "(0008,1150) at 526 runs past its container");
reject!(dicom_reject_unknown_vr, Dicom, "dicom/dicom_reject_unknown_vr.dcm", "unknown VR 'ZZ' at 496");
reject!(dicom_reject_wsi_frames, Dicom, "dicom/dicom_reject_wsi_frames.dcm", "3 frames for 2 by 2 tiles");
reject!(dicom_reject_zero_frames, Dicom, "dicom/dicom_reject_zero_frames.dcm", "Number of Frames 0 is not from 1 to 2^53 - 1");
reject!(nifti_reject_binary, Nifti, "nifti/nifti_reject_binary.nii", "unsupported NIfTI datatype 1 (BINARY)");
reject!(nifti_reject_bitpix, Nifti, "nifti/nifti_reject_bitpix.nii", "bitpix 8 does not match datatype 4");
reject!(nifti_reject_complex64, Nifti, "nifti/nifti_reject_complex64.nii", "unsupported NIfTI datatype 32 (COMPLEX64)");
reject!(nifti_reject_datatype_unknown, Nifti, "nifti/nifti_reject_datatype_unknown.nii", "unknown NIfTI datatype 3");
reject!(nifti_reject_dim0, Nifti, "nifti/nifti_reject_dim0.nii", "dim[0] = 0 is not from 1 to 7");
reject!(nifti_reject_dim0_8, Nifti, "nifti/nifti_reject_dim0_8.nii", "dim[0] = 8 is not from 1 to 7");
reject!(nifti_reject_dim6, Nifti, "nifti/nifti_reject_dim6.nii", "dimensions 6 and 7 must have size 1");
reject!(nifti_reject_dim7, Nifti, "nifti/nifti_reject_dim7.nii", "dimensions 6 and 7 must have size 1");
reject!(nifti_reject_dim_negative, Nifti, "nifti/nifti_reject_dim_negative.nii", "dim[2] = -2 is not from 1 to 2^53 - 1");
reject!(nifti_reject_dim_zero, Nifti, "nifti/nifti_reject_dim_zero.nii", "dim[2] = 0 is not from 1 to 2^53 - 1");
reject!(nifti_reject_float128, Nifti, "nifti/nifti_reject_float128.nii", "unsupported NIfTI datatype 1536 (FLOAT128)");
reject!(nifti_reject_n2_dim_huge, Nifti, "nifti/nifti_reject_n2_dim_huge.nii", "dim[1] = 9007199254740992 is not from 1 to 2^53 - 1");
reject!(nifti_reject_n2_short_header, Nifti, "nifti/nifti_reject_n2_short_header.nii", "file too short for a 540-byte NIfTI-2 header");
reject!(nifti_reject_n2_size_overflow, Nifti, "nifti/nifti_reject_n2_size_overflow.nii", "the 1237940039285380274899124224-byte voxel data at 544 is outside the 556-byte file");
reject!(nifti_reject_n2_vox_offset_low, Nifti, "nifti/nifti_reject_n2_vox_offset_low.nii", "vox_offset 540 is not from 544 to 2^53 - 1");
reject!(nifti_reject_pair_magic, Nifti, "nifti/nifti_reject_pair_magic.nii", "not a NIfTI-1 or NIfTI-2 single file");
reject!(nifti_reject_rgb_5d, Nifti, "nifti/nifti_reject_rgb_5d.nii", "color data with a fifth dimension");
reject!(nifti_reject_truncated, Nifti, "nifti/nifti_reject_truncated.nii", "the 24-byte voxel data at 352 is outside the 375-byte file");
reject!(nifti_reject_vox_offset_fraction, Nifti, "nifti/nifti_reject_vox_offset_fraction.nii", "vox_offset 352.5 is not an integer");
reject!(nifti_reject_vox_offset_in_extender, Nifti, "nifti/nifti_reject_vox_offset_in_extender.nii", "vox_offset 348 is not from 352 to 2^53 - 1");
reject!(nifti_reject_vox_offset_nan, Nifti, "nifti/nifti_reject_vox_offset_nan.nii", "vox_offset nan is not an integer");
reject!(nifti_reject_vox_offset_zero, Nifti, "nifti/nifti_reject_vox_offset_zero.nii", "vox_offset 0 is not from 352 to 2^53 - 1");
reject!(mrc_reject_short_header, Mrc, "mrc/mrc_reject_short_header.mrc", "file too short for a 1024-byte MRC header");
reject!(mrc_reject_machst, Mrc, "mrc/mrc_reject_machst.mrc", "machine stamp 00 00 00 00 is neither 44 (little-endian) nor 11 (big-endian)");
reject!(mrc_reject_machst_swapped, Mrc, "mrc/mrc_reject_machst_swapped.mrc", "machine stamp 41 44 00 00 is neither 44 (little-endian) nor 11 (big-endian)");
reject!(mrc_reject_nx_zero, Mrc, "mrc/mrc_reject_nx_zero.mrc", "nx = 0 is not positive");
reject!(mrc_reject_ny_negative, Mrc, "mrc/mrc_reject_ny_negative.mrc", "ny = -2 is not positive");
reject!(mrc_reject_nz_zero, Mrc, "mrc/mrc_reject_nz_zero.mrc", "nz = 0 is not positive");
reject!(mrc_reject_mode_complex_int16, Mrc, "mrc/mrc_reject_mode_complex_int16.mrc", "unsupported MRC mode 3 (COMPLEX_INT16)");
reject!(mrc_reject_mode_int4, Mrc, "mrc/mrc_reject_mode_int4.mrc", "unsupported MRC mode 101 (INT4)");
reject!(mrc_reject_mode_unknown, Mrc, "mrc/mrc_reject_mode_unknown.mrc", "unknown MRC mode 7");
reject!(mrc_reject_mode_negative, Mrc, "mrc/mrc_reject_mode_negative.mrc", "unknown MRC mode -1");
reject!(mrc_reject_nsymbt_negative, Mrc, "mrc/mrc_reject_nsymbt_negative.mrc", "nsymbt -80 is negative");
reject!(mrc_reject_extended_outside, Mrc, "mrc/mrc_reject_extended_outside.mrc", "the 1000-byte extended header at 1024 is outside the 1072-byte file");
reject!(mrc_reject_volume_stack_mz, Mrc, "mrc/mrc_reject_volume_stack_mz.mrc", "a volume stack (ispg 401) of 5 sections, which mz = 2 does not divide");
reject!(mrc_reject_volume_stack_mz_zero, Mrc, "mrc/mrc_reject_volume_stack_mz_zero.mrc", "a volume stack (ispg 401) of 4 sections, which mz = 0 does not divide");
reject!(mrc_reject_truncated, Mrc, "mrc/mrc_reject_truncated.mrc", "the 48-byte data at 1024 is outside the 1071-byte file");
reject!(mrc_reject_data_overflow, Mrc, "mrc/mrc_reject_data_overflow.mrc", "the 4951760157141521099596496896-byte data at 1024 is outside the 1072-byte file");
reject!(mrc_reject_data_outside, Mrc, "mrc/mrc_reject_data_outside.mrc", "the 48-byte data at 1040 is outside the 1087-byte file");
reject!(ndpi_edge_reject_focal_planes, Tiff, "ndpi/edge_reject_ndpi_focal_planes.ndpi", "NDPI focal planes (two levels with one magnification) are not supported");
reject!(ndpi_edge_reject_growing, Tiff, "ndpi/edge_reject_ndpi_growing.ndpi", "NDPI levels do not decrease in size");
reject!(ndpi_edge_reject_mcustarts, Tiff, "ndpi/edge_reject_ndpi_mcustarts.ndpi", "McuStarts does not match the strip's intervals");
reject!(ndpi_edge_reject_no_restart_interval, Tiff, "ndpi/edge_reject_ndpi_no_restart_interval.ndpi", "an NDPI strip has no SOF0 or no restart interval");
reject!(ndpi_edge_reject_short_strip, Tiff, "ndpi/edge_reject_ndpi_short_strip.ndpi", "an NDPI strip of 3 bytes is shorter than 4");
reject!(ndpi_edge_reject_wide_interval, Tiff, "ndpi/edge_reject_ndpi_wide_interval.ndpi", "an NDPI chunk would be 65536 pixels wide, more than 65535");
reject!(czi_reject_attachment_file_part, Czi, "czi/czi_reject_attachment_file_part.czi", "attachment entry 0 is in FilePart 1");
reject!(czi_reject_attachment_id, Czi, "czi/czi_reject_attachment_id.czi", "attachment 0 at 1440 is not a ZISRAWATTACH segme");
reject!(czi_reject_attachment_outside_file, Czi, "czi/czi_reject_attachment_outside_file.czi", "attachment 0's data is outside the file");
reject!(czi_reject_attdir_count_over_limit, Czi, "czi/czi_reject_attdir_count_over_limit.czi", "the attachment directory's EntryCount 65537 is n");
reject!(czi_reject_attdir_id, Czi, "czi/czi_reject_attdir_id.czi", "the attachment directory at 1760 is not a ZISRAW");
reject!(czi_reject_compression_chunked, Czi, "czi/czi_reject_compression_chunked.czi", "directory entry 0 has an unsupported Compression");
reject!(czi_reject_compression_lzw, Czi, "czi/czi_reject_compression_lzw.czi", "directory entry 0 has an unsupported Compression");
reject!(czi_reject_compression_raw_camera, Czi, "czi/czi_reject_compression_raw_camera.czi", "directory entry 0 has an unsupported Compression");
reject!(czi_reject_de_schema, Czi, "czi/czi_reject_de_schema.czi", "directory entry 0 has schema DE, not DV");
reject!(czi_reject_dimension_count_13, Czi, "czi/czi_reject_dimension_count_13.czi", "directory entry 0 has DimensionCount 13, not 2 t");
reject!(czi_reject_dimension_letter, Czi, "czi/czi_reject_dimension_letter.czi", "directory entry 0 has the dimension 'Q'");
reject!(czi_reject_dimension_lowercase, Czi, "czi/czi_reject_dimension_lowercase.czi", "directory entry 0 has the dimension '[122, 0, 0,");
reject!(czi_reject_dimension_padding, Czi, "czi/czi_reject_dimension_padding.czi", "directory entry 0 has the dimension '[90, 0, 0,");
reject!(czi_reject_directory_id, Czi, "czi/czi_reject_directory_id.czi", "the subblock directory at 2176 is not a ZISRAWDI");
reject!(czi_reject_directory_overrun, Czi, "czi/czi_reject_directory_overrun.czi", "directory entry 1 does not end within the direct");
reject!(czi_reject_directory_used, Czi, "czi/czi_reject_directory_used.czi", "directory entry 0 does not end within the direct");
reject!(czi_reject_duplicate_dimension, Czi, "czi/czi_reject_duplicate_dimension.czi", "directory entry 0 has the dimension C twice");
reject!(czi_reject_entry_count_negative, Czi, "czi/czi_reject_entry_count_negative.czi", "the directory's EntryCount -1 is not from 0 to 2");
reject!(czi_reject_entry_count_over_limit, Czi, "czi/czi_reject_entry_count_over_limit.czi", "the directory's EntryCount 2097153 is not from 0");
reject!(czi_reject_entry_file_part, Czi, "czi/czi_reject_entry_file_part.czi", "directory entry 0 is in FilePart 1");
reject!(czi_reject_extent, Czi, "czi/czi_reject_extent.czi", "an array dimension of 4294967296, more than 2^31");
reject!(czi_reject_file_part, Czi, "czi/czi_reject_file_part.czi", "FilePart 1: a CZI split over several files is no");
reject!(czi_reject_jpeg_gray16, Czi, "czi/czi_reject_jpeg_gray16.czi", "directory entry 0: Compression 1 with PixelType");
reject!(czi_reject_major_2, Czi, "czi/czi_reject_major_2.czi", "CZI file version 2.0 (Major MUST be 1)");
reject!(czi_reject_metadata_id, Czi, "czi/czi_reject_metadata_id.czi", "the metadata segment at 864 is not a ZISRAWMETAD");
reject!(czi_reject_metadata_negative, Czi, "czi/czi_reject_metadata_negative.czi", "the metadata segment has a negative XmlSize or A");
reject!(czi_reject_metadata_outside_file, Czi, "czi/czi_reject_metadata_outside_file.czi", "the metadata segment's parts is outside the file");
reject!(czi_reject_missing_x, Czi, "czi/czi_reject_missing_x.czi", "directory entry 0 lacks the X or Y dimension");
reject!(czi_reject_negative_sizes, Czi, "czi/czi_reject_negative_sizes.czi", "subblock 0 has a negative MetadataSize, Attachme");
reject!(czi_reject_no_directory, Czi, "czi/czi_reject_no_directory.czi", "the subblock directory at 0 is not a ZISRAWDIREC");
reject!(czi_reject_pixel_type_unknown, Czi, "czi/czi_reject_pixel_type_unknown.czi", "directory entry 0 has an unsupported PixelType 5");
reject!(czi_reject_plane_size_not_1, Czi, "czi/czi_reject_plane_size_not_1.czi", "directory entry 0's C Size and StoredSize must b");
reject!(czi_reject_segment_negative, Czi, "czi/czi_reject_segment_negative.czi", "the subblock directory's AllocatedSize -1 is not");
reject!(czi_reject_subblock_copy_count, Czi, "czi/czi_reject_subblock_copy_count.czi", "subblock 0's copy of its entry has DimensionCoun");
reject!(czi_reject_subblock_copy_schema, Czi, "czi/czi_reject_subblock_copy_schema.czi", "subblock 0's copy of its entry has schema DE, no");
reject!(czi_reject_subblock_id, Czi, "czi/czi_reject_subblock_id.czi", "subblock 0 at 544 is not a ZISRAWSUBBLOCK segmen");
reject!(czi_reject_subblock_outside_file, Czi, "czi/czi_reject_subblock_outside_file.czi", "subblock 0's parts is outside the file");
reject!(czi_reject_unknown_schema, Czi, "czi/czi_reject_unknown_schema.czi", "directory entry 0 has schema XX, not DV");
reject!(czi_reject_zero_size, Czi, "czi/czi_reject_zero_size.czi", "directory entry 0's X Size and StoredSize must b");
reject!(ims_reject_chunk_mismatch, Ims, "ims/ims_reject_chunk_mismatch.ims", "the datasets of level 0 differ in size, chunk shape or filters");
reject!(ims_reject_contiguous, Ims, "ims/ims_reject_contiguous.ims", "dataset layout class 1 is not chunked");
reject!(ims_reject_dtype_mismatch, Ims, "ims/ims_reject_dtype_mismatch.ims", "the datasets have different data types");
reject!(ims_reject_fill_value, Ims, "ims/ims_reject_fill_value.ims", "the fill value is not zero");
reject!(ims_reject_fletcher32, Ims, "ims/ims_reject_fletcher32.ims", "unsupported HDF5 filters [3]");
reject!(ims_reject_float64, Ims, "ims/ims_reject_float64.ims", "unsupported HDF5 datatype (class 1, 8 bytes)");
reject!(ims_reject_image_larger, Ims, "ims/ims_reject_image_larger.ims", "the image of level 0 is larger than its dataset");
reject!(ims_reject_imagesize_text, Ims, "ims/ims_reject_imagesize_text.ims", "ImageSizeX of level 0 is not a positive integer");
reject!(ims_reject_imagesize_vlen, Ims, "ims/ims_reject_imagesize_vlen.ims", "attribute ImageSizeX is not a string");
reject!(ims_reject_missing_channel, Ims, "ims/ims_reject_missing_channel.ims", "no Channel 1");
reject!(ims_reject_not_imaris, Ims, "ims/ims_reject_not_imaris.ims", "not an Imaris file (no DataSet group)");
reject!(ims_reject_offset_size, Ims, "ims/ims_reject_offset_size.ims", "HDF5 offsets and lengths of 4 and 8 bytes are not supported");
reject!(ims_reject_rank, Ims, "ims/ims_reject_rank.ims", "a dataset of level 0 is not 3-dimensional");
reject!(ims_reject_raw_edges_image, Ims, "ims/ims_reject_raw_edges_image.ims", "a chunk's filter mask is not 0");
reject!(ims_reject_relative_soft_link, Ims, "ims/ims_reject_relative_soft_link.ims", "soft link \"Data\" to a relative path");
reject!(ims_reject_shuffle, Ims, "ims/ims_reject_shuffle.ims", "unsupported HDF5 filters [2, 1]");
reject!(ims_reject_size_mismatch, Ims, "ims/ims_reject_size_mismatch.ims", "the datasets of level 0 differ in size, chunk shape or filters");
reject!(ims_reject_z_levels, Ims, "ims/ims_reject_z_levels.ims", "a level has more than one z plane, and level 0 has one");

// ---- the round-3 TIFF probes, built here

/// A little-endian classic TIFF: `blobs` after the header, then the IFDs (sorted
/// entries (tag, type, count, value)), each IFD's next given by `next` (an index).
fn tiff(ifds: &[Vec<(u16, u16, u32, u32)>], blobs: &[u8], next: &[Option<usize>]) -> Vec<u8> {
    let start = 8 + blobs.len();
    let mut offs = Vec::new();
    let mut at = start;
    for e in ifds {
        offs.push(at as u32);
        at += 2 + 12 * e.len() + 4;
    }
    let mut out = b"II*\0".to_vec();
    out.extend_from_slice(&offs[0].to_le_bytes());
    out.extend_from_slice(blobs);
    for (k, e) in ifds.iter().enumerate() {
        let mut e = e.clone();
        e.sort();
        out.extend_from_slice(&(e.len() as u16).to_le_bytes());
        for (t, ty, c, v) in e {
            out.extend_from_slice(&t.to_le_bytes());
            out.extend_from_slice(&ty.to_le_bytes());
            out.extend_from_slice(&c.to_le_bytes());
            out.extend_from_slice(&v.to_le_bytes());
        }
        out.extend_from_slice(&next[k].map(|j| offs[j]).unwrap_or(0).to_le_bytes());
    }
    out
}

/// A tiled 32 x 32 8-bit image's entries and its four tiles and tables.
fn image() -> (Vec<(u16, u16, u32, u32)>, Vec<u8>) {
    let mut blobs = vec![0u8; 1024];
    let at = 8 + blobs.len() as u32;
    for k in 0..4u32 {
        blobs.extend_from_slice(&(8 + 256 * k).to_le_bytes());
    }
    for _ in 0..4 {
        blobs.extend_from_slice(&256u32.to_le_bytes());
    }
    let e = vec![
        (256, 4, 1, 32), (257, 4, 1, 32), (258, 3, 1, 8), (259, 3, 1, 1), (262, 3, 1, 1), (277, 3, 1, 1),
        (322, 3, 1, 16), (323, 3, 1, 16), (324, 4, 4, at), (325, 4, 4, at + 16),
    ];
    (e, blobs)
}

fn rejects(data: &[u8], msg: &str) {
    match parse_bytes::<Tiff>(data) {
        Err(m) => assert!(m.contains(msg), "{m}"),
        Ok(_) => panic!("accepted"),
    }
}

#[test]
fn tiff_probes_parse() {
    let (e, blobs) = image();
    // one IFD, then IFD 0 with an EXIF pointer to a second IFD that points back to both: aliases
    let first = 8 + blobs.len() as u32;
    let second = first + 2 + 12 * 11 + 4;
    let mut with_exif = e.clone();
    with_exif.push((34665, 4, 1, second));
    let cases = [
        tiff(&[e.clone()], &blobs, &[None]),
        tiff(&[with_exif, vec![(34665, 4, 1, first), (34853, 4, 1, second)]], &blobs, &[None, None]),
        // a tag the profile does not read, with a count whose value would be 16 GiB: not read
        tiff(&[[e.clone(), vec![(65000, 7, u32::MAX, 64)]].concat()], &blobs, &[None]),
    ];
    for data in cases {
        let (ir, facts) = parse_bytes::<Tiff>(&data).unwrap();
        check::check(&ir).unwrap();
        assert_eq!(rebuilt(&ir, &data), data);
        assert_eq!(facts["levels"][0]["w"], 32);
        assert!(facts["requested"].as_u64().unwrap() < 4 * data.len() as u64);
    }
}

#[test]
fn a_chain_that_loops_is_rejected() {
    let (e, blobs) = image();
    rejects(&tiff(&[e.clone(), e], &blobs, &[Some(1), Some(0)]), "read twice");
}

#[test]
fn an_ifd_that_is_its_own_next_is_rejected() {
    let (e, blobs) = image();
    rejects(&tiff(&[e], &blobs, &[Some(0)]), "read twice");
}

#[test]
fn a_subifd_that_is_its_parent_is_rejected() {
    let (mut e, blobs) = image();
    e.push((330, 4, 1, 8 + blobs.len() as u32));
    rejects(&tiff(&[e], &blobs, &[None]), "read twice");
}

#[test]
fn a_bigtiff_entry_count_past_the_file_is_rejected() {
    let mut data = b"II+\0".to_vec();
    data.extend_from_slice(&8u16.to_le_bytes());
    data.extend_from_slice(&0u16.to_le_bytes());
    data.extend_from_slice(&16u64.to_le_bytes());
    data.extend_from_slice(&(1u64 << 40).to_le_bytes());
    data.extend_from_slice(&[0; 64]);
    rejects(&data, "not within the file");
}

#[test]
fn a_tile_table_count_past_the_file_is_rejected() {
    let (mut e, blobs) = image();
    e[8] = (324, 4, u32::MAX, 64);
    rejects(&tiff(&[e], &blobs, &[None]), "the value of tag 324 is outside the file");
}

#[test]
fn more_than_100000_ifds_are_rejected() {
    let ifds = vec![vec![(256u16, 4u16, 1u32, 1u32)]; 100001];
    let next: Vec<Option<usize>> = (0..ifds.len()).map(|k| (k + 1 < ifds.len()).then_some(k + 1)).collect();
    rejects(&tiff(&ifds, &[], &next), "too many IFDs");
}

#[test]
fn a_segment_id_with_bytes_after_its_nul_is_not_the_id() {
    // spec/virtualize/czi.md §2.1
    // <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/czi.md#21-segments>: the 16 bytes of
    // the id, NUL-padded; a byte after the NUL makes another id
    let mut data = std::fs::read(fixtures().join("czi/czi_gray8_single.czi")).unwrap();
    let at = (0..data.len() - 16).find(|&k| data[k..k + 15] == *b"ZISRAWSUBBLOCK\0").unwrap();
    data[at + 15] = 0xDF;
    match parse_bytes::<Czi>(&data) {
        Err(m) => assert!(m.contains("is not a ZISRAWSUBBLOCK segment"), "{m}"),
        Ok(_) => panic!("accepted"),
    }
}

/// Runs the IMS parser over bytes in memory, the walk's room in elements set to `room`.
fn ims_with_room(data: &[u8], room: Option<u64>) -> (refkv_ir::ir::Ir, serde_json::Value) {
    let mut p = Ims::new(data.len() as u64);
    p.room = room;
    loop {
        match p.step().unwrap() {
            refkv_ir::nd2::Step::Read(r) => {
                for (o, n) in r {
                    p.feed(o, data[o as usize..(o + n) as usize].to_vec());
                }
            }
            refkv_ir::nd2::Step::Done => break,
        }
    }
    p.finish().unwrap()
}

#[test]
fn past_the_walks_room_the_image_is_described_and_the_rest_is_gaps() {
    let project = |ir: &refkv_ir::ir::Ir, facts: &serde_json::Value| {
        let out = refkv_ir::project::project(refkv_ir::run::Format::Ims, ir, facts, "u", &Default::default()).unwrap();
        out.refs.iter().map(|(k, p)| format!("{k}{p:?}")).collect::<Vec<_>>()
    };
    // dense links and attributes (fractal heaps, version 2 B-trees), huge attributes, and
    // variable-length attributes (global heap collections)
    for name in ["ims_latest_many_channels", "ims_latest_dense_links", "ims_latest_huge_attribute", "ims_latest_review2",
                 "ims_earliest_review2"] {
        let data = std::fs::read(fixtures().join(format!("ims/{name}.ims"))).unwrap();
        let (all, facts) = ims_with_room(&data, None);
        let n = all.len() as u64;
        for room in [0, n / 8, n / 4, n / 2, 3 * n / 4] {
            let (ir, f) = ims_with_room(&data, Some(room));
            check::check(&ir).unwrap();
            assert!(rebuilt(&ir, &data) == data);
            assert!(ir.len() < all.len(), "{name} room {room}");
            assert_eq!(project(&ir, &f), project(&all, &facts), "{name} room {room}");
            // the elements outside the image's datasets and the objects' structs: at most the
            // room, and a few more (each loop stops at the room, within one structure)
            let paths: Vec<String> = (0..ir.len() as u32).filter(|&i| ir.kind[i as usize] != refkv_ir::ir::GAP).map(|i| ir.path(i)).collect();
            let images: std::collections::HashSet<String> =
                paths.iter().filter_map(|p| p.strip_suffix("/chunks")).map(|p| p.to_string()).collect();
            let outside = |p: &String| {
                let parts: Vec<&str> = p.splitn(3, '/').collect();
                !(parts[0] == "objects" && parts.len() >= 2 && (parts.len() == 2 || images.contains(&format!("objects/{}", parts[1]))))
            };
            let rest: Vec<&String> = paths.iter().filter(|p| outside(p)).collect();
            assert!(rest.len() <= room as usize + 30, "{name} room {room}: {} elements outside the image", rest.len());
            if room == 0 {
                // nothing of a non-image object but its struct
                assert!(rest.iter().all(|p| !p.starts_with("objects/")), "{name}: {rest:?}");
            }
        }
    }
}

#[test]
fn the_walk_stops_inside_heaps_b_trees_and_global_heaps() {
    // (fixture, a structure whose children the room cuts: its path's start, and a child's)
    for (name, under) in [
        ("ims_latest_review2", "global_heaps/0/objects/"),
        ("ims_latest_dense_links", "/links/heap/blocks/"),
        ("ims_latest_dense_links", "/links/index/nodes/"),
        ("ims_latest_review2", "/attributes/heap/blocks/"),
    ] {
        let data = std::fs::read(fixtures().join(format!("ims/{name}.ims"))).unwrap();
        let (all, _) = ims_with_room(&data, None);
        let paths = |ir: &refkv_ir::ir::Ir| (0..ir.len() as u32).map(|i| ir.path(i)).collect::<Vec<_>>();
        let full = paths(&all);
        // the room is spent right after the structure's first child: the walk is in it
        let first = full.iter().position(|p| p.contains(under)).unwrap_or_else(|| panic!("{name}: no {under}"));
        let parent = &full[first][..full[first].find(under).unwrap() + under.len()];
        let total = full.iter().filter(|p| p.starts_with(parent)).count();
        assert!(total > 1, "{name}: {parent} has one child");
        let (ir, _) = ims_with_room(&data, Some(first as u64 + 1));
        check::check(&ir).unwrap();
        assert!(rebuilt(&ir, &data) == data);
        let kept = paths(&ir).iter().filter(|p| p.starts_with(parent)).count();
        assert!(kept >= 1 && kept < total, "{name}: {kept} of {total} under {parent}");
    }
    // the room spent right after the first collection: no later collection is described
    let data = std::fs::read(fixtures().join("ims/ims_latest_global_heaps.ims")).unwrap();
    let (all, _) = ims_with_room(&data, None);
    let full: Vec<String> = (0..all.len() as u32).map(|i| all.path(i)).collect();
    assert!(full.iter().any(|p| p.starts_with("global_heaps/2")), "fewer than three collections");
    let last = full.iter().rposition(|p| p.starts_with("global_heaps/0")).unwrap();
    let (ir, _) = ims_with_room(&data, Some(last as u64 + 1));
    check::check(&ir).unwrap();
    assert!(rebuilt(&ir, &data) == data);
    let paths: Vec<String> = (0..ir.len() as u32).map(|i| ir.path(i)).collect();
    for p in full.iter().filter(|p| p.starts_with("global_heaps/0")) {
        assert!(paths.contains(p), "{p} of collection 0 is not described");
    }
    assert!(!paths.iter().any(|p| p.starts_with("global_heaps/1")), "a later collection is described");
}

/// A DICOM file in explicit VR little endian: `body` after the File Meta Information.
fn dicom(syntax: &str, body: &[u8]) -> Vec<u8> {
    let mut uid = syntax.as_bytes().to_vec();
    if uid.len() % 2 == 1 {
        uid.push(0);
    }
    let mut out = vec![0u8; 128];
    out.extend_from_slice(b"DICM");
    out.extend(short(2, 0x10, b"UI", &uid));
    out.extend_from_slice(body);
    out
}

/// One element of a short VR in explicit VR little endian.
fn short(g: u16, e: u16, vr: &[u8; 2], v: &[u8]) -> Vec<u8> {
    let mut b = Vec::new();
    for x in [g, e] {
        b.extend_from_slice(&x.to_le_bytes());
    }
    b.extend_from_slice(vr);
    b.extend_from_slice(&(v.len() as u16).to_le_bytes());
    b.extend_from_slice(v);
    b
}

#[test]
fn a_long_fragment_chain_is_walked_to_its_end() {
    // 600 frames of 70,000 bytes, without an offset table: each fragment's item header is
    // past the window that read the one before, so a round each
    let n = 600u32;
    let mut body = Vec::new();
    for (e, v) in [(2u16, 1u16), (0x10, 1), (0x11, 1), (0x100, 8), (0x101, 8), (0x103, 0)] {
        body.extend(short(0x28, e, b"US", &v.to_le_bytes()));
    }
    body.extend(short(0x28, 4, b"CS", b"MONOCHROME2 "));
    body.extend(short(0x28, 8, b"IS", n.to_string().as_bytes()));
    body.extend_from_slice(&[0xE0, 0x7F, 0x10, 0x00, b'O', b'B', 0, 0, 0xFF, 0xFF, 0xFF, 0xFF]);
    body.extend_from_slice(&[0xFE, 0xFF, 0x00, 0xE0, 0, 0, 0, 0]);
    for _ in 0..n {
        body.extend_from_slice(&[0xFE, 0xFF, 0x00, 0xE0]);
        body.extend_from_slice(&70_000u32.to_le_bytes());
        body.extend(std::iter::repeat_n(0u8, 70_000));
    }
    body.extend_from_slice(&[0xFE, 0xFF, 0xDD, 0xE0, 0, 0, 0, 0]);
    let data = dicom("1.2.840.10008.1.2.4.90", &body);
    let (ir, facts, _) = refkv_ir::run::run_bytes(refkv_ir::run::Format::Dicom, &data).unwrap();
    check::check(&ir).unwrap();
    assert!(rebuilt(&ir, &data) == data);
    assert!(facts["rounds"].as_u64().unwrap() > n as u64);
}

#[test]
fn rejects_a_dicom_range_asked_for_and_not_fed() {
    use refkv_ir::nd2::Step;
    let err = |r: Result<Step, String>| match r {
        Err(m) => m,
        Ok(_) => panic!("stepped on"),
    };
    let data = std::fs::read(fixtures().join("dicom/dicom_implicit_mono16.dcm")).unwrap();
    // fed short: one byte less than asked
    let mut p = Dicom::new(data.len() as u64);
    let Step::Read(r) = p.step().unwrap() else { panic!("no batch") };
    let (o, n) = r[0];
    p.feed(o, data[o as usize..(o + n - 1) as usize].to_vec());
    assert_eq!(err(p.step()), format!("internal: the range ({o}, {n}) was asked for and not fed"));
    // not fed at all
    let mut p = Dicom::new(data.len() as u64);
    assert!(p.step().is_ok());
    assert!(err(p.step()).ends_with("was asked for and not fed"));
}
