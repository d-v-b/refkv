//! The SAFE parser in both forms (src/safe/; spec/virtualize/safe.md): every product fixture
//! it accepts is a valid IR whose mirror is canonical, whose view is the one it rebuilds, and
//! that rebuilds its source (a directory's held documents from the mirror alone, a zip file
//! byte for byte from its leaves); its band files are described in place; and one test per
//! rejected fixture (each the role the schema names).

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use vzip_ir::check;
use vzip_ir::ir::{Ir, OBJECT};
use vzip_ir::mirror::{canonical_problem, load, rebuild_store, table_from_out, view_problem};
use vzip_ir::out::Out;
use vzip_ir::run::{output, run_bytes, Format};
use vzip_ir::store::{run_store, store_output, StoreFormat};

fn fixtures() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../fixtures/safe")
}

/// A directory's regular files, keys in byte order.
fn walk(root: &Path) -> BTreeMap<String, Vec<u8>> {
    let mut out = BTreeMap::new();
    let mut stack = vec![root.to_path_buf()];
    while let Some(d) = stack.pop() {
        for e in std::fs::read_dir(&d).unwrap() {
            let e = e.unwrap();
            if e.file_type().unwrap().is_dir() {
                stack.push(e.path());
            } else {
                let k = e.path().strip_prefix(root).unwrap().to_str().unwrap().to_string();
                out.insert(k, std::fs::read(e.path()).unwrap());
            }
        }
    }
    out
}

fn entry(out: &Out) -> impl Fn(&str) -> Option<Vec<u8>> + '_ {
    |k: &str| out.entries.iter().find(|e| e.0 == k).map(|e| e.1.clone())
}

/// The directory form's output (mirrored), its IR, or the rejection.
fn directory(files: &BTreeMap<String, Vec<u8>>) -> Result<(Out, Ir), String> {
    let keys: Vec<(String, u64)> = files.iter().map(|(k, v)| (k.clone(), v.len() as u64)).collect();
    let bytes: Vec<&Vec<u8>> = files.values().collect();
    let (ir, facts, stats) = run_store(StoreFormat::Safe, keys, &|k| bytes[k].clone())?;
    Ok((store_output(StoreFormat::Safe, &ir, &facts, stats, "https://example.org/p/", true)?, ir))
}

/// The zip form's output (mirrored), its IR, or the rejection.
fn zip(data: &[u8]) -> Result<(Out, Ir), String> {
    let (ir, facts, stats) = run_bytes(Format::Safe, data)?;
    Ok((output(Format::Safe, &ir, &facts, stats, "https://example.org/p.SAFE.zip", true)?, ir))
}

fn check_directory(path: &Path, files: &BTreeMap<String, Vec<u8>>, out: &Out, ir: &Ir) {
    check::check(ir).unwrap_or_else(|m| panic!("{path:?}: {m}"));
    let get = entry(out);
    // every held document from the mirror alone, the listing whole
    let rebuilt = rebuild_store(&get).unwrap();
    assert_eq!(rebuilt.len(), files.len(), "{path:?}");
    for (k, n, b) in &rebuilt {
        assert_eq!(*n as usize, files[k].len(), "{path:?} {k}");
        if let Some(b) = b {
            assert!(b == &files[k], "{path:?}: {k} is not rebuilt");
        }
    }
    // the band files are described in place; the XML documents read whole are held
    let back = load(&table_from_out(out).unwrap(), &mut |_, _| Err("no source".into())).unwrap();
    let src = back.sources();
    let jp2 = src.objects.iter().filter(|&&o| back.in_place(o)).count();
    assert!(jp2 > 0 && src.objects.iter().all(|&o| back.kind[o as usize] == OBJECT), "{path:?}");
    let mut read = |v: u64, n: u64| -> Result<Vec<u8>, String> {
        let (o, base) = src.at(&back, v).ok_or("no object at that virtual offset")?;
        let x = v - base;
        if x + n > back.len[o as usize] {
            return Err(format!("[{x}, {}) runs past {:?}", x + n, back.key(o)));
        }
        Ok(files[&back.key(o)][x as usize..(x + n) as usize].to_vec())
    };
    assert_eq!(canonical_problem(&get, &mut read), None, "{path:?}");
    let keys: Vec<String> = out.entries.iter().map(|e| e.0.clone()).collect();
    assert_eq!(view_problem(&keys, &get, &mut read), None, "{path:?}");
}

