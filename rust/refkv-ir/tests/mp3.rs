//! The MP3 parser (src/mp3.rs; spec/virtualize/mp3.md): one test per rejected fixture.

mod common;

use common::reject;
use refkv_ir::mp3::Mp3;

// one test per rejected fixture, each the message its check gives
reject!(mp3_reject_layer2, Mp3, "mp3/mp3_reject_layer2.mp3", "MPEG-2 Layer II (the frame at 0) is not supported: only Layer III is");
reject!(mp3_reject_layer1, Mp3, "mp3/mp3_reject_layer1.mp3", "MPEG-1 Layer I (the frame at 0) is not supported: only Layer III is");
reject!(mp3_reject_free_format, Mp3, "mp3/mp3_reject_free_format.mp3", "free-format bitrate (the frame header at 0) is not supported");
reject!(mp3_reject_sample_rate_change, Mp3, "mp3/mp3_reject_sample_rate_change.mp3", "the frame at ");
reject!(mp3_reject_channels_change, Mp3, "mp3/mp3_reject_channels_change.mp3", "the frame at ");
reject!(mp3_reject_version_change, Mp3, "mp3/mp3_reject_version_change.mp3", "the frame at ");
reject!(mp3_reject_layer_change, Mp3, "mp3/mp3_reject_layer_change.mp3", "the frame at ");
reject!(mp3_reject_no_frames, Mp3, "mp3/mp3_reject_no_frames.mp3", "no MPEG audio frames");
reject!(mp3_reject_only_id3v1, Mp3, "mp3/mp3_reject_only_id3v1.mp3", "no MPEG audio frames");
reject!(mp3_reject_id3v2_outside, Mp3, "mp3/mp3_reject_id3v2_outside.mp3", "the 100010-byte ID3v2 tag at 0 is outside the 60-byte file");
reject!(mp3_reject_id3v2_syncsafe, Mp3, "mp3/mp3_reject_id3v2_syncsafe.mp3", "the ID3v2 tag at 0 has a size byte that is not syncsafe (128)");
reject!(mp3_reject_short, Mp3, "mp3/mp3_reject_short.mp3", "file too short for an MPEG audio frame header");
