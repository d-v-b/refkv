//! The NIfTI parser (src/nifti.rs; spec/virtualize/nifti.md): one test per rejected fixture.

mod common;

use common::reject;
use refkv_ir::nifti::Nifti;

// one test per rejected fixture, each the message its check gives
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