fn check_zip(path: &Path, data: &[u8], out: &Out, ir: &Ir) {
    check::check(ir).unwrap_or_else(|m| panic!("{path:?}: {m}"));
    let get = entry(out);
    let mut read = |o: u64, n: u64| -> Result<Vec<u8>, String> { Ok(data[o as usize..(o + n) as usize].to_vec()) };
    let back = load(&table_from_out(out).unwrap(), &mut read).unwrap();
    let mut rebuilt = Vec::new();
    for (o, n) in check::leaves(&back).unwrap() {
        rebuilt.extend_from_slice(&data[o as usize..(o + n) as usize]);
    }
    assert!(rebuilt == data, "{path:?} does not rebuild");
    assert_eq!(canonical_problem(&get, &mut read), None, "{path:?}");
    let keys: Vec<String> = out.entries.iter().map(|e| e.0.clone()).collect();
    assert_eq!(view_problem(&keys, &get, &mut read), None, "{path:?}");
}

#[test]
fn accepted_products_are_valid_irs_whose_mirrors_rebuild_them() {
    let (mut dirs, mut zips) = (0, 0);
    let mut paths: Vec<PathBuf> = std::fs::read_dir(fixtures()).unwrap().map(|e| e.unwrap().path()).collect();
    paths.sort();
    for path in paths {
        if path.file_name().unwrap().to_str().unwrap().contains("reject") {
            continue;
        }
        if path.is_dir() {
            let files = walk(&path);
            let (out, ir) = directory(&files).unwrap_or_else(|m| panic!("{path:?}: {m}"));
            check_directory(&path, &files, &out, &ir);
            dirs += 1;
        } else {
            let data = std::fs::read(&path).unwrap();
            let (out, ir) = zip(&data).unwrap_or_else(|m| panic!("{path:?}: {m}"));
            check_zip(&path, &data, &out, &ir);
            zips += 1;
        }
    }
    assert_eq!((dirs, zips), (10, 4));
}

macro_rules! reject {
    ($name:ident, $file:expr, $msg:expr) => {
        #[test]
        fn $name() {
            let path = fixtures().join($file);
            let got = if path.is_dir() {
                directory(&walk(&path)).map(|_| ())
            } else {
                zip(&std::fs::read(&path).unwrap()).map(|_| ())
            };
            match got {
                Err(m) => assert_eq!(m, $msg),
                Ok(_) => panic!("{} was accepted", $file),
            }
        }
    };
}

