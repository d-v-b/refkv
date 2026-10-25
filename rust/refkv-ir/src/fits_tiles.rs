//! The `refkv.fits_tiles` codec (spec/zarrify/fits.md §3.3
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#33-tile-compressed-images>):
//! decodes a chunk of a tile-compressed FITS image, whose bytes are a prefix describing each
//! tile (its stream's length and kind, its quantization) followed by the tiles' streams, into
//! the chunk's pixels. The tiles are decoded as cfitsio decodes them: RICE_1 is a port of
//! cfitsio's `fits_rdecomp`, `fits_rdecomp_short` and `fits_rdecomp_byte` (ricecomp.c), the
//! dequantization and its dither of `unquantize_i4r4` and `fits_init_randoms` (unquantize.c),
//! bit for bit. One implementation serves Python and the browser.

use crate::fed::Word;
use crate::fits::tile_at;
use serde_json::Value as J;
use std::sync::OnceLock;

type Res<T> = Result<T, String>;

/// The codec's configuration (the array's `codecs` entry).
#[derive(Clone, Debug, PartialEq)]
pub struct Config {
    /// `RICE_1`, `GZIP_1`, `GZIP_2` or `NOCOMPRESS`
    pub compression: String,
    pub blocksize: u32,
    pub bytepix: u32,
    pub zbitpix: i32,
    /// the image's shape and tile shape, in the array's order
    pub shape: Vec<u64>,
    pub tile: Vec<u64>,
    /// 0 none, 1 `SUBTRACTIVE_DITHER_1`, 2 `SUBTRACTIVE_DITHER_2`
    pub quantize: u8,
    pub dither_seed: i64,
}

impl Config {
    pub fn from_json(v: &J) -> Res<Config> {
        let list = |k: &str| -> Res<Vec<u64>> {
            v[k].as_array().ok_or(format!("fits_tiles: no {k}"))?.iter().map(|x| x.as_u64().ok_or(format!("fits_tiles: {k} is not a list of sizes"))).collect()
        };
        let p = &v["parameters"];
        let quantize = match v["quantize"].as_str().unwrap_or("NO_DITHER") {
            "SUBTRACTIVE_DITHER_1" => 1,
            "SUBTRACTIVE_DITHER_2" => 2,
            _ => 0,
        };
        Ok(Config {
            compression: v["compression"].as_str().ok_or("fits_tiles: no compression")?.to_string(),
            blocksize: p["BLOCKSIZE"].as_u64().unwrap_or(32) as u32,
            bytepix: p["BYTEPIX"].as_u64().unwrap_or(4) as u32,
            zbitpix: v["zbitpix"].as_i64().ok_or("fits_tiles: no zbitpix")? as i32,
            shape: list("shape")?,
            tile: list("tile_shape")?,
            quantize,
            dither_seed: v["dither_seed"].as_i64().unwrap_or(0),
        })
    }
}

/// A tile's record in the chunk's prefix (32 bytes, little-endian).
#[derive(Clone, Copy, Debug, Default)]
pub struct TileRecord {
    pub length: u32,
    /// 0 a `ZCMPTYPE` stream, 1 a gzip member of the tile's raw values (the fallback)
    pub kind: u32,
    pub zblank: i32,
    /// bit 0: zscale and zzero given; bit 1: zblank given
    pub flags: u32,
    pub zscale: f64,
    pub zzero: f64,
}

pub const PREFIX_HEADER: usize = 8;
pub const RECORD: usize = 32;

/// The prefix of a chunk: the tile count, the first tile's storage index, the records.
pub fn prefix(first: u64, records: &[TileRecord]) -> Vec<u8> {
    let mut out = Vec::with_capacity(PREFIX_HEADER + RECORD * records.len());
    out.extend_from_slice(&(records.len() as u32).to_le_bytes());
    out.extend_from_slice(&(first as u32).to_le_bytes());
    for r in records {
        out.extend_from_slice(&r.length.to_le_bytes());
        out.extend_from_slice(&r.kind.to_le_bytes());
        out.extend_from_slice(&r.zblank.to_le_bytes());
        out.extend_from_slice(&r.flags.to_le_bytes());
        out.extend_from_slice(&r.zscale.to_le_bytes());
        out.extend_from_slice(&r.zzero.to_le_bytes());
    }
    out
}

