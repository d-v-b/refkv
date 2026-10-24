//! The MP4 parser (src/mp4.rs; spec/virtualize/mp4.md): one test per rejected fixture.

mod common;

use common::reject;
use refkv_ir::mp4::Mp4;

// one test per rejected fixture, each the message its check gives
reject!(mp4_reject_box_outside, Mp4, "mp4/mp4_reject_box_outside.mp4", "the 7481-byte moov box at 32 is outside its parent, which ends at 6388");
reject!(mp4_reject_box_too_small, Mp4, "mp4/mp4_reject_box_too_small.mp4", "the free box at 32 is 4 bytes, less than its 8-byte header");
reject!(mp4_reject_fragmented, Mp4, "mp4/mp4_reject_fragmented.mp4", "fragmented MP4 (the moof box at ");
reject!(mp4_reject_hevc_only, Mp4, "mp4/mp4_reject_hevc_only.mp4", "no track can be virtualized: track 1 (hev1): hev1 video is not supported: only H.264 (avc1) is");
reject!(mp4_reject_moov_twice, Mp4, "mp4/mp4_reject_moov_twice.mp4", "a second moov box at 6388");
reject!(mp4_reject_no_moov, Mp4, "mp4/mp4_reject_no_moov.mp4", "no moov box");
reject!(mp4_reject_sample_outside, Mp4, "mp4/mp4_reject_sample_outside.mp4", "track 1's sample 0 at 6398 (1724 bytes) is outside the 6388-byte file");
reject!(mp4_reject_stts_count, Mp4, "mp4/mp4_reject_stts_count.mp4", "track 1's stts box counts 31 samples, its stsz box 30");
