//! The PNG images of GRIB template 5.41 (WMO-No. 306 FM 92, template 7.41; the PNG
//! specification, W3C, third edition): their samples, each pixel's bits as one big-endian
//! integer, row after row. Non-interlaced grayscale of 1, 2, 4, 8 or 16 bits, 8-bit RGB (a 24-bit
//! integer) and 8-bit RGBA (32 bits), as GRIB's encoders write them; the image data inflated with
//! miniz_oxide, the filters of each row undone. Chunk CRCs are not checked.

type Res<T> = Result<T, String>;

const SIGNATURE: &[u8; 8] = b"\x89PNG\r\n\x1a\n";

/// The samples of the PNG image `b`, `width × height` of them.
pub fn decode(b: &[u8]) -> Res<Vec<u64>> {
    if b.len() < 8 || &b[..8] != SIGNATURE {
        return Err("section 7 is not a PNG image".into());
    }
    let mut pos = 8;
    let mut header: Option<&[u8]> = None;
    let mut idat = Vec::new();
    loop {
        if pos + 12 > b.len() {
            return Err("the PNG image ends before its IEND chunk".into());
        }
        let n = u32::from_be_bytes(b[pos..pos + 4].try_into().unwrap()) as usize;
        let kind = &b[pos + 4..pos + 8];
        if n > b.len() - pos - 12 {
            return Err(format!("the PNG image's {} chunk runs past its end", String::from_utf8_lossy(kind)));
        }
        let data = &b[pos + 8..pos + 8 + n];
        match kind {
            b"IHDR" => header = Some(data),
            b"IDAT" => idat.extend_from_slice(data),
            b"PLTE" => return Err("a PNG image with a palette is not decoded".into()),
            b"IEND" => break,
            _ => {}
        }
        pos += 12 + n;
    }
    let Some(h) = header.filter(|h| h.len() == 13) else {
        return Err("the PNG image has no IHDR chunk".into());
    };
    let width = u32::from_be_bytes(h[0..4].try_into().unwrap()) as usize;
    let height = u32::from_be_bytes(h[4..8].try_into().unwrap()) as usize;
    let (depth, color, interlace) = (h[8] as usize, h[9], h[12]);
    let pixel = match (color, depth) {
        (0, 1 | 2 | 4 | 8 | 16) => depth,
        (2, 8) => 24,
        (6, 8) => 32,
        _ => return Err(format!("a PNG image of color type {color} and bit depth {depth} is not decoded")),
    };
    if interlace != 0 {
        return Err("an interlaced PNG image is not decoded".into());
    }
    if h[10] != 0 || h[11] != 0 {
        return Err("the PNG image's compression or filter method is not 0".into());
    }
    let row = (width * pixel).div_ceil(8);
    let size = height.checked_mul(row + 1).ok_or("the PNG image is too large")?;
    let raw = miniz_oxide::inflate::decompress_to_vec_zlib_with_limit(&idat, size)
        .map_err(|e| format!("the PNG image's data does not inflate: {:?}", e.status))?;
    if raw.len() != size {
        return Err(format!("the PNG image's data inflates to {} bytes, not {size}", raw.len()));
    }
    let bpp = (pixel / 8).max(1);
    let mut rows = vec![0u8; height * row];
    let mut prior = vec![0u8; row];
    for (r, line) in raw.chunks(row + 1).enumerate() {
        let out = &mut rows[r * row..(r + 1) * row];
        unfilter(line[0], &line[1..], &prior, bpp, out)?;
        prior.copy_from_slice(out);
    }
    let mut samples = Vec::with_capacity(width * height);
    for line in rows.chunks(row.max(1)).take(height) {
        for x in 0..width {
            samples.push(if pixel < 8 {
                let bit = x * pixel;
                ((line[bit / 8] >> (8 - pixel - bit % 8)) & ((1 << pixel) - 1) as u8) as u64
            } else {
                line[x * pixel / 8..(x + 1) * pixel / 8].iter().fold(0u64, |v, &c| (v << 8) | c as u64)
            });
        }
    }
    Ok(samples)
}

/// A row's bytes from its filtered bytes `line` of filter type `f`, the row before it `prior`
/// (zeros for the first), and the bytes of a whole pixel `bpp` (at least 1).
fn unfilter(f: u8, line: &[u8], prior: &[u8], bpp: usize, out: &mut [u8]) -> Res<()> {
    for k in 0..line.len() {
        let a = if k >= bpp { out[k - bpp] } else { 0 } as i16;
        let b = prior[k] as i16;
        let c = if k >= bpp { prior[k - bpp] } else { 0 } as i16;
        let p = match f {
            0 => 0,
            1 => a,
            2 => b,
            3 => (a + b) / 2,
            4 => {
                let (pa, pb, pc) = ((b - c).abs(), (a - c).abs(), (a + b - 2 * c).abs());
                if pa <= pb && pa <= pc { a } else if pb <= pc { b } else { c }
            }
            _ => return Err(format!("a PNG row's filter type {f} is not one of 0–4")),
        };
        out[k] = line[k].wrapping_add(p as u8);
    }
    Ok(())
}