fn parse_prefix(chunk: &[u8]) -> Res<(u64, Vec<TileRecord>, usize)> {
    if chunk.len() < PREFIX_HEADER {
        return Err("fits_tiles: the chunk is shorter than its prefix".into());
    }
    let n = u32::le_at(chunk, 0) as usize;
    let first = u32::le_at(chunk, 4) as u64;
    let end = PREFIX_HEADER + n * RECORD;
    if chunk.len() < end {
        return Err("fits_tiles: the chunk is shorter than its prefix".into());
    }
    let mut records = Vec::with_capacity(n);
    for k in 0..n {
        let r = &chunk[PREFIX_HEADER + k * RECORD..PREFIX_HEADER + (k + 1) * RECORD];
        let u = |i: usize| u32::le_at(r, i);
        records.push(TileRecord {
            length: u(0),
            kind: u(4),
            zblank: u(8) as i32,
            flags: u(12),
            zscale: f64::le_at(r, 16),
            zzero: f64::le_at(r, 24),
        });
    }
    Ok((first, records, end))
}

// ---- RICE_1 (cfitsio ricecomp.c, the decoders)

/// The RICE_1 stream of `npix` pixels of `bytepix` bytes, coded in blocks of `blocksize`:
/// the pixels as 32-bit values (a 2-byte pixel's low 16 bits, a byte's low 8).
pub fn rice_decode(c: &[u8], npix: usize, blocksize: u32, bytepix: u32) -> Res<Vec<u32>> {
    let (fsbits, fsmax, mask): (i32, i32, u32) = match bytepix {
        1 => (3, 6, 0xff),
        2 => (4, 14, 0xffff),
        4 => (5, 25, 0xffff_ffff),
        _ => return Err(format!("fits_tiles: RICE_1 with BYTEPIX {bytepix}")),
    };
    let bbits: i32 = 1 << fsbits;
    let bp = bytepix as usize;
    if c.len() < bp + 1 {
        return Err("fits_tiles: a RICE_1 stream shorter than its first pixel".into());
    }
    let mut lastpix: u32 = 0;
    for &byte in &c[..bp] {
        lastpix = (lastpix << 8) | byte as u32;
    }
    let mut out = vec![0u32; npix];
    let mut pos = bp;
    let next = |pos: &mut usize| -> Res<u32> {
        let b = *c.get(*pos).ok_or("fits_tiles: a RICE_1 stream ends before its last pixel")?;
        *pos += 1;
        Ok(b as u32)
    };
    let mut b: u32 = next(&mut pos)?;
    let mut nbits: i32 = 8;
    let mut i = 0usize;
    while i < npix {
        nbits -= fsbits;
        while nbits < 0 {
            b = (b << 8) | next(&mut pos)?;
            nbits += 8;
        }
        let fs = (b >> nbits) as i32 - 1;
        b &= (1u32 << nbits) - 1;
        let imax = (i + blocksize as usize).min(npix);
        if fs < 0 {
            // low entropy: every difference is zero
            while i < imax {
                out[i] = lastpix;
                i += 1;
            }
        } else if fs == fsmax {
            // high entropy: the values coded directly
            while i < imax {
                let mut k = bbits - nbits;
                let mut diff: u32 = b.wrapping_shl(k as u32);
                k -= 8;
                while k >= 0 {
                    b = next(&mut pos)?;
                    diff |= b << k;
                    k -= 8;
                }
                if nbits > 0 {
                    b = next(&mut pos)?;
                    diff |= b >> (-k);
                    b &= (1u32 << nbits) - 1;
                } else {
                    b = 0;
                }
                diff = if diff & 1 == 0 { diff >> 1 } else { !(diff >> 1) };
                lastpix = diff.wrapping_add(lastpix) & mask;
                out[i] = lastpix;
                i += 1;
            }
        } else {
            while i < imax {
                while b == 0 {
                    nbits += 8;
                    b = next(&mut pos)?;
                }
                let nzero = nbits - (32 - b.leading_zeros() as i32);
                nbits -= nzero + 1;
                b ^= 1 << nbits;
                nbits -= fs;
                while nbits < 0 {
                    b = (b << 8) | next(&mut pos)?;
                    nbits += 8;
                }
                let mut diff = ((nzero as u32) << fs) | (b >> nbits);
                b &= (1u32 << nbits) - 1;
                diff = if diff & 1 == 0 { diff >> 1 } else { !(diff >> 1) };
                lastpix = diff.wrapping_add(lastpix) & mask;
                out[i] = lastpix;
                i += 1;
            }
        }
    }
    Ok(out)
}