reject!(safe_reject_bad_band_name, "safe_reject_bad_band_name", "the band name \"B-1\" of \"T32TNS_20240103T101329_B-1\" is not ASCII letters and digits");
reject!(safe_reject_bad_cs_code, "safe_reject_bad_cs_code", "HORIZONTAL_CS_CODE \"WGS84 / UTM 32N\" is not EPSG: and digits");
reject!(safe_reject_box_beyond_file, "safe_reject_box_beyond_file", "a JP2 box reaches beyond the file");
reject!(safe_reject_bytes_after_eoc, "safe_reject_bytes_after_eoc", "a band file's codestream does not end with EOC after its last tile-part");
reject!(safe_reject_duplicate_band_name, "safe_reject_duplicate_band_name", "two band files at 60 m have the band name B01");
reject!(safe_reject_duplicate_resolution, "safe_reject_duplicate_resolution", "two Sizes have the resolution 60");
reject!(safe_reject_eph, "safe_reject_eph", "a band file's COD uses SOP or EPH markers, or precinct anchors (Scod)");
reject!(safe_reject_image_file_form, "safe_reject_image_file_form", "MTD_MSIL1C.xml: IMAGE_FILE 'IMG_DATA/B01' is not GRANULE/<g>/<directory>/<file>");
reject!(safe_reject_infinite_ulx, "safe_reject_infinite_ulx", "Geoposition ULX \"1e999\" is not a finite decimal number");
reject!(safe_reject_jp2c_not_last, "safe_reject_jp2c_not_last", "a band file's jp2c box does not end at the end of the file");
reject!(safe_reject_missing_band_file, "safe_reject_missing_band_file", "the band file GRANULE/L1C_T32TNS_A035655_20240103T101328/IMG_DATA/T32TNS_20240103T101329_B02.jp2 is not in the product");
reject!(safe_reject_mixed_granule_dirs, "safe_reject_mixed_granule_dirs", "MTD_MSIL1C.xml: the image files are in two granule directories");
reject!(safe_reject_mixed_precision, "safe_reject_mixed_precision", "a band file's components differ in precision or sign");
reject!(safe_reject_no_band_file, "safe_reject_no_band_file", "MTD_MSIL1C.xml: no image file is in IMG_DATA");
reject!(safe_reject_no_eoc, "safe_reject_no_eoc", "a band file's tile-part 15 reaches beyond the codestream");
reject!(safe_reject_no_geoposition, "safe_reject_no_geoposition", "Geoposition of resolution 60: 0 elements where there must be one");
reject!(safe_reject_no_image_file, "safe_reject_no_image_file", "MTD_MSIL1C.xml: no granule has an IMAGE_FILE");
reject!(safe_reject_no_manifest, "safe_reject_no_manifest", "a SAFE product has no manifest.safe");
reject!(safe_reject_no_tile_metadata, "safe_reject_no_tile_metadata", "the tile metadata GRANULE/L1C_T32TNS_A035655_20240103T101328/MTD_TL.xml is not in the product");
reject!(safe_reject_not_jp2, "safe_reject_not_jp2", "a band file is not a JP2 file: no JPEG 2000 signature box");
reject!(safe_reject_old_format, "safe_reject_old_format", "not a SAFE product of the compact format: the root has no MTD_MSIL1C.xml or MTD_MSIL2A.xml (products before December 2016 are not supported)");
reject!(safe_reject_ppm, "safe_reject_ppm", "a band file's main header has a PPM marker");
reject!(safe_reject_precision_17, "safe_reject_precision_17", "a band file's components have 17 bits, more than 16");
reject!(safe_reject_psot_zero, "safe_reject_psot_zero", "a band file's tile-part 15 has Psot 0");
reject!(safe_reject_signed, "safe_reject_signed", "a band file's components are signed");
reject!(safe_reject_size_matches_no_resolution, "safe_reject_size_matches_no_resolution", "the band file GRANULE/L1C_T32TNS_A035655_20240103T101328/IMG_DATA/T32TNS_20240103T101329_B01.jp2 (19 × 19) matches no resolution of the tile geocoding");
reject!(safe_reject_size_matches_two_resolutions, "safe_reject_size_matches_two_resolutions", "the band file GRANULE/L1C_T32TNS_A035655_20240103T101328/IMG_DATA/T32TNS_20240103T101329_B01.jp2 (19 × 19) matches several resolution of the tile geocoding");
reject!(safe_reject_sop, "safe_reject_sop", "a band file's COD uses SOP or EPH markers, or precinct anchors (Scod)");
reject!(safe_reject_subsampled, "safe_reject_subsampled", "a band file's components are subsampled");
reject!(safe_reject_tail_too_long, "safe_reject_tail_too_long", "the empty tiles of a chunk of tile 15 would take more than 4096 bytes");
reject!(safe_reject_tails_over_band_size, "safe_reject_tails_over_band_size", "the empty tiles of GRANULE/L1C_T32TNS_A035655_20240103T101328/IMG_DATA/T32TNS_20240103T101329_B01.jp2's chunks would take 4342 bytes, more than the band file's 3116");
reject!(safe_reject_tile_origin, "safe_reject_tile_origin", "a band file's image or tile origin is not 0");
reject!(safe_reject_tile_parts_out_of_order, "safe_reject_tile_parts_out_of_order", "a band file's tile-parts are not in raster order (tile 1 where 0 is)");
reject!(safe_reject_tlm, "safe_reject_tlm", "a band file's main header has a TLM marker");
reject!(safe_reject_two_components, "safe_reject_two_components", "a band file has 2 components, not 1 or 3");
reject!(safe_reject_two_granules, "safe_reject_two_granules", "MTD_MSIL1C.xml: IMAGE_FILE 'GRANULE/L1C_T32TNS_A035655_20240103T101328/IMG_DATA/T32TNS_20240103T101329_B01' is listed twice");
reject!(safe_reject_two_mtd, "safe_reject_two_mtd", "not a SAFE product of the compact format: the root has both MTD_MSIL1C.xml and MTD_MSIL2A.xml");
reject!(safe_reject_two_tile_parts, "safe_reject_two_tile_parts", "a band file's tile 0 has more than one tile-part");
reject!(safe_reject_wrong_root_element, "safe_reject_wrong_root_element", "MTD_MSIL1C.xml's root element is not Level-1C_User_Product");
reject!(safe_reject_xml_doctype, "safe_reject_xml_doctype", "MTD_MSIL1C.xml is not well formed: expected a name at 56");
reject!(safe_reject_xml_not_utf8_metadata, "safe_reject_xml_not_utf8_metadata", "MTD_MSIL1C.xml is not UTF-8");
reject!(safe_reject_xml_too_deep, "safe_reject_xml_too_deep", "MTD_MSIL1C.xml is not well formed: elements nest more than 256 deep at 12649");
reject!(safe_reject_xml_unclosed_tile_metadata, "safe_reject_xml_unclosed_tile_metadata", "MTD_TL.xml is not well formed: unterminated element at 4066");
reject!(safe_reject_zero_xdim, "safe_reject_zero_xdim", "a Geoposition's XDIM or YDIM is 0");
reject!(safe_reject_zip_bad_crc, "safe_reject_zip_bad_crc.SAFE.zip", "a zip entry's CRC-32 does not match its bytes");
reject!(safe_reject_zip_bzip2, "safe_reject_zip_bzip2.SAFE.zip", "the zip entry MTD_MSIL1C.xml has the compression method 12");
reject!(safe_reject_zip_comment_misaligned, "safe_reject_zip_comment_misaligned.SAFE.zip", "not a zip file: no end of central directory record");
reject!(safe_reject_zip_deflated_band, "safe_reject_zip_deflated_band.SAFE.zip", "the zip entry GRANULE/L1C_T32TNS_A035655_20240103T101328/IMG_DATA/T32TNS_20240103T101329_B01.jp2 is compressed: only XML documents may be");
reject!(safe_reject_zip_deflated_mask, "safe_reject_zip_deflated_mask.SAFE.zip", "the zip entry GRANULE/L1C_T32TNS_A035655_20240103T101328/QI_DATA/MSK_CLASSI_B00.jp2 is compressed: only XML documents may be");
reject!(safe_reject_zip_deflated_too_large, "safe_reject_zip_deflated_too_large.SAFE.zip", "the deflated XML document MTD_MSIL1C.xml is larger than 2^26 bytes");
reject!(safe_reject_zip_dotdot, "safe_reject_zip_dotdot.SAFE.zip", "a zip entry's name \"S2B_MSIL1C_20240103T101329_N0510_R022_T32TNS_20240103T110304.SAFE/GRANULE/../x.txt\" has an empty, . or .. segment, or a backslash");
reject!(safe_reject_zip_duplicate_name, "safe_reject_zip_duplicate_name.SAFE.zip", "a zip file has two entries named \"S2B_MSIL1C_20240103T101329_N0510_R022_T32TNS_20240103T110304.SAFE/manifest.safe\"");
reject!(safe_reject_zip_encrypted, "safe_reject_zip_encrypted.SAFE.zip", "a zip entry is encrypted");
reject!(safe_reject_zip_inflated_total, "safe_reject_zip_inflated_total.SAFE.zip", "the deflated XML documents are larger than 2^27 bytes together");
reject!(safe_reject_zip_no_eocd, "safe_reject_zip_no_eocd.SAFE.zip", "not a zip file: no end of central directory record");
reject!(safe_reject_zip_not_safe_root, "safe_reject_zip_not_safe_root.SAFE.zip", "a zip entry \"S2B_MSIL1C_product/\" is not under a .SAFE directory");
reject!(safe_reject_zip_overlapping_entries, "safe_reject_zip_overlapping_entries.SAFE.zip", "two zip entries' local headers and data overlap");
reject!(safe_reject_zip_truncated_cd, "safe_reject_zip_truncated_cd.SAFE.zip", "a zip file's central directory has fewer entries than it says");
reject!(safe_reject_zip_two_roots, "safe_reject_zip_two_roots.SAFE.zip", "a zip file has entries under two root directories");

