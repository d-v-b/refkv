//! The CZI parser (src/czi.rs; spec/virtualize/czi.md): one test per rejected fixture, and a
//! segment id read as its 16 bytes.

mod common;

use common::{fixtures, reject};
use refkv_ir::czi::Czi;
use refkv_ir::parse_bytes;

// one test per rejected fixture, each the message its check gives
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