// ---- gzip

/// The content of one gzip member (RFC 1952).
pub fn gunzip(data: &[u8], limit: usize) -> Res<Vec<u8>> {
    if data.len() < 18 || data[0] != 0x1f || data[1] != 0x8b || data[2] != 8 {
        return Err("fits_tiles: not a gzip member".into());
    }
    let flg = data[3];
    let mut at = 10;
    if flg & 4 != 0 {
        let n = u16::le_at(data, at) as usize;
        at += 2 + n;
    }
    for bit in [8u8, 16] {
        if flg & bit != 0 {
            while *data.get(at).ok_or("fits_tiles: a truncated gzip header")? != 0 {
                at += 1;
            }
            at += 1;
        }
    }
    if flg & 2 != 0 {
        at += 2;
    }
    if at + 8 > data.len() {
        return Err("fits_tiles: a truncated gzip member".into());
    }
    miniz_oxide::inflate::decompress_to_vec_with_limit(&data[at..data.len() - 8], limit).map_err(|e| format!("fits_tiles: gzip: {e:?}"))
}

// ---- dequantization (cfitsio unquantize.c)

const N_RANDOM: usize = 10000;

/// cfitsio's table of 10000 random values (`fits_init_randoms`): the Park–Miller generator
/// from seed 1, each value `seed / (2^31 − 1)` as a float.
pub fn random_table() -> &'static [f32; N_RANDOM] {
    static TABLE: OnceLock<[f32; N_RANDOM]> = OnceLock::new();
    TABLE.get_or_init(|| {
        let (a, m) = (16807.0f64, 2147483647.0f64);
        let mut seed = 1.0f64;
        let mut t = [0f32; N_RANDOM];
        for v in t.iter_mut() {
            let temp = a * seed;
            seed = temp - m * ((temp / m) as i64 as f64);
            *v = (seed / m) as f32;
        }
        debug_assert_eq!(seed as i64, 1043618065);
        t
    })
}

/// The quantized integers of tile `row` (its storage index plus `ZDITHER0`, as cfitsio's
/// `row`) as values: `(I − R + 0.5) × scale + zero` with the dither's random `R` for the
/// subtractive dithers, `I × scale + zero` without; `zblank` as NaN; `−2147483646` as 0 for
/// `SUBTRACTIVE_DITHER_2`.
pub fn dequantize(ints: &[i32], row: i64, method: u8, scale: f64, zero: f64, zblank: Option<i32>) -> Vec<f64> {
    let rn = random_table();
    let mut iseed = ((row - 1).rem_euclid(N_RANDOM as i64)) as usize;
    let mut nextrand = (rn[iseed] * 500.0) as usize;
    let mut out = Vec::with_capacity(ints.len());
    for &i in ints {
        let v = if zblank == Some(i) {
            f64::NAN
        } else if method == 2 && i == -2147483646 {
            0.0
        } else if method > 0 {
            (i as f64 - rn[nextrand] as f64 + 0.5) * scale + zero
        } else {
            i as f64 * scale + zero
        };
        out.push(v);
        if method > 0 {
            nextrand += 1;
            if nextrand == N_RANDOM {
                iseed = (iseed + 1) % N_RANDOM;
                nextrand = (rn[iseed] * 500.0) as usize;
            }
        }
    }
    out
}

// ---- the chunk

