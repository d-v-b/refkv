//! The `refkv.h5_bitshuffle` codec (spec/zarrify/hdf5.md §3.4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/hdf5.md#34-the-codec-refkvh5_bitshuffle>): decodes a chunk of
//! HDF5's bitshuffle filter (32008), as `bshuf_h5_filter` and `bshuf_blocked_wrap_fun` of the
//! bitshuffle project write it and `decompress_buffer_bslz4_hdf5` of DECTRIS's compression library
//! reads it. One implementation serves Python and the browser's core.
//!
//! **Blocks.** A chunk of `s` elements of `e` bytes is cut into blocks of `b` elements, `b` a
//! multiple of 8; what is left is a smaller block of a multiple of 8 elements, and the last
//! `s mod 8` elements, kept as they are. Each block is bit-transposed: bit `i` of byte `j` of element
//! `k` is bit `k mod 8` of byte `floor(k / 8)` of row `8j + i`, the rows `b / 8` bytes each, in order.
//!
//! **LZ4.** With LZ4 the chunk starts with its decoded length (8 bytes, big-endian) and the block
//! size in bytes (4 bytes, big-endian), which govern; each block is its compressed length (4 bytes,
//! big-endian) and an LZ4 block. Without it there is no header, and the blocks are bare.

type Res<T> = Result<T, String>;

/// The element counts a block size must be a multiple of, and bitshuffle's default block's bytes
/// and least elements (`BSHUF_BLOCKED_MULT`, `BSHUF_TARGET_BLOCK_SIZE_B`, `BSHUF_MIN_RECOMMEND_BLOCK`).
const MULT: usize = 8;
const TARGET_BYTES: u64 = 8192;
const LEAST: u64 = 128;

/// The block size, in elements, bitshuffle chooses for elements of `size` bytes
/// (`bshuf_default_block_size`).
pub fn default_block(size: u64) -> u64 {
    (TARGET_BYTES / size.max(1) / MULT as u64 * MULT as u64).max(LEAST)
}

/// The chunk's bytes, decoded.
pub fn decode(chunk: &[u8], size: usize, block: usize, lz4: bool) -> Res<Vec<u8>> {
    if size == 0 {
        return Err("refkv.h5_bitshuffle: an element of no bytes".into());
    }
    if lz4 { decode_lz4(chunk, size) } else { decode_bare(chunk, size, block) }
}

fn decode_bare(chunk: &[u8], size: usize, block: usize) -> Res<Vec<u8>> {
    if block == 0 || block % MULT != 0 {
        return Err(format!("refkv.h5_bitshuffle: a block of {block} elements is not a multiple of {MULT}"));
    }
    if chunk.len() % size != 0 {
        return Err(format!("refkv.h5_bitshuffle: {} bytes are not whole elements of {size} bytes", chunk.len()));
    }
    let mut out = vec![0u8; chunk.len()];
    let block_bytes = block * size;
    let tail = chunk.len() % (MULT * size);
    let shuffled = chunk.len() - tail;
    let (mut at, end) = (0usize, shuffled);
    while at < end {
        let n = block_bytes.min(end - at);
        untranspose(&chunk[at..at + n], &mut out[at..at + n], size);
        at += n;
    }
    out[shuffled..].copy_from_slice(&chunk[shuffled..]);
    Ok(out)
}

fn decode_lz4(chunk: &[u8], size: usize) -> Res<Vec<u8>> {
    let short = || "refkv.h5_bitshuffle: a chunk ends inside its framing".to_string();
    let total = u64::from_be_bytes(chunk.get(..8).ok_or_else(short)?.try_into().unwrap());
    let block = u32::from_be_bytes(chunk.get(8..12).ok_or_else(short)?.try_into().unwrap()) as usize;
    if total == 0 {
        return Ok(Vec::new());
    }
    if block == 0 || block % (MULT * size) != 0 {
        return Err(format!("refkv.h5_bitshuffle: a block of {block} bytes is not a multiple of {} bytes", MULT * size));
    }
    // LZ4 expands at most 255 times, so a length past that is no chunk's (and not worth allocating)
    if total % size as u64 != 0 || total > (chunk.len() as u64).saturating_mul(255).saturating_add(block as u64) {
        return Err(format!("refkv.h5_bitshuffle: {total} decoded bytes from a chunk of {} bytes", chunk.len()));
    }
    let total = total as usize;
    let tail = total % (MULT * size);
    let shuffled = total - tail;
    let mut out = vec![0u8; total];
    let mut plain = vec![0u8; block];
    let (mut src, mut dst) = (12usize, 0usize);
    while dst < shuffled {
        let n = block.min(shuffled - dst);
        let stored = chunk.get(src..src + 4).ok_or_else(short)?;
        let stored = u32::from_be_bytes(stored.try_into().unwrap()) as usize;
        src += 4;
        let body = chunk.get(src..src.saturating_add(stored)).ok_or_else(short)?;
        src += stored;
        lz4_block(body, &mut plain[..n])?;
        untranspose(&plain[..n], &mut out[dst..dst + n], size);
        dst += n;
    }
    let rest = chunk.get(src..).unwrap_or(&[]);
    if rest.len() != tail {
        return Err(format!("refkv.h5_bitshuffle: {} bytes after the blocks, not the {tail} left over", rest.len()));
    }
    out[shuffled..].copy_from_slice(rest);
    Ok(out)
}

