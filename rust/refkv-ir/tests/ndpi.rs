//! The NDPI profile (spec/virtualize/ndpi.md), read by the TIFF parser: one test per rejected
//! fixture.

mod common;

use common::reject;
use refkv_ir::tiff::Tiff;

// one test per rejected fixture, each the message its check gives
reject!(ndpi_edge_reject_focal_planes, Tiff, "ndpi/edge_reject_ndpi_focal_planes.ndpi", "NDPI focal planes (two levels with one magnification) are not supported");
reject!(ndpi_edge_reject_growing, Tiff, "ndpi/edge_reject_ndpi_growing.ndpi", "NDPI levels do not decrease in size");
reject!(ndpi_edge_reject_mcustarts, Tiff, "ndpi/edge_reject_ndpi_mcustarts.ndpi", "McuStarts does not match the strip's intervals");
reject!(ndpi_edge_reject_no_restart_interval, Tiff, "ndpi/edge_reject_ndpi_no_restart_interval.ndpi", "an NDPI strip has no SOF0 or no restart interval");
reject!(ndpi_edge_reject_short_strip, Tiff, "ndpi/edge_reject_ndpi_short_strip.ndpi", "an NDPI strip of 3 bytes is shorter than 4");
reject!(ndpi_edge_reject_wide_interval, Tiff, "ndpi/edge_reject_ndpi_wide_interval.ndpi", "an NDPI chunk would be 65536 pixels wide, more than 65535");
