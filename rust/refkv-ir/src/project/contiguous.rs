//! The chunks of contiguous values (conventions §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#7-source-values-as-arrays>), as
//! the projections that cut them with `out::grid_chunks` write them (NIfTI, MRC, FITS, and NRRD's
//! store run, through `padded`): the copy
//! threshold of a run's facts, the source's ranges of the edge chunks a projection copies, and
//! the chunks' references, copies and bytes. A reference's zero padding is a range of the
//! output's zero data source (spec/zarrify.md §1.2
//! <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>), not a literal.

use super::{key, Copied};
use crate::out::{Chunk, Out, Part, MAX_PAYLOAD};
use crate::refs::Refs;
use serde_json::Value as J;

/// The payload past which an edge chunk is copied: the facts' `copy_threshold` (the run's,
/// `out::copy_threshold`), else MAX_PAYLOAD.
pub fn threshold(facts: &J) -> usize {
    facts["copy_threshold"].as_u64().map_or(MAX_PAYLOAD, |t| t as usize)
}

/// The source's ranges of the chunks a projection copies (`Chunk::Copy`), which the run reads
/// after the parser.
pub fn copy_reads(chunks: &[(Vec<u64>, Chunk)]) -> impl Iterator<Item = (u64, u64)> + '_ {
    chunks.iter().filter_map(|(_, k)| match k {
        Chunk::Copy(o, n, _) => Some((*o, *n)),
        _ => None,
    })
}

/// The zero padding of a chunk's reference: its literal of zero bytes after its range, as
/// `out::grid_chunks` and `out::blob_chunks` pad an edge chunk.
fn padding_of(chunk: &Chunk) -> Option<u64> {
    match chunk {
        Chunk::Ranges(parts) => match parts.as_slice() {
            [Part::Src(..), Part::Lit(z)] if z.iter().all(|&b| b == 0) => Some(z.len() as u64),
            _ => None,
        },
        _ => None,
    }
}

/// The output's zero data source: as many zero bytes as the most padding a reference of its
/// chunks holds, each padded reference naming a prefix; added at its first use.
pub struct Zeros {
    size: u64,
    index: Option<u32>,
}

impl Zeros {
    /// The zero data source of an output of `chunks` (every array's).
    pub fn of<'a>(chunks: impl IntoIterator<Item = &'a (Vec<u64>, Chunk)>) -> Zeros {
        Zeros { size: chunks.into_iter().filter_map(|(_, c)| padding_of(c)).max().unwrap_or(0), index: None }
    }

    /// The range of its first `n` bytes, the source added to `out` at its first use.
    fn range(&mut self, out: &mut Out, n: u64) -> Result<Part, String> {
        if n > self.size {
            return Err("internal: a chunk's padding is past the zero data source".into());
        }
        let i = match self.index {
            Some(i) => i,
            None => {
                let Part::Data(i, ..) = out.shared(&vec![0; self.size as usize]) else {
                    return Err("internal: a shared range of no data source".into());
                };
                *self.index.insert(i)
            }
        };
        Ok(Part::Data(i, 0, n))
    }
}

/// A chunk's reference `parts` with its zero padding, a literal of zero bytes after its range
/// (`padding`), as a range of the output's zero data source `zeros`; any other `parts` as they are.
pub fn padded(parts: Vec<Part>, padding: Option<u64>, zeros: &mut Zeros, out: &mut Out) -> Result<Vec<Part>, String> {
    let Some(n) = padding else { return Ok(parts) };
    let mut parts = parts;
    parts.pop();
    parts.push(zeros.range(out, n)?);
    Ok(parts)
}

/// The zero padding of a chunk's reference (`padding_of`), for a projection that writes its own
/// references, as NRRD's store run does.
pub fn padding(chunk: &Chunk) -> Option<u64> {
    padding_of(chunk)
}