/// The decoded values of one tile, as little-endian bytes of the array's data type.
fn tile_values(stream: &[u8], rec: &TileRecord, cfg: &Config, tile_index: u64, npix: usize) -> Res<Vec<u8>> {
    let item = (cfg.zbitpix.unsigned_abs() / 8) as usize;
    let float = cfg.zbitpix < 0;
    let quantized = float && rec.flags & 1 != 0 && rec.kind == 0;
    // the tile's raw big-endian values, or its quantized integers
    let big: Vec<u8> = if rec.kind == 1 {
        gunzip(stream, npix * item)?
    } else {
        match cfg.compression.as_str() {
            "RICE_1" => {
                let bp = if quantized { 4 } else { cfg.bytepix };
                let vals = rice_decode(stream, npix, cfg.blocksize, bp)?;
                let mut out = Vec::with_capacity(npix * bp as usize);
                for v in vals {
                    out.extend_from_slice(&v.to_be_bytes()[4 - bp as usize..]);
                }
                out
            }
            "GZIP_1" => gunzip(stream, npix * 8)?,
            "GZIP_2" => {
                let shuffled = gunzip(stream, npix * 8)?;
                if npix == 0 || shuffled.len() % npix != 0 {
                    return Err("fits_tiles: a GZIP_2 tile's bytes are not a whole number per pixel".into());
                }
                let e = shuffled.len() / npix;
                let mut out = vec![0u8; shuffled.len()];
                for k in 0..e {
                    for i in 0..npix {
                        out[i * e + k] = shuffled[k * npix + i];
                    }
                }
                out
            }
            "NOCOMPRESS" => stream.to_vec(),
            other => return Err(format!("fits_tiles: compression {other}")),
        }
    };
    if quantized {
        if big.len() != npix * 4 {
            return Err(format!("fits_tiles: a quantized tile of {} bytes for {npix} pixels", big.len()));
        }
        let ints: Vec<i32> = big.chunks(4).map(|c| i32::be_at(c, 0)).collect();
        let zblank = (rec.flags & 2 != 0).then_some(rec.zblank);
        let vals = dequantize(&ints, tile_index as i64 + cfg.dither_seed, cfg.quantize, rec.zscale, rec.zzero, zblank);
        let mut out = Vec::with_capacity(npix * item);
        for v in vals {
            if item == 4 {
                out.extend_from_slice(&(v as f32).to_le_bytes());
            } else {
                out.extend_from_slice(&v.to_le_bytes());
            }
        }
        return Ok(out);
    }
    if big.len() != npix * item {
        return Err(format!("fits_tiles: a tile of {} bytes for {npix} pixels of {item} bytes", big.len()));
    }
    // big-endian to little-endian
    let mut out = Vec::with_capacity(big.len());
    for px in big.chunks(item) {
        out.extend(px.iter().rev());
    }
    Ok(out)
}

