// Copyright 2022 Matthew Iannucci
//
// Permission is hereby granted, free of charge, to any person obtaining a copy of this software
// and associated documentation files (the "Software"), to deal in the Software without
// restriction, including without limitation the rights to use, copy, modify, merge, publish,
// distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the
// Software is furnished to do so, subject to the following conditions:
//
// The above copyright notice and this permission notice shall be included in all copies or
// substantial portions of the Software.
//
// THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING
// BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
// NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
// DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
// OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

//! The CCSDS 121.0-B adaptive entropy coded streams of GRIB template 5.42 (WMO-No. 306 FM 92,
//! template 7.42), decoded to their samples. Ported from gribberish's pure-Rust decoder
//! (`gribberish/src/utils/ccsds.rs` of <https://github.com/mpiannucci/gribberish>, MIT, at
//! commit d9e8e9024adcc03a3c7505575d4788fb55a87921), itself modeled on libaec's `decode.c`:
//! trimmed to decoding a whole stream at once (gribberish's resumable modes, for output buffers
//! that fill mid-block, are gone), with libaec's flags as plain constants (no `bitflags`), and
//! the samples returned as integers rather than bytes. Two changes follow libaec where
//! gribberish does not: the identifier of a block's coding option is 3 bits wide for samples
//! of 1 to 8 bits, 1 or 2 bits only with the restricted set of options (`AEC_RESTRICTED`); and a
//! stream that ends early is an error, not a short output. Signed samples (`AEC_DATA_SIGNED`)
//! are not decoded: GRIB's are unsigned.

type Res<T> = Result<T, String>;

const DATA_SIGNED: u8 = 1;
const DATA_PREPROCESS: u8 = 8;
const RESTRICTED: u8 = 16;
const PAD_RSI: u8 = 32;
const NOT_ENFORCE: u8 = 64;

/// A run of zero blocks that ends at the reference sample interval's end, or its next multiple
/// of 64 blocks (the "remainder of segment").
const ROS: u32 = 5;
/// The last index of the second extension's table.
const SE_TABLE_SIZE: usize = 90;

/// The second extension's table: for each index m, the pair's sum and the first index of that
/// sum (libaec's `create_se_table`).
fn se_table() -> [i32; 2 * (SE_TABLE_SIZE + 1)] {
    let mut table = [0; 2 * (SE_TABLE_SIZE + 1)];
    let mut k = 0;
    for i in 0..13 {
        let ms = k as i32;
        for _ in 0..=i {
            table[2 * k] = i;
            table[2 * k + 1] = ms;
            k += 1;
        }
    }
    table
}

/// The stream's bits, most significant first.
struct Reader<'a> {
    b: &'a [u8],
    pos: usize,
}

impl Reader<'_> {
    fn short<T>() -> Res<T> {
        Err("the CCSDS stream ends before its samples do".into())
    }

    /// The next `n` bits (n ≤ 32).
    fn get(&mut self, n: u32) -> Res<u32> {
        let end = self.pos + n as usize;
        if end > self.b.len() * 8 {
            return Self::short();
        }
        let mut v = 0u64;
        while self.pos < end {
            let off = (self.pos & 7) as u32;
            let take = (8 - off).min((end - self.pos) as u32);
            let byte = self.b[self.pos >> 3] as u64;
            v = (v << take) | ((byte >> (8 - off - take)) & ((1 << take) - 1));
            self.pos += take as usize;
        }
        Ok(v as u32)
    }

    /// A fundamental sequence: the number of 0 bits before the next 1 bit, which it consumes.
    fn fs(&mut self) -> Res<u32> {
        let mut n = 0u32;
        loop {
            let Some(&byte) = self.b.get(self.pos >> 3) else {
                return Self::short();
            };
            let off = (self.pos & 7) as u32;
            let rest = byte << off;
            if rest == 0 {
                n += 8 - off;
                self.pos += (8 - off) as usize;
                continue;
            }
            let z = rest.leading_zeros();
            n += z;
            self.pos += z as usize + 1;
            return Ok(n);
        }
    }

    /// To the next octet.
    fn align(&mut self) {
        self.pos = self.pos.div_ceil(8) * 8;
    }
}