/// Writes `chunks`, cut from the element `(start, length, form)`: the i-th at the key of each of
/// `prefixes` and `coords` of its coordinates (`project::key`). A chunk of ranges is a reference,
/// charged to the budget as piece i at the first key and the same ranges, not charged, at the
/// others, its zero padding a range of the output's zero data source `zeros`; a copied chunk is
/// its bytes of `copied` then its zero padding, and a chunk of bytes its bytes, at every key.
pub fn emit(refs: &mut Refs, chunks: Vec<(Vec<u64>, Chunk)>, (start, length, form): (u64, u64, u32), copied: &Copied,
            prefixes: &[&str], coords: impl Fn(Vec<u64>) -> Vec<u64>, zeros: &mut Zeros) -> Result<(), String> {
    for (piece, (c, chunk)) in chunks.into_iter().enumerate() {
        let c = coords(c);
        let keys: Vec<String> = prefixes.iter().map(|p| key(p, c.iter().copied())).collect();
        let Some((first, rest)) = keys.split_first() else { return Err("internal: a chunk without a key".into()) };
        let pad = padding_of(&chunk);
        let bytes = match chunk {
            Chunk::Ranges(parts) => {
                let parts = padded(parts, pad, zeros, &mut refs.out)?;
                let same = (!rest.is_empty()).then(|| parts.clone());
                refs.chunk(first.clone(), start, length, form, Some(parts), piece as u64)?;
                for k in rest {
                    refs.out.refs(k, same.clone().unwrap_or_default());
                }
                continue;
            }
            // an edge chunk whose padding does not fit in a reference: its bytes, then the padding
            Chunk::Copy(o, len, pad) => {
                let mut b = copied
                    .get(&(o, len))
                    .ok_or("a copied chunk's bytes were not read: project a run's IR with the bytes its last batch read")?
                    .clone();
                b.resize(b.len() + pad as usize, 0);
                b
            }
            Chunk::Bytes(b) => b,
        };
        let (last, before) = keys.split_last().unwrap_or((first, &[]));
        for k in before {
            refs.out.bytes(k, bytes.clone());
        }
        refs.out.bytes(last, bytes);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ir::Ir;

    /// Two arrays' chunks, written by two calls with one `Zeros`, the first at two keys: each
    /// zero padding is a prefix of the one data source, of the larger padding, at every key; a
    /// literal of other bytes stays a literal.
    #[test]
    fn padding_is_a_prefix_of_one_zero_data_source() {
        let ir = Ir::new(100);
        let mut refs = Refs::new(&ir, Out::new("u"));
        let a = vec![(vec![0], Chunk::Ranges(vec![Part::Src(0, 8)])), (vec![1], Chunk::Ranges(vec![Part::Src(8, 2), Part::Lit(vec![0; 6])]))];
        let b = vec![(vec![0], Chunk::Ranges(vec![Part::Src(10, 4), Part::Lit(vec![0; 2])])), (vec![1], Chunk::Ranges(vec![Part::Src(14, 4), Part::Lit(vec![7])]))];
        let mut zeros = Zeros::of(a.iter().chain(&b));
        emit(&mut refs, a, (0, 10, 0), &Copied::new(), &["a/c/", "d/c/"], |c| c, &mut zeros).unwrap();
        emit(&mut refs, b, (10, 8, 0), &Copied::new(), &["b/c/"], |c| c, &mut zeros).unwrap();
        let got: Vec<(&str, &[Part])> = refs.out.refs.iter().map(|(k, p)| (k.as_str(), p.as_slice())).collect();
        assert_eq!(got, vec![
            ("a/c/0", &[Part::Src(0, 8)][..]),
            ("d/c/0", &[Part::Src(0, 8)][..]),
            ("a/c/1", &[Part::Src(8, 2), Part::Data(1, 0, 6)][..]),
            ("d/c/1", &[Part::Src(8, 2), Part::Data(1, 0, 6)][..]),
            ("b/c/0", &[Part::Src(10, 4), Part::Data(1, 0, 2)][..]),
            ("b/c/1", &[Part::Src(14, 4), Part::Lit(vec![7])][..]),
        ]);
        assert_eq!(refs.out.data, vec![vec![0; 6]]);
    }

    /// A padding past the zero data source, of chunks `Zeros` was not made from, is an error,
    /// and adds no source.
    #[test]
    fn rejects_a_padding_past_the_zero_data_source() {
        let ir = Ir::new(100);
        let mut refs = Refs::new(&ir, Out::new("u"));
        let mut zeros = Zeros::of(&[(vec![0], Chunk::Ranges(vec![Part::Src(0, 2), Part::Lit(vec![0; 2])]))]);
        let wider = vec![(vec![0], Chunk::Ranges(vec![Part::Src(0, 2), Part::Lit(vec![0; 6])]))];
        let got = emit(&mut refs, wider, (0, 2, 0), &Copied::new(), &["a/c/"], |c| c, &mut zeros);
        assert_eq!(got, Err("internal: a chunk's padding is past the zero data source".into()));
        assert!(refs.out.data.is_empty());
    }
}
