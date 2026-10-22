//! TIFF LZW streams (TIFF 6.0 §13, as libtiff writes them: codes of 9 to 12 bits packed most
//! significant bit first, Clear 256, end-of-information 257, new entries from 258, and the
//! "early change" of the code width), as far as the QPTIFF profile needs them
//! (spec/virtualize/qptiff.md §4.2
//! <https://github.com/d-v-b/refkv/blob/main/spec/virtualize/qptiff.md#42-tiles-and-strips>): a
//! scan that decodes a strip's stream only to find where its output reaches the strip's size,
//! and the tail that continues such a stream with zero bytes, so that a short last strip
//! decodes to a whole chunk. The bytes decoded are not kept.

const CLEAR: u32 = 256;
const EOI: u32 = 257;
const FIRST: u32 = 258;
const MAX_CODE: u32 = 4096;

/// Where a stream's output reaches a size: the bit after the last code read, and the
/// decoder's state there (the code width, the next table entry, and whether an entry is
/// added on the next code).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Cut {
    pub bit: usize,
    pub width: u32,
    pub next: u32,
    pub has_prev: bool,
}

/// A decoder's table state without the strings: each entry's length and first byte are
/// enough to count the output.
struct State {
    length: Vec<u32>,
    first: Vec<u8>,
    width: u32,
    next: u32,
    prev: Option<u32>,
}

impl State {
    fn new() -> State {
        let mut s = State { length: vec![0; MAX_CODE as usize], first: vec![0; MAX_CODE as usize], width: 9, next: FIRST, prev: None };
        for i in 0..256 {
            s.length[i] = 1;
            s.first[i] = i as u8;
        }
        s
    }
    fn clear(&mut self) {
        self.width = 9;
        self.next = FIRST;
        self.prev = None;
    }
    /// The output a code adds, and the table entry it defines (as the decoder does).
    fn take(&mut self, code: u32) -> Result<u32, String> {
        let (n, first) = if code < self.next {
            (self.length[code as usize], self.first[code as usize])
        } else if code == self.next && self.prev.is_some() {
            let p = self.prev.unwrap() as usize;
            (self.length[p] + 1, self.first[p])
        } else {
            return Err(format!("invalid LZW code {code}"));
        };
        if let Some(p) = self.prev {
            if self.next < MAX_CODE {
                let k = self.next as usize;
                self.length[k] = self.length[p as usize] + 1;
                self.first[k] = first;
                self.next += 1;
                if self.next + 1 == 1 << self.width && self.width < 12 {
                    self.width += 1;
                }
            }
        }
        self.prev = Some(code);
        Ok(n)
    }
}

/// Reads `width` bits at `bit` of `data`, most significant first; None past the end.
fn read(data: &[u8], bit: usize, width: u32) -> Option<u32> {
    if bit + width as usize > data.len() * 8 {
        return None;
    }
    let mut v: u32 = 0;
    for i in 0..width as usize {
        let b = bit + i;
        v = (v << 1) | ((data[b / 8] >> (7 - b % 8)) & 1) as u32;
    }
    Some(v)
}

/// Decodes `data` until its output reaches `size` bytes: the cut there. The stream MUST
/// reach `size` exactly (a code whose string crosses it, an end-of-information code before
/// it, or a stream that ends first rejects); what follows the cut (the end-of-information
/// code, or the malformed final code some scanners write) is not read.
pub fn scan(data: &[u8], size: u64) -> Result<Cut, String> {
    let mut s = State::new();
    let mut bit = 0usize;
    let mut out: u64 = 0;
    if size == 0 {
        return Ok(Cut { bit, width: s.width, next: s.next, has_prev: false });
    }
    loop {
        let code = read(data, bit, s.width).ok_or("the LZW stream ends before the strip's size")?;
        bit += s.width as usize;
        if code == CLEAR {
            s.clear();
            continue;
        }
        if code == EOI {
            return Err("the LZW stream ends before the strip's size".into());
        }
        out += s.take(code)? as u64;
        if out >= size {
            if out > size {
                return Err("an LZW code crosses the strip's size".into());
            }
            return Ok(Cut { bit, width: s.width, next: s.next, has_prev: s.prev.is_some() });
        }
    }
}

/// Appends the `width` low bits of `code` to `bits`, most significant first.
fn put(bits: &mut Vec<u8>, nbits: &mut usize, code: u32, width: u32) {
    for i in (0..width).rev() {
        if *nbits % 8 == 0 {
            bits.push(0);
        }
        if (code >> i) & 1 == 1 {
            let last = bits.len() - 1;
            bits[last] |= 1 << (7 - *nbits % 8);
        }
        *nbits += 1;
    }
}

