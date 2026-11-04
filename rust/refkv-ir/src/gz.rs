//! The start of a gzipped file (RFC 1952), for the profiles of a single array that a gzip stream
//! holds whole, its header first (spec/zarrify/nifti.md §7.4
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/nifti.md#74-gzipped-files>,
//! spec/zarrify/mrc.md §8.4 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mrc.md#84-gzipped-files>):
//! the stream is inflated only as far as the header, and its trailer says how many bytes it holds.

use crate::fed::Fed;
use crate::nd2::Step;
use serde_json::Value as J;

/// The bytes a gzip member starts with: its magic number, and the method deflate.
const SIGNATURE: [u8; 3] = [0x1f, 0x8b, 8];
/// The bytes of a gzip member's header without its optional fields, and of ISIZE, which ends its
/// trailer (after the CRC-32).
const MEMBER_HEADER: usize = 10;
const ISIZE: u64 = 4;
/// The compressed bytes read first; a read that does not reach as far as needed doubles, up to
/// `MAX_WINDOW`: a stream whose start holds more than that for what it inflates to is taken to end there.
const WINDOW: u64 = 1 << 16;
const MAX_WINDOW: u64 = 1 << 24;

/// The flags of a gzip member's header.
const FHCRC: u8 = 2;
const FEXTRA: u8 = 4;
const FNAME: u8 = 8;
const FCOMMENT: u8 = 16;

/// Whether a file's first bytes are a gzip member's.
pub fn is_member(head: &[u8]) -> bool {
    head.starts_with(&SIGNATURE)
}

/// Where the deflate data of the gzip member at the start of `b` starts, past its optional fields
/// (RFC 1952 §2.3); None when `b` ends before they do.
fn deflate_start(b: &[u8]) -> Option<usize> {
    let flags = *b.get(3)?;
    let mut at = MEMBER_HEADER;
    if flags & FEXTRA != 0 {
        let n = u16::from_le_bytes([*b.get(at)?, *b.get(at + 1)?]) as usize;
        at += 2 + n;
    }
    for field in [FNAME, FCOMMENT] {
        if flags & field != 0 {
            at += b.get(at..)?.iter().position(|&c| c == 0)? + 1;
        }
    }
    if flags & FHCRC != 0 {
        at += 2;
    }
    (at <= b.len()).then_some(at)
}

/// The start of a raw deflate stream, from its first bytes `data`: at most `cap` bytes of its
/// output, and whether more of the stream could give more (the output stopped short of `cap`
/// where `data` ran out, not where the stream ended or broke).
pub fn inflate_prefix(data: &[u8], cap: u64) -> (Vec<u8>, bool) {
    use miniz_oxide::inflate::stream::{InflateState, inflate};
    use miniz_oxide::{DataFormat, MZFlush, MZStatus};
    let mut st = InflateState::new_boxed(DataFormat::Raw);
    let mut out = Vec::new();
    let mut input = data;
    let mut buf = vec![0u8; 1 << 16];
    while (out.len() as u64) < cap {
        let room = (cap - out.len() as u64).min(buf.len() as u64) as usize;
        let r = inflate(&mut st, input, &mut buf[..room], MZFlush::None);
        out.extend_from_slice(&buf[..r.bytes_written]);
        input = &input[r.bytes_consumed..];
        match r.status {
            Ok(MZStatus::Ok) if r.bytes_written > 0 || r.bytes_consumed > 0 => {}
            Ok(MZStatus::StreamEnd) | Err(miniz_oxide::MZError::Data) => return (out, false),
            _ => {
                let more = (out.len() as u64) < cap && input.is_empty();
                return (out, more);
            }
        }
    }
    (out, false)
}

/// What the first bytes `head` of a gzip member inflate to, at most `cap` of them; None when they
/// are not a gzip member's, or end before its deflate data starts. A signature's test takes these
/// (spec/zarrify.md §1.2 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>).
pub fn start(head: &[u8], cap: u64) -> Option<Vec<u8>> {
    if !is_member(head) {
        return None;
    }
    Some(inflate_prefix(&head[deflate_start(head)?..], cap).0)
}

/// What a parser learned of a gzipped file: the first bytes it inflates to, and its trailer's
/// ISIZE, the stream's size modulo 2^32.
pub struct Inflated {
    pub bytes: Vec<u8>,
    pub isize: u32,
}

/// A parser's reads of a gzipped file of `size` bytes: its trailer, and as much of its start as
/// inflates to the bytes it asks for.
pub struct Reader {
    size: u64,
    window: u64,
}

impl Reader {
    pub fn new(size: u64) -> Reader {
        Reader { size, window: size.min(WINDOW) }
    }

    /// The first `want` bytes the file inflates to (fewer when the stream ends or breaks first),
    /// and its trailer; or the reads that will give them.
    pub fn inflate(&mut self, fed: &Fed, want: u64) -> Result<Inflated, Step> {
        let trailer_at = self.size.saturating_sub(ISIZE);
        let trailer = fed.get(trailer_at, self.size - trailer_at);
        loop {
            let Some(window) = fed.get(0, self.window) else {
                let mut reads = vec![(0, self.window)];
                if trailer.is_none() {
                    reads.push((trailer_at, self.size - trailer_at));
                }
                return Err(Step::Read(reads));
            };
            let (bytes, more) = match deflate_start(window) {
                Some(at) => inflate_prefix(&window[at..], want),
                None => (Vec::new(), true),
            };
            if !more || self.window == self.size || self.window >= MAX_WINDOW {
                let Some(trailer) = trailer else {
                    return Err(Step::Read(vec![(trailer_at, self.size - trailer_at)]));
                };
                let isize = u32::from_le_bytes(trailer.try_into().unwrap_or_default());
                return Ok(Inflated { bytes, isize });
            }
            self.window = (self.window * 2).min(self.size).min(MAX_WINDOW);
        }
    }
}

/// A size as a gzip trailer records it: modulo 2^32.
pub fn recorded(size: u128) -> u32 {
    size as u32
}

/// The inflated start a gzipped file's facts hold (`head`), which a projection reads for the
/// header the IR cannot hold: the stream's bytes are not the file's.
pub fn head_of(facts: &J) -> Option<Vec<u8>> {
    let hex = facts["gzip"]["head"].as_str()?;
    (0..hex.len() / 2).map(|i| u8::from_str_radix(&hex[2 * i..2 * i + 2], 16).ok()).collect()
}

/// `head` as `head_of` reads it.
pub fn head(bytes: &[u8]) -> J {
    J::String(bytes.iter().map(|b| format!("{b:02x}")).collect())
}
