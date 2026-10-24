//! The FITS parser (src/fits.rs; spec/virtualize/fits.md): one test per rejected fixture.

mod common;

use common::reject;
use refkv_ir::fits::Fits;

// FITS (spec/virtualize/fits.md §9.3
// <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/fits.md#93-checks>): one per rejected
// fixture, each the message its check gives.
reject!(fits_reject_short, Fits, "fits/fits_reject_short.fits", "file too short for a 2880-byte FITS block");
reject!(fits_reject_simple_false, Fits, "fits/fits_reject_simple_false.fits", "not a FITS file");
reject!(fits_reject_not_fits, Fits, "fits/fits_reject_not_fits.fits", "not a FITS file");
reject!(fits_reject_extension_first, Fits, "fits/fits_reject_extension_first.fits", "not a FITS file");
reject!(fits_reject_gzip_file, Fits, "fits/fits_reject_gzip_file.fits", "not a FITS file");
reject!(fits_reject_bzip2_file, Fits, "fits/fits_reject_bzip2_file.fits", "not a FITS file");
reject!(fits_reject_no_end, Fits, "fits/fits_reject_no_end.fits", "HDU 0: no END card in the 5760 bytes from 0");
reject!(fits_reject_header_nul, Fits, "fits/fits_reject_header_nul.fits", "HDU 0: byte 10 of card 4 is 0x00, not printable ASCII");
reject!(fits_reject_mandatory_order, Fits, "fits/fits_reject_mandatory_order.fits", "HDU 0: card 1 is NAXIS, not BITPIX");
reject!(fits_reject_bitpix, Fits, "fits/fits_reject_bitpix.fits", "HDU 0: BITPIX 12 is not one of 8, 16, 32, 64, -32, -64");
reject!(fits_reject_bitpix_text, Fits, "fits/fits_reject_bitpix_text.fits", "HDU 0: BITPIX 'sixteen' is not an integer");
reject!(fits_reject_naxis_negative, Fits, "fits/fits_reject_naxis_negative.fits", "HDU 0: NAXIS -1 is not in 0..999");
reject!(fits_reject_naxis_1000, Fits, "fits/fits_reject_naxis_1000.fits", "HDU 0: NAXIS 1000 is not in 0..999");
reject!(fits_reject_naxisn_negative, Fits, "fits/fits_reject_naxisn_negative.fits", "HDU 0: NAXIS1 -4 is negative");
reject!(fits_reject_naxisn_missing, Fits, "fits/fits_reject_naxisn_missing.fits", "HDU 0: card 4 is NAXIS3, not NAXIS2");
reject!(fits_reject_pcount_negative, Fits, "fits/fits_reject_pcount_negative.fits", "HDU 1: PCOUNT -1 is negative");
reject!(fits_reject_gcount_zero, Fits, "fits/fits_reject_gcount_zero.fits", "HDU 1: GCOUNT 0 is not positive");
reject!(fits_reject_xtension_unknown, Fits, "fits/fits_reject_xtension_unknown.fits", "HDU 1: XTENSION 'FOREIGN' is not IMAGE, TABLE or BINTABLE");
reject!(fits_reject_groups, Fits, "fits/fits_reject_groups.fits", "HDU 0: random groups (GROUPS = T) are not supported");
reject!(fits_reject_data_outside, Fits, "fits/fits_reject_data_outside.fits", "HDU 0: the 24-byte data at 2880 is outside the 2903-byte file");
reject!(fits_reject_data_overflow, Fits, "fits/fits_reject_data_overflow.fits", "HDU 0: the 32000000000000000000000000000000-byte data at 2880 is outside the 5760-byte file");
reject!(fits_reject_table_bitpix, Fits, "fits/fits_reject_table_bitpix.fits", "HDU 1: a BINTABLE with BITPIX 16, not 8");
reject!(fits_reject_table_naxis, Fits, "fits/fits_reject_table_naxis.fits", "HDU 1: a BINTABLE with NAXIS 1, not 2");
reject!(fits_reject_tfields, Fits, "fits/fits_reject_tfields.fits", "HDU 1: TFIELDS 1000 is not in 0..999");
reject!(fits_reject_tform_missing, Fits, "fits/fits_reject_tform_missing.fits", "HDU 1: no TFORM1 card");
reject!(fits_reject_theap_small, Fits, "fits/fits_reject_theap_small.fits", "HDU 1: THEAP 4 is less than the rows' 8 bytes");
reject!(fits_reject_ztable, Fits, "fits/fits_reject_ztable.fits", "HDU 1: tile-compressed tables (ZTABLE = T) are not supported");
reject!(fits_reject_zcmptype_hcompress, Fits, "fits/fits_reject_zcmptype_hcompress.fits", "HDU 1: ZCMPTYPE 'HCOMPRESS_1' is not supported");
reject!(fits_reject_zcmptype_plio, Fits, "fits/fits_reject_zcmptype_plio.fits", "HDU 1: ZCMPTYPE 'PLIO_1' is not supported");
reject!(fits_reject_zcmptype_unknown, Fits, "fits/fits_reject_zcmptype_unknown.fits", "HDU 1: ZCMPTYPE 'FOO_1' is not RICE_1, GZIP_1, GZIP_2 or NOCOMPRESS");
reject!(fits_reject_zbitpix, Fits, "fits/fits_reject_zbitpix.fits", "HDU 1: ZBITPIX 12 is not one of 8, 16, 32, 64, -32, -64");
reject!(fits_reject_ztile_zero, Fits, "fits/fits_reject_ztile_zero.fits", "HDU 1: ZTILE1 0 is not in 1..20");
reject!(fits_reject_ztile_large, Fits, "fits/fits_reject_ztile_large.fits", "HDU 1: ZTILE2 100 is not in 1..12");
reject!(fits_reject_bytepix8, Fits, "fits/fits_reject_bytepix8.fits", "HDU 1: BYTEPIX 8 is not 1, 2 or 4");
reject!(fits_reject_zquantiz_unknown, Fits, "fits/fits_reject_zquantiz_unknown.fits", "HDU 1: ZQUANTIZ 'DITHER_9' is not NO_DITHER, SUBTRACTIVE_DITHER_1 or SUBTRACTIVE_DITHER_2");
reject!(fits_reject_zdither0_range, Fits, "fits/fits_reject_zdither0_range.fits", "HDU 1: ZDITHER0 20000 is not in 1..10000");
reject!(fits_reject_zdither0_missing, Fits, "fits/fits_reject_zdither0_missing.fits", "HDU 1: no ZDITHER0 card for SUBTRACTIVE_DITHER_2");
reject!(fits_reject_row_count, Fits, "fits/fits_reject_row_count.fits", "HDU 1: 5 rows for 4 tiles");
reject!(fits_reject_columns_extra, Fits, "fits/fits_reject_columns_extra.fits", "HDU 1: column 'FOO' is not a tile-compressed image's");
reject!(fits_reject_uncompressed_data, Fits, "fits/fits_reject_uncompressed_data.fits", "HDU 1: column 'UNCOMPRESSED_DATA' is not supported");
reject!(fits_reject_null_pixel_mask, Fits, "fits/fits_reject_null_pixel_mask.fits", "HDU 1: column 'NULL_PIXEL_MASK' is not supported");
reject!(fits_reject_descriptor_outside, Fits, "fits/fits_reject_descriptor_outside.fits", "HDU 1: tile 3's ");
reject!(fits_reject_descriptor_both, Fits, "fits/fits_reject_descriptor_both.fits", "HDU 1: tile 1 is in both COMPRESSED_DATA and GZIP_COMPRESSED_DATA");
reject!(fits_reject_tiles_out_of_order, Fits, "fits/fits_reject_tiles_out_of_order.fits", "HDU 1: tile 1 at heap offset 0 starts before tile 0 ends");