/// One block's elements from its rows (the header's comment): each element's byte `j`, 8 elements at
/// a time, is the transpose of 8 rows' bytes at one offset.
fn untranspose(rows: &[u8], out: &mut [u8], size: usize) {
    let row = rows.len() / (8 * size);
    for j in 0..size {
        for g in 0..row {
            let mut x = 0u64;
            for i in 0..8 {
                x |= (rows[(8 * j + i) * row + g] as u64) << (8 * i);
            }
            // the 8 × 8 bit matrix transposed, by three swaps (Hacker's Delight, 7-3)
            let t = (x ^ (x >> 7)) & 0x00AA00AA00AA00AA;
            x ^= t ^ (t << 7);
            let t = (x ^ (x >> 14)) & 0x0000CCCC0000CCCC;
            x ^= t ^ (t << 14);
            let t = (x ^ (x >> 28)) & 0x00000000F0F0F0F0;
            x ^= t ^ (t << 28);
            for m in 0..8 {
                out[(8 * g + m) * size + j] = (x >> (8 * m)) as u8;
            }
        }
    }
}

/// An LZ4 block (LZ4's block format) into `out`, which it must fill exactly.
fn lz4_block(src: &[u8], out: &mut [u8]) -> Res<()> {
    let bad = || "refkv.h5_bitshuffle: an LZ4 block does not decode to its block".to_string();
    let (mut s, mut d) = (0usize, 0usize);
    let run = |s: &mut usize, mut n: usize| -> Res<usize> {
        if n == 15 {
            loop {
                let b = *src.get(*s).ok_or_else(bad)?;
                *s += 1;
                n = n.checked_add(b as usize).ok_or_else(bad)?;
                if b != 255 {
                    break;
                }
            }
        }
        Ok(n)
    };
    while s < src.len() {
        let token = src[s];
        s += 1;
        let literals = run(&mut s, (token >> 4) as usize)?;
        let from = src.get(s..s.saturating_add(literals)).ok_or_else(bad)?;
        out.get_mut(d..d + literals).ok_or_else(bad)?.copy_from_slice(from);
        s += literals;
        d += literals;
        if s == src.len() {
            break; // the last sequence is literals alone
        }
        let offset = u16::from_le_bytes(src.get(s..s + 2).ok_or_else(bad)?.try_into().unwrap()) as usize;
        s += 2;
        let length = run(&mut s, (token & 15) as usize)? + 4;
        if offset == 0 || offset > d || d + length > out.len() {
            return Err(bad());
        }
        // a match may overlap what it writes, so a byte at a time
        for k in 0..length {
            out[d + k] = out[d + k - offset];
        }
        d += length;
    }
    if d != out.len() {
        return Err(bad());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The transposition of the header's comment, written out bit by bit.
    fn transpose(plain: &[u8], size: usize) -> Vec<u8> {
        let n = plain.len() / size;
        let mut rows = vec![0u8; plain.len()];
        for k in 0..n {
            for j in 0..size {
                for i in 0..8 {
                    if plain[k * size + j] >> i & 1 != 0 {
                        rows[(8 * j + i) * (n / 8) + k / 8] |= 1 << (k % 8);
                    }
                }
            }
        }
        rows
    }

    /// LZ4 literals alone: a valid block, whatever its bytes.
    fn lz4_literals(b: &[u8]) -> Vec<u8> {
        let mut out = Vec::new();
        if b.len() < 15 {
            out.push((b.len() as u8) << 4);
        } else {
            out.push(0xF0);
            let mut n = b.len() - 15;
            while n >= 255 {
                out.push(255);
                n -= 255;
            }
            out.push(n as u8);
        }
        out.extend_from_slice(b);
        out
    }

    fn frame(plain: &[u8], size: usize, block: usize) -> Vec<u8> {
        let mut out = (plain.len() as u64).to_be_bytes().to_vec();
        out.extend_from_slice(&((block * size) as u32).to_be_bytes());
        let tail = plain.len() % (MULT * size);
        for b in plain[..plain.len() - tail].chunks(block * size) {
            let z = lz4_literals(&transpose(b, size));
            out.extend_from_slice(&(z.len() as u32).to_be_bytes());
            out.extend_from_slice(&z);
        }
        out.extend_from_slice(&plain[plain.len() - tail..]);
        out
    }

    #[test]
    fn decodes_every_framing() {
        // element sizes, block sizes, and counts with and without a short block and a leftover
        for (size, block, count) in [(1usize, 8usize, 8usize), (2, 16, 64), (2, 16, 75), (4, 8, 35), (8, 128, 1000), (3, 16, 17)] {
            let plain: Vec<u8> = (0..size * count).map(|k| (k * 31 % 251) as u8).collect();
            let tail = plain.len() % (MULT * size);
            let mut bare: Vec<u8> = plain[..plain.len() - tail].chunks(block * size).flat_map(|b| transpose(b, size)).collect();
            bare.extend_from_slice(&plain[plain.len() - tail..]);
            assert_eq!(decode(&bare, size, block, false).unwrap(), plain, "bare {size} {block} {count}");
            assert_eq!(decode(&frame(&plain, size, block), size, 0, true).unwrap(), plain, "lz4 {size} {block} {count}");
        }
        assert_eq!(decode(&[0; 12], 2, 0, true).unwrap(), Vec::<u8>::new());
        assert_eq!(default_block(1), 8192);
        assert_eq!(default_block(4), 2048);
        assert_eq!(default_block(1000), 128);
    }

    #[test]
    fn lz4_matches_overlap() {
        // 1 literal, then a match of 7 at offset 1: "aaaaaaaa"
        let mut out = [0u8; 8];
        lz4_block(&[0x13, b'a', 1, 0], &mut out).unwrap();
        assert_eq!(&out, b"aaaaaaaa");
    }

    #[test]
    fn refuses_an_element_of_no_bytes() {
        assert!(decode(&[], 0, 8, false).unwrap_err().contains("no bytes"));
    }

    #[test]
    fn refuses_a_block_not_a_multiple_of_8() {
        assert!(decode(&[0; 8], 1, 7, false).unwrap_err().contains("multiple"));
    }

    #[test]
    fn refuses_partial_elements() {
        assert!(decode(&[0; 7], 2, 8, false).unwrap_err().contains("whole elements"));
    }

    #[test]
    fn refuses_a_short_header() {
        assert!(decode(&[0; 11], 2, 0, true).unwrap_err().contains("framing"));
    }

    #[test]
    fn refuses_a_header_block_not_a_multiple() {
        let mut c = 16u64.to_be_bytes().to_vec();
        c.extend_from_slice(&7u32.to_be_bytes());
        assert!(decode(&c, 2, 0, true).unwrap_err().contains("multiple"));
    }

    #[test]
    fn refuses_an_impossible_length() {
        let mut c = (1u64 << 40).to_be_bytes().to_vec();
        c.extend_from_slice(&16u32.to_be_bytes());
        assert!(decode(&c, 2, 0, true).unwrap_err().contains("decoded bytes"));
    }

    #[test]
    fn refuses_a_truncated_block() {
        let plain = [1u8; 16];
        let f = frame(&plain, 1, 8);
        assert!(decode(&f[..f.len() - 2], 1, 0, true).unwrap_err().contains("framing"));
    }

    #[test]
    fn refuses_trailing_bytes() {
        let mut f = frame(&[1u8; 16], 1, 8);
        f.push(0);
        assert!(decode(&f, 1, 0, true).unwrap_err().contains("left over"));
    }

    #[test]
    fn refuses_a_block_of_the_wrong_length() {
        let mut c = 8u64.to_be_bytes().to_vec();
        c.extend_from_slice(&8u32.to_be_bytes());
        let z = lz4_literals(&[1, 2, 3]);
        c.extend_from_slice(&(z.len() as u32).to_be_bytes());
        c.extend_from_slice(&z);
        assert!(decode(&c, 1, 0, true).unwrap_err().contains("LZ4"));
    }

    #[test]
    fn refuses_a_match_before_the_block() {
        let mut out = [0u8; 8];
        assert!(lz4_block(&[0x13, b'a', 2, 0], &mut out).unwrap_err().contains("LZ4"));
    }
}
