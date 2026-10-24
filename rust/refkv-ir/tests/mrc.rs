//! The MRC parser (src/mrc.rs; spec/virtualize/mrc.md): one test per rejected fixture.

mod common;

use common::reject;
use refkv_ir::mrc::Mrc;

// one test per rejected fixture, each the message its check gives
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