/// A chunk's pixels, as little-endian bytes of the array's data type in C order over
/// `chunk_shape`; the pixels no tile gives are 0.
pub fn decode(chunk: &[u8], cfg: &Config, chunk_shape: &[u64]) -> Res<Vec<u8>> {
    let n = cfg.shape.len();
    if cfg.tile.len() != n || chunk_shape.len() != n {
        return Err("fits_tiles: the shape, the tile shape and the chunk shape differ in rank".into());
    }
    let item = (cfg.zbitpix.unsigned_abs() / 8) as usize;
    let chunk_pixels: usize = chunk_shape.iter().product::<u64>() as usize;
    let mut out = vec![0u8; chunk_pixels * item];
    let (first, records, mut at) = parse_prefix(chunk)?;
    let (origin, _) = tile_at(&cfg.shape, &cfg.tile, first);
    for (k, rec) in records.iter().enumerate() {
        let r = first + k as u64;
        let len = rec.length as usize;
        if len == 0 {
            continue;
        }
        if at + len > chunk.len() {
            return Err("fits_tiles: the chunk is shorter than its tiles".into());
        }
        let stream = &chunk[at..at + len];
        at += len;
        let (coords, extent) = tile_at(&cfg.shape, &cfg.tile, r);
        let npix: usize = extent.iter().product::<u64>() as usize;
        let values = tile_values(stream, rec, cfg, r, npix)?;
        // place the tile: its offset in the chunk, by axis
        let offset: Vec<u64> = (0..n).map(|j| (coords[j] - origin[j]) * cfg.tile[j]).collect();
        if (0..n).any(|j| offset[j] + extent[j] > chunk_shape[j]) {
            return Err("fits_tiles: a tile lies outside its chunk".into());
        }
        // rows of the tile's last axis, copied one by one
        let row = extent[n - 1] as usize * item;
        let rows: usize = extent[..n - 1].iter().product::<u64>() as usize;
        for q in 0..rows {
            // the tile row's coords within the tile (axes before the last)
            let mut rest = q as u64;
            let mut dst = 0u64;
            let mut stride = 1u64;
            for j in (0..n).rev() {
                let c = if j == n - 1 { 0 } else { let c = rest % extent[j]; rest /= extent[j]; c };
                dst += (offset[j] + c) * stride;
                stride *= chunk_shape[j];
            }
            let dst = dst as usize * item;
            out[dst..dst + row].copy_from_slice(&values[q * row..(q + 1) * row]);
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn hex(s: &str) -> Vec<u8> {
        (0..s.len()).step_by(2).map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap()).collect()
    }

    /// Streams astropy's RICE_1 codec (cfitsio's) wrote, and the values it wrote them from.
    #[test]
    fn rice_streams_of_each_width() {
        let u8_vals = [207u32, 21, 45, 60, 46, 205, 222, 149, 10, 24, 85, 110, 159, 122, 67, 40, 177, 188, 8, 29, 115, 100, 227, 132, 107, 110, 170, 150, 44, 188, 193, 244, 201, 72, 81, 166, 166, 178, 222, 74];
        assert_eq!(rice_decode(&hex("cfe0118603c37824523d438f464c492da6bda2d3055583bfd7a620cf04fa7be14cdd57f84aa800616360"), 40, 32, 1).unwrap(), u8_vals);
        let i16_vals = [7i32, 463, 163, -151, -54, 162, 580, -80, -73, 300, -265, -87, 264, 174, 27, 201, -848, 306, -287, -500, 82, 210, -133, -322, 7, -15, 421, 224, 58, 333, -61, -277, 175, 174, -64, -234, 68, -748, 207, 147, -491, 18, -289, 227, -610];
        let got = rice_decode(&hex("0007a801c82574e761760d10c9e0e5d44d3b22beacf25d702310c10a87a928cc0156ef2a4a2b6d1c4e9689989f5f4e2201f6f534b825f1bb4ee5f6fe9329040a24"), 45, 32, 2).unwrap();
        assert_eq!(got.iter().map(|&v| v as u16 as i16 as i32).collect::<Vec<_>>(), i16_vals);
        let i16_b16 = [-274i32, 212, 346, -647, -149, 98, -182, 477, -357, 106, -314, 421, -6, -111, -515, 504, 225, 226, 341, 104, -191, -240, -240, 411, -438, -178, -96, 67, 172, -374, -519, -1, 364, 227, 64, -95, 87, -73, 245, -238, 40, -33, 163, 67, 765];
        let got = rice_decode(&hex("feeea801e66183c17c9f722f326141b9e68e77cd56d164e3f6a45b015cdeca4d98600316150a08a9346b4890f2120c93b488a8a9ead89f97c1c5259916237e0ba0"), 45, 16, 2).unwrap();
        assert_eq!(got.iter().map(|&v| v as u16 as i16 as i32).collect::<Vec<_>>(), i16_b16);
        let i32_vals = [104905i32, 104771, -142765, -23822, -42602, 37290, -159531, 82214, 74688, -91144, -68498, -56082, 3030, 44867, 143352, -13821, 53725, 10879, 123194, 51951, 95798, -75437, -13456, -56964, 105346, 46034, -21360, -31672, 33926, -49104, -65141, 33689, 172419, -17229, -38910, -81980, -93450];
        let got = rice_decode(&hex("000199c994000100858e377dd09f5256e9c0a040352360a28eb2cc3e3eb0ec98403cdd0d1b6980b5165f4a0fb4d3aedb6bb22c95d5a38ce717e43ad4f9cbd033cf5f41d072847a007c511573ea4b041c911dea1e4cfd52c3a83dcb3360"), 37, 32, 4).unwrap();
        assert_eq!(got.iter().map(|&v| v as i32).collect::<Vec<_>>(), i32_vals);
        // low entropy (a constant tile) and high entropy (values coded directly)
        assert!(rice_decode(&hex("fff900"), 40, 32, 2).unwrap().iter().all(|&v| v as u16 as i16 == -7));
        let wild = [542434981i32, 155977968, 679805593, 1876616576, 1876386371, -1606236804, 1247703095, -364365215, 1374178818, 722230708, -1906777950, 1652338267, 1832616318, 2146637659, -1887138398, -1530567662, 560680319, 160192505, -1744203052, 1637243803, -1140590678, -1919722930, 1551588626, 379216215, -2036024378, -1400540091, 1630469595, 1150322215, 1575087767, 1879610980, -856568480, 164446534, -553703477, -2108972940, -1134635898, -1871530619, -1643510598, -362057481, 1787333326, 1491659192];
        let got = rice_decode(&hex("2054e6a5d000000001708ddb49f38f9a94755e2e70003833cb06b633955e45544e01635d5e7a013a126dbf04de34c82ee2bdc32468abed32312b795dd0f9176471540d6707ca5e96d17def585f182c524b6733738da6d9c0f2e709d5bb117fd3bc5e0fbbaf00a5a6f25e0b87f4b5676699c9e77b3995168701226a7cd5ce936fc3cdb73e6695670cbd6e59c3ab5d099a0915f60f8046cba79aa630b61ebff1731448cfd08ac0"), 40, 32, 4).unwrap();
        assert_eq!(got.iter().map(|&v| v as i32).collect::<Vec<_>>(), wild);
        assert!(rice_decode(&hex("0007a8"), 45, 32, 2).is_err());
    }

    /// cfitsio's random table and dither, against astropy's `Quantize.decode_quantized`.
    #[test]
    fn dequantizes_as_cfitsio() {
        let rn = random_table();
        assert_eq!(&rn[..4], &[7.826369255781174e-06f32, 0.1315377950668335, 0.7556053400039673, 0.458650141954422]);
        assert_eq!(rn[1236], 0.25911977887153625);
        let ints = [5i32, -3, 0, 1000000, -2147483648, 42, 7, 7, 7, 9];
        let f32s = |v: Vec<f64>| v.into_iter().map(|x| x as f32).collect::<Vec<f32>>();
        assert_eq!(
            f32s(dequantize(&ints, 1237, 1, 0.125, -2.5, None)),
            [-1.8521924018859863f32, -2.9233739376068115, -2.519007444381714, 124997.546875, -268435456.0, 2.764273166656494, -1.610965609550476, -1.6249675750732422, -1.5809416770935059, -1.3858983516693115]
        );
        assert_eq!(
            f32s(dequantize(&ints, 4321, 2, 0.125, -2.5, None)),
            [-1.894239902496338f32, -2.864499807357788, -2.525148868560791, 124997.4453125, -268435456.0, 2.779693365097046, -1.5684330463409424, -1.654510259628296, -1.6035293340682983, -1.3915798664093018]
        );
        assert_eq!(f32s(dequantize(&ints, 0, 0, 0.125, -2.5, None)), [-1.875f32, -2.875, -2.5, 124997.5, -268435456.0, 2.75, -1.625, -1.625, -1.625, -1.375]);
        assert_eq!(dequantize(&ints[..4], 1237, 1, 0.125, -2.5, None), [-1.8521924428641796, -2.923373833298683, -2.519007332623005, 124997.54370811768]);
        // the blank value is NaN, and dither 2's zero value is 0
        let v = dequantize(&[-2147483648, -2147483646, 1], 10, 2, 1.0, 0.0, Some(-2147483648));
        assert!(v[0].is_nan() && v[1] == 0.0 && v[2] != 0.0);
    }

    /// A chunk of two row tiles of a 3 x 4 int16 image, the second an edge tile of one row,
    /// decoded into a [2, 4] chunk, and a chunk whose second tile is absent.
    #[test]
    fn places_tiles_in_the_chunk() {
        let cfg = Config { compression: "NOCOMPRESS".into(), blocksize: 32, bytepix: 2, zbitpix: 16, shape: vec![3, 4], tile: vec![1, 4], quantize: 0, dither_seed: 0 };
        let t0: Vec<u8> = [1i16, 2, 3, 4].iter().flat_map(|v| v.to_be_bytes()).collect();
        let t1: Vec<u8> = [5i16, 6, 7, 8].iter().flat_map(|v| v.to_be_bytes()).collect();
        let mut chunk = prefix(0, &[TileRecord { length: 8, ..Default::default() }, TileRecord { length: 8, ..Default::default() }]);
        chunk.extend(&t0);
        chunk.extend(&t1);
        let out = decode(&chunk, &cfg, &[2, 4]).unwrap();
        let vals: Vec<i16> = out.chunks(2).map(|c| i16::le_at(c, 0)).collect();
        assert_eq!(vals, [1, 2, 3, 4, 5, 6, 7, 8]);
        let mut chunk = prefix(2, &[TileRecord { length: 8, ..Default::default() }, TileRecord::default()]);
        chunk.extend(&t0);
        let out = decode(&chunk, &cfg, &[2, 4]).unwrap();
        let vals: Vec<i16> = out.chunks(2).map(|c| i16::le_at(c, 0)).collect();
        assert_eq!(vals, [1, 2, 3, 4, 0, 0, 0, 0]);
    }
}
