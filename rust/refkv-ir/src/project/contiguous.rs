//! The chunks of contiguous values (conventions §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#7-source-values-as-arrays>), as
//! the projections that cut them with `out::grid_chunks` write them (NIfTI, MRC, FITS): the copy
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
            Chunk::Ranges(mut parts) => {
                if let Some(n) = pad {
                    parts.pop();
                    parts.push(zeros.range(&mut refs.out, n)?);
                }
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
