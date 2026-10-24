//! The DICOM parser (src/dicom/; spec/virtualize/dicom.md): one test per rejected fixture, a
//! long fragment chain built here, and a range asked for and not fed.

mod common;

use common::{fixtures, rebuilt, reject};
use refkv_ir::check;
use refkv_ir::dicom::Dicom;

// one test per rejected fixture, each the message its check gives
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