#[test]
fn a_value_the_view_shows_unread_in_an_object_described_in_place_is_refused() {
    let mut ir = Ir::new_store();
    let o = ir.listing(&[("a".to_string(), 4)]).unwrap()[0];
    ir.describe_in_place(o).unwrap();
    ir.value(o, "v", vzip_ir::ir::NO_INDEX, ">u4", 0, (0, 4), None).unwrap();
    check::finish(&mut ir).unwrap();
    let mut out = Out::new("https://example.org/s/");
    let e = vzip_ir::mirror::mirror(&ir, &mut out, "safe").unwrap_err();
    assert!(e.contains("described in place, was not read"), "{e}");
}

#[test]
fn a_band_file_whose_main_header_passes_64_kib_is_refused_near_the_bound() {
    use vzip_ir::safe::jp2::BandReader;
    // a JP2 signature and file type, then a codestream whose main header after SIZ is COD, QCD
    // and COM segments, 70000 bytes in all, and no SOT
    let mut f = vec![0, 0, 0, 12, b'j', b'P', b' ', b' ', 13, 10, 0x87, 10];
    f.extend_from_slice(&[0, 0, 0, 20, b'f', b't', b'y', b'p', b'j', b'p', b'2', b' ', 0, 0, 0, 0, b'j', b'p', b'2', b' ']);
    f.extend_from_slice(&[0, 0, 0, 0, b'j', b'p', b'2', b'c', 0xFF, 0x4F, 0xFF, 0x51, 0, 41, 0, 0]);
    for v in [16u32, 16, 0, 0, 16, 16, 0, 0] {
        f.extend_from_slice(&v.to_be_bytes());
    }
    f.extend_from_slice(&[0, 1, 14, 1, 1]);
    f.extend_from_slice(&[0xFF, 0x52, 0, 12, 0, 0, 0, 1, 0, 0, 4, 4, 0, 0, 0xFF, 0x5C, 0, 4, 0, 0]);
    while f.len() < 70000 {
        f.extend_from_slice(&[0xFF, 0x64, 0x03, 0xE8]);
        f.extend(std::iter::repeat_n(b'x', 1000 - 2));
    }
    f.extend_from_slice(&[0xFF, 0x90]);
    let mut r = BandReader::new(f.len() as u64);
    let rules = vzip_ir::rules::safe();
    let e = loop {
        match r.step(rules) {
            Ok(Some((o, n))) => r.feed(o, f[o as usize..(o + n) as usize].to_vec()),
            Ok(None) => panic!("accepted"),
            Err(e) => break e,
        }
    };
    assert_eq!(e, "a band file's main header is more than 65536 bytes");
    assert!(r.bytes <= (1 << 16) + (1 << 16), "{} bytes read", r.bytes);
}