/// The bytes that continue a stream cut at `cut` with `zeros` zero bytes and an
/// end-of-information code: the stream's bytes before the cut's byte are referenced as they
/// are (`prefix` of them), and the rest is this literal, which starts with the bits of the
/// cut's byte before the cut. The literal is a Clear code (at the cut's width), then the run
/// of zeros as the chain 0, 258, 259, ... (each code one zero longer than the last, the
/// decoder's own KwKwK rule), with a final code for the remainder, then the end code; every
/// code at the width the decoder has when it reads it.
pub fn tail(data: &[u8], cut: Cut, zeros: u64) -> (u64, Vec<u8>) {
    let prefix = (cut.bit / 8) as u64;
    let mut bits: Vec<u8> = Vec::new();
    let mut nbits = 0usize;
    let keep = cut.bit % 8;
    if keep > 0 {
        let b = data[cut.bit / 8];
        put(&mut bits, &mut nbits, (b >> (8 - keep)) as u32, keep as u32);
    }
    // a decoder, from the cut on
    let mut s = State::new();
    s.width = cut.width;
    s.next = cut.next;
    s.prev = if cut.has_prev { Some(0) } else { None };
    put(&mut bits, &mut nbits, CLEAR, s.width);
    s.clear();
    let mut left = zeros;
    // emits a code at the decoder's width, as the decoder takes it
    fn emit(bits: &mut Vec<u8>, nbits: &mut usize, s: &mut State, code: u32) {
        put(bits, nbits, code, s.width);
        let _ = s.take(code);
    }
    // the chain: code 0 (one zero), then the entry the decoder just defined, each one longer
    let mut code = 0u32;
    let mut n = 1u32;
    while left > 0 && left >= n as u64 {
        emit(&mut bits, &mut nbits, &mut s, code);
        left -= n as u64;
        if code == 0 {
            code = FIRST;
            n = 2;
        } else {
            code += 1;
            n += 1;
        }
        if code >= MAX_CODE - 1 {
            break; // never for a strip: 4094 codes are more than 8 million zeros
        }
    }
    if left > 0 {
        // a remainder shorter than the next entry: an entry of that length, which exists
        let r = left as u32;
        let code = if r == 1 { 0 } else { FIRST + r - 2 };
        emit(&mut bits, &mut nbits, &mut s, code);
    }
    put(&mut bits, &mut nbits, EOI, s.width);
    (prefix, bits)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A plain TIFF LZW encoder (libtiff's, with the early change), for the tests.
    fn encode(data: &[u8]) -> Vec<u8> {
        use std::collections::HashMap;
        let mut bits = Vec::new();
        let mut nbits = 0usize;
        let mut table: HashMap<(u32, u8), u32> = HashMap::new();
        let (mut width, mut next) = (9u32, FIRST);
        put(&mut bits, &mut nbits, CLEAR, width);
        let mut cur: Option<u32> = None;
        for &b in data {
            match cur {
                None => cur = Some(b as u32),
                Some(c) => {
                    if let Some(&e) = table.get(&(c, b)) {
                        cur = Some(e);
                    } else {
                        put(&mut bits, &mut nbits, c, width);
                        table.insert((c, b), next);
                        next += 1;
                        if next == 1 << width && width < 12 {
                            width += 1;
                        }
                        if next >= MAX_CODE - 2 {
                            put(&mut bits, &mut nbits, CLEAR, width);
                            table.clear();
                            width = 9;
                            next = FIRST;
                        }
                        cur = Some(b as u32);
                    }
                }
            }
        }
        if let Some(c) = cur {
            put(&mut bits, &mut nbits, c, width);
            next += 1;
            if next == 1 << width && width < 12 {
                width += 1;
            }
        }
        put(&mut bits, &mut nbits, EOI, width);
        bits
    }

    /// A plain decoder, for the tests: the whole output.
    fn decode(data: &[u8]) -> Result<Vec<u8>, String> {
        let mut prefix = vec![-1i32; MAX_CODE as usize];
        let mut suffix = vec![0u8; MAX_CODE as usize];
        let mut first = vec![0u8; MAX_CODE as usize];
        let mut length = vec![0u32; MAX_CODE as usize];
        for i in 0..256 {
            suffix[i] = i as u8;
            first[i] = i as u8;
            length[i] = 1;
        }
        let (mut width, mut next, mut prev) = (9u32, FIRST, None::<u32>);
        let mut out = Vec::new();
        let mut bit = 0;
        loop {
            let code = read(data, bit, width).ok_or("ends")?;
            bit += width as usize;
            if code == CLEAR {
                width = 9;
                next = FIRST;
                prev = None;
                continue;
            }
            if code == EOI {
                return Ok(out);
            }
            let f;
            if code < next {
                f = first[code as usize];
                let n = length[code as usize] as usize;
                let at = out.len();
                out.resize(at + n, 0);
                let mut p = at + n - 1;
                let mut c = code as i32;
                while c >= 0 {
                    out[p] = suffix[c as usize];
                    c = prefix[c as usize];
                    p = p.wrapping_sub(1);
                }
            } else if code == next && prev.is_some() {
                let p = prev.unwrap();
                f = first[p as usize];
                let n = length[p as usize] as usize;
                let at = out.len();
                out.resize(at + n + 1, 0);
                let mut q = at + n - 1;
                let mut c = p as i32;
                while c >= 0 {
                    out[q] = suffix[c as usize];
                    c = prefix[c as usize];
                    q = q.wrapping_sub(1);
                }
                out[at + n] = f;
            } else {
                return Err(format!("invalid code {code}"));
            }
            if let Some(p) = prev {
                if next < MAX_CODE {
                    prefix[next as usize] = p as i32;
                    suffix[next as usize] = f;
                    first[next as usize] = first[p as usize];
                    length[next as usize] = length[p as usize] + 1;
                    next += 1;
                    if next + 1 == 1 << width && width < 12 {
                        width += 1;
                    }
                }
            }
            prev = Some(code);
        }
    }

    fn sample(n: usize) -> Vec<u8> {
        (0..n).map(|i| ((i * 7 + (i >> 5)) % 251) as u8).collect()
    }

    #[test]
    fn the_test_codec_round_trips() {
        for n in [1, 300, 5000, 70000] {
            let d = sample(n);
            assert_eq!(decode(&encode(&d)).unwrap(), d, "{n}");
        }
    }

    #[test]
    fn the_scan_finds_the_cut_and_the_tail_pads_with_zeros() {
        for (n, zeros) in [(1, 7), (300, 500), (5000, 3), (20000, 12000), (70000, 1)] {
            let d = sample(n);
            let e = encode(&d);
            let cut = scan(&e, n as u64).unwrap();
            assert!(cut.bit <= e.len() * 8 - 9, "{n}: the end code follows the cut");
            let (prefix, lit) = tail(&e, cut, zeros);
            let mut padded = e[..prefix as usize].to_vec();
            padded.extend_from_slice(&lit);
            let mut want = d.clone();
            want.extend(std::iter::repeat_n(0u8, zeros as usize));
            assert_eq!(decode(&padded).unwrap(), want, "{n} + {zeros}");
        }
    }

    #[test]
    fn a_tail_of_no_zeros_only_ends_the_stream() {
        let d = sample(300);
        let e = encode(&d);
        let (prefix, lit) = tail(&e, scan(&e, 300).unwrap(), 0);
        let mut padded = e[..prefix as usize].to_vec();
        padded.extend_from_slice(&lit);
        assert_eq!(decode(&padded).unwrap(), d);
    }

    #[test]
    fn the_scan_ignores_what_follows_the_size() {
        // a stream of 300 bytes whose end code is overwritten with junk from its first whole
        // byte on (the bits before it, in the cut's byte, stay)
        let d = sample(300);
        let mut e = encode(&d);
        let cut = scan(&e, 300).unwrap();
        e.truncate(cut.bit.div_ceil(8));
        e.extend_from_slice(&[0xff, 0xff, 0xff]);
        let cut = scan(&e, 300).unwrap();
        let (prefix, lit) = tail(&e, cut, 10);
        let mut padded = e[..prefix as usize].to_vec();
        padded.extend_from_slice(&lit);
        let mut want = d.clone();
        want.extend([0u8; 10]);
        assert_eq!(decode(&padded).unwrap(), want);
    }

    #[test]
    fn the_scan_rejects_short_and_crossing_streams() {
        let d = sample(300);
        let e = encode(&d);
        assert!(scan(&e, 400).unwrap_err().contains("ends before"));
        assert!(scan(&e[..20], 300).unwrap_err().contains("ends before"));
        // a size that falls inside a multi-byte code
        let repeated: Vec<u8> = std::iter::repeat_n(7u8, 1000).collect();
        let e = encode(&repeated);
        assert!(scan(&e, 999).unwrap_err().contains("crosses"));
    }
}