/// The first `n` samples of the stream `data`, of `bits_per_sample` bits, coded in blocks of
/// `block_size` samples, `rsi` blocks to a reference sample interval, with libaec's `flags`.
pub fn decode(data: &[u8], bits_per_sample: u32, block_size: u32, rsi: u32, flags: u8, n: usize) -> Res<Vec<u32>> {
    let bps = bits_per_sample;
    if !(1..=32).contains(&bps) {
        return Err(format!("CCSDS samples of {bps} bits are not decoded"));
    }
    if flags & DATA_SIGNED != 0 {
        return Err("signed CCSDS samples are not decoded".into());
    }
    let enforced = flags & NOT_ENFORCE == 0;
    if block_size == 0 || (enforced && ![8, 16, 32, 64].contains(&block_size)) || block_size & 1 != 0 {
        return Err(format!("a CCSDS block size of {block_size} is not decoded"));
    }
    if rsi == 0 || rsi > 4096 {
        return Err(format!("a CCSDS reference sample interval of {rsi} blocks is not decoded"));
    }
    let id_len = if flags & RESTRICTED != 0 {
        match bps {
            1..=2 => 1,
            3..=4 => 2,
            _ => return Err(format!("the restricted CCSDS options with samples of {bps} bits are not decoded")),
        }
    } else if bps > 16 {
        5
    } else if bps > 8 {
        4
    } else {
        3
    };
    let uncompressed = (1u32 << id_len) - 1;
    let xmax = ((1u64 << bps) - 1) as u32;
    let pp = flags & DATA_PREPROCESS != 0;
    let se = se_table();
    let (block, rsi_size) = (block_size as usize, (rsi * block_size) as usize);
    let mut r = Reader { b: data, pos: 0 };
    let mut out = Vec::with_capacity(n);
    let mut buf: Vec<u32> = Vec::with_capacity(rsi_size);
    while out.len() < n {
        // one reference sample interval: blocks until it is whole, or holds the last samples
        buf.clear();
        let mut reference = pp as usize;
        while buf.len() < rsi_size && out.len() + buf.len() < n {
            let id = r.get(id_len)?;
            if id == 0 {
                // low entropy: the second extension, or a run of zero blocks
                let second = r.get(1)?;
                if reference == 1 {
                    buf.push(r.get(bps)?);
                }
                if second == 1 {
                    let mut i = reference;
                    while i < block {
                        let m = r.fs()? as usize;
                        if m > SE_TABLE_SIZE {
                            return Err("a CCSDS second extension's index is past its table".into());
                        }
                        let d1 = m as i32 - se[2 * m + 1];
                        if i & 1 == 0 {
                            buf.push((se[2 * m] - d1) as u32);
                            i += 1;
                        }
                        buf.push(d1 as u32);
                        i += 1;
                    }
                } else {
                    let mut zero_blocks = r.fs()? + 1;
                    if zero_blocks == ROS {
                        let b = (buf.len() / block) as u32;
                        zero_blocks = (rsi - b).min(64 - b % 64);
                    } else if zero_blocks > ROS {
                        zero_blocks -= 1;
                    }
                    let zeros = (zero_blocks as usize * block).saturating_sub(reference);
                    if buf.len() + zeros > rsi_size {
                        return Err("a CCSDS run of zero blocks is past its reference sample interval".into());
                    }
                    buf.resize(buf.len() + zeros, 0);
                }
            } else if id == uncompressed {
                for _ in 0..block {
                    buf.push(r.get(bps)?);
                }
            } else {
                // split samples: k low bits of each, after the fundamental sequences of the rest
                let k = id - 1;
                if reference == 1 {
                    buf.push(r.get(bps)?);
                }
                let start = buf.len();
                for _ in reference..block {
                    buf.push(r.fs()?.wrapping_shl(k));
                }
                if k > 0 {
                    for v in &mut buf[start..] {
                        *v = v.wrapping_add(r.get(k)?);
                    }
                }
            }
            reference = 0;
        }
        if pp {
            postprocess(&buf, xmax, &mut out);
        } else {
            out.extend_from_slice(&buf);
        }
        if flags & PAD_RSI != 0 {
            r.align();
        }
    }
    out.truncate(n);
    Ok(out)
}

/// The samples of a reference sample interval from their preprocessed residuals `d`, after its
/// reference sample `d[0]` (CCSDS 121.0-B's unit-delay predictor, unsigned; libaec's
/// `flush_kind`).
fn postprocess(d: &[u32], xmax: u32, out: &mut Vec<u32>) {
    let Some(&first) = d.first() else { return };
    out.push(first);
    let med = xmax / 2 + 1;
    let mut data = first;
    for &d in &d[1..] {
        let half = (d >> 1) + (d & 1);
        let mask = if data & med != 0 { xmax } else { 0 };
        data = if half <= mask ^ data {
            // + d/2 for an even residual, − (d + 1)/2 for an odd one
            data.wrapping_add((d >> 1) ^ !(d & 1).wrapping_sub(1))
        } else {
            mask ^ d
        };
        out.push(data);
    }
}
