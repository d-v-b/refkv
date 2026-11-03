//! The JPEG 2000 codestreams of GRIB template 5.40 (WMO-No. 306 FM 92, template 7.40; ITU-T
//! T.800 | ISO/IEC 15444-1): their samples, decoded with hayro-jpeg2000 as OpenJPEG, which
//! eccodes calls, decodes them. One unsigned component of the reversible 5/3 wavelet transform:
//! a lossless codestream, or one truncated at a rate (eccodes' lossy 5.40, octet 22 = 1, keeps
//! the 5/3 transform). The irreversible 9/7 transform is not decoded: hayro computes it in
//! binary32 in another order than OpenJPEG, with a fused multiply-add only where the target has
//! one, and its samples differ from OpenJPEG's by 1 at some points.
//!
//! hayro computes the 5/3 transform in binary32 too, exact while every integer it computes is at
//! most 2^24. For samples of P bits (at most 2^(P−1) in magnitude after the DC level shift), the
//! 5/3 analysis filters cascaded over any number of levels have L1 norms below 8.25, so a
//! lossless codestream's coefficients are under 8.25 × 2^(P−1), and the sums of two neighbors
//! the lifting steps compute under 16.5 × 2^(P−1): within 2^24 for P ≤ 20. A truncated
//! codestream's coefficients are its encoder's to within the bits it drops.

use hayro_jpeg2000::{DecodeSettings, DecoderContext, Image};

type Res<T> = Result<T, String>;

/// The most bits a sample may have.
pub const MAX_PRECISION: u32 = 20;

/// The `n` samples of the JPEG 2000 codestream `b`, which must have `n` samples.
pub fn decode(b: &[u8], n: usize) -> Res<Vec<u64>> {
    let precision = check(b)?;
    let settings = DecodeSettings { resolve_palette_indices: false, strict: false, target_resolution: None };
    let image = Image::new(b, &settings).map_err(|e| format!("the JPEG 2000 codestream does not decode: {e}"))?;
    let mut context = DecoderContext::default();
    let decoded = image.decode(&mut context).map_err(|e| format!("the JPEG 2000 codestream does not decode: {e}"))?;
    let samples = decoded.components()[0].samples();
    if samples.len() != n {
        return Err(format!("the JPEG 2000 image has {} samples, and section 5 gives {n} values", samples.len()));
    }
    // whole numbers (above), clamped to the samples' range as OpenJPEG clamps a truncated
    // codestream's
    let top = ((1u32 << precision) - 1) as f32;
    Ok(samples.iter().map(|&s| s.clamp(0.0, top) as u64).collect())
}

/// The precision of the codestream `b`'s component, once its main and tile-part headers are found
/// to describe what `decode` decodes.
fn check(b: &[u8]) -> Res<u32> {
    if !b.starts_with(&[0xff, 0x4f, 0xff, 0x51]) {
        return Err("section 7 is not a JPEG 2000 codestream".into());
    }
    let short = || "the JPEG 2000 codestream ends in its headers".to_string();
    let u16_at = |at: usize| b.get(at..at + 2).map(|s| u16::from_be_bytes([s[0], s[1]]) as usize).ok_or_else(short);
    // a marker segment at `at`: its marker and body (after its length, which counts itself)
    let segment = |at: usize| -> Res<(usize, &[u8])> {
        let length = u16_at(at + 2)?;
        Ok((u16_at(at)?, b.get(at + 4..at + 2 + length).filter(|_| length >= 2).ok_or_else(short)?))
    };
    // SIZ, after SOC: Rsiz, the sizes and offsets (32 bytes), Csiz, then each component's Ssiz,
    // XRsiz and YRsiz
    let (_, siz) = segment(2)?;
    let Some(&[c0, c1, ssiz, xr, yr]) = siz.get(34..39) else { return Err(short()) };
    let components = u16::from_be_bytes([c0, c1]);
    if components != 1 {
        return Err(format!("a JPEG 2000 codestream of {components} components is not decoded"));
    }
    if ssiz & 0x80 != 0 {
        return Err("a JPEG 2000 codestream of signed samples is not decoded".into());
    }
    if (xr, yr) != (1, 1) {
        return Err("a JPEG 2000 codestream of subsampled samples is not decoded".into());
    }
    let precision = (ssiz & 0x7f) as u32 + 1;
    if precision > MAX_PRECISION {
        return Err(format!("a JPEG 2000 codestream of {precision}-bit samples, more than {MAX_PRECISION}, is not decoded"));
    }
    // the main header's other marker segments, then each tile-part's, from its SOT
    let mut pos = 2 + 4 + siz.len();
    let mut tile_part = None;
    loop {
        let (marker, body) = segment(pos)?;
        let at = |k: usize| body.get(k).copied().ok_or_else(short);
        match marker {
            // COD's and COC's (of one component, so a 1-byte Ccoc) wavelet transform: 0 9/7, 1 5/3
            0xff52 | 0xff53 if at(if marker == 0xff52 { 9 } else { 6 })? != 1 => {
                return Err("a JPEG 2000 codestream of the irreversible 9/7 wavelet transform is not decoded".into());
            }
            // SOT: Isot, then Psot, the tile-part's length from its SOT (0: to the codestream's end)
            0xff90 => tile_part = Some((pos, u32::from_be_bytes([at(2)?, at(3)?, at(4)?, at(5)?]) as usize)),
            _ => {}
        }
        pos += 4 + body.len();
        // SOD: the tile-part's packets follow, to its end, where the next SOT is, or EOC
        if u16_at(pos)? == 0xff93 {
            match tile_part {
                Some((start, psot)) if psot > 0 && u16_at(start + psot).is_ok_and(|m| m == 0xff90) => pos = start + psot,
                _ => return Ok(precision),
            }
        }
    }
}
