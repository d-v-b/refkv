//! The chunks of an array of audio samples (spec/zarrify/mp3.md §8.2
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp3.md#82-chunks>, spec/zarrify/mp4.md
//! §8.3 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/mp4.md#83-chunks>): runs of frames,
//! each a `refkv.stream` chunk with the frames before it that a decoder needs (its lead-in).

use crate::ir::Ir;
use crate::out::stream_parts;
use crate::refs::Refs;

/// The first frame of the lead-in of an MP3 chunk that starts at frame `a`: none for the first
/// chunk; else the two frames before the chunk, and before them the fewest frames whose main
/// data (`main(j)` bytes in frame j) holds at least `reservoir` bytes, or every frame from the
/// first when they do not.
pub fn reservoir_lead(a: usize, reservoir: u64, main: impl Fn(usize) -> u64) -> usize {
    if a == 0 {
        return 0;
    }
    let mut l = a.saturating_sub(2);
    let mut have = 0u64;
    while l > 0 && have < reservoir {
        l -= 1;
        have += main(l);
    }
    l
}

/// The chunks `{path}/c/{q}/0` of the `count` frames that are the IR's elements `first`,
/// `first + 1`, ... (of the form `form`), `per` frames a chunk of `spf` samples each: chunk q the
/// frames from its lead-in `lead(a)` to its last (the bytes between frames, which are no frame,
/// left out: a decoder resyncs on them), the lead-in's samples dropped and frame j's at
/// (j − a) · `spf`, charged as its first frame a, piece q. Returns the count of chunks.
#[allow(clippy::too_many_arguments)]
pub fn chunks(refs: &mut Refs, ir: &Ir, path: &str, first: usize, count: usize, per: usize, spf: u64, form: u32,
              lead: impl Fn(usize) -> usize) -> Result<usize, String> {
    let frame = |j: usize| (ir.start[first + j], ir.len[first + j]);
    let chunks = count.div_ceil(per);
    for q in 0..chunks {
        let a = q * per;
        let b = (a + per).min(count);
        let units: Vec<(u64, u64, i64)> = (lead(a)..b)
            .map(|j| {
                let (o, len) = frame(j);
                (o, len, if j < a { -1 } else { ((j - a) as u64 * spf) as i64 })
            })
            .collect();
        let (o, len) = frame(a);
        refs.chunk(format!("{path}/c/{q}/0"), o, len, form, Some(stream_parts(&units)), q as u64)?;
    }
    Ok(chunks)
}

#[cfg(test)]
mod tests {
    use super::reservoir_lead;

    /// Frames of 100 bytes of main data, a reservoir of 511: the first chunk has no lead-in;
    /// a chunk at frame 8 takes frames 6 and 7, and before them the 6 frames that hold 600
    /// bytes (frames 0 to 5): all of them; at frame 12, frames 10 and 11 and then frames 4
    /// to 9 (600 bytes), so the lead-in starts at 4; a small reservoir takes one frame.
    #[test]
    fn the_lead_in_holds_the_reservoir_before_the_two_frames_before_the_chunk() {
        let main = |_| 100u64;
        assert_eq!(reservoir_lead(0, 511, main), 0);
        assert_eq!(reservoir_lead(1, 511, main), 0);
        assert_eq!(reservoir_lead(8, 511, main), 0);
        assert_eq!(reservoir_lead(12, 511, main), 4);
        assert_eq!(reservoir_lead(12, 50, main), 9);
        assert_eq!(reservoir_lead(12, 0, main), 10);
    }
}
