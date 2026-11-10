//! The stored arrays of an ND2 file (spec/stored-array.md §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/stored-array.md#7-the-formats>): one array of
//! the frames the experiment's loops count, keyed by the `ImageAttributesLV!` chunk, a frame a
//! unit, or, where its rows are padded, a row a unit; and an array for each frame chunk the map
//! names past them, keyed by `frames`. Built from the finished IR, whose frame elements say which
//! frames the map holds, and from what the parser read of the attributes.

use crate::array::{AxisUnits, Chain, ChunkMap, Codec, Edge, Fill, Grid, Place, Row, StoredArray};
use crate::fed::Word;
use crate::ir::{Ir, Res, NO_INDEX, STRUCT};
use std::collections::BTreeMap;

/// How `ImageAttributesLV!` says a frame is stored.
pub(super) struct Frame {
    pub height: u64,
    pub width: u64,
    pub components: u64,
    /// the bits of a component (`uiBpcInMemory`)
    pub bits: u64,
    /// the bytes from a row's start to the next's (`uiWidthBytes`)
    pub width_bytes: u64,
    /// the bytes of a row's components
    pub row: u64,
    pub compressed: bool,
}

/// A frame chunk at or past the frames the loops count: its frame number and, where the IR holds
/// its data, the `data` element, the member of its run it is in, and the data length its header
/// gives.
type Beyond = (u64, Option<(u32, Option<u64>, u64)>);

impl Frame {
    /// Whether a row's bytes end before the next row's start.
    fn padded(&self) -> bool {
        !self.compressed && self.width_bytes != self.row
    }

    /// The units a frame is cut into: its rows, each apart from the next, where they are padded.
    fn units(&self) -> u64 {
        if self.padded() { self.height } else { 1 }
    }

    /// A frame's axes: its rows, its columns and, of more than one, its components.
    fn shape(&self) -> Vec<u64> {
        let mut shape = vec![self.height, self.width];
        if self.components > 1 {
            shape.push(self.components);
        }
        shape
    }

    /// A frame's grid, after the axes `before` it: a frame a unit, or a row a unit.
    fn grid(&self, before: &[u64]) -> Grid {
        let rows = if self.padded() { 1 } else { self.height };
        let mut axes: Vec<AxisUnits> = before.iter().map(|&n| AxisUnits::Each(n)).collect();
        axes.extend([AxisUnits::Each(rows), AxisUnits::Each(self.width)]);
        if self.components > 1 {
            axes.push(AxisUnits::Each(self.components));
        }
        Grid { axes, edge: Edge::Short }
    }

    /// The first unit of a frame whose bytes start `base` bytes into an element of `len` bytes,
    /// as (offset, length) within the element: the zlib stream, which runs to the element's end;
    /// the frame's rows together; or, of padded rows, the first row, each later one `width_bytes`
    /// after the one before. None where the element does not hold the frame's rows.
    fn first(&self, len: u64, base: u64) -> Option<(u64, u64)> {
        if self.compressed {
            return (len > base).then(|| (base, len - base));
        }
        // the last row's padding need not lie in the element
        let rows = (self.height.checked_sub(1)?).checked_mul(self.width_bytes)?.checked_add(self.row)?;
        (base.checked_add(rows)? <= len).then_some((base, if self.padded() { self.row } else { rows }))
    }

    /// The rows of the array `array` for one frame, whose first unit is `first` of the element
    /// `element` (in member `member` of its run) of `len` bytes: `before` are the frame's
    /// coordinates before its rows'.
    fn rows(&self, map: &mut ChunkMap, array: u32, before: &[u64], (element, member, len): (u32, Option<u64>, u64), first: (u64, u64)) -> Res<()> {
        for r in 0..self.units() {
            let at = (first.0 + r * if self.padded() { self.width_bytes } else { 0 }, first.1);
            let mut coords = before.to_vec();
            coords.extend([r, 0]);
            if self.components > 1 {
                coords.push(0);
            }
            // a unit that is all of its element is the element alone: its recipe is then the
            // identity (a stream, unpadded rows, or one row with no padding in the file)
            let range = (at != (0, len)).then_some(at);
            map.push(Row { array, coords, element: Some(element), member, range, ..Row::default() })?;
        }
        Ok(())
    }
}

/// The entries of the chunk map that `frames` frames' rows take, each of `rank` coordinates.
fn weight(frame: &Frame, frames: u64, rank: usize) -> Option<u64> {
    frames.checked_mul(frame.units())?.checked_mul(1 + rank as u64)
}

/// The descriptions of the file's frames, their chains and their rows, and the key of the arrays
/// past the loops' count with how many they are. `key` is the `ImageAttributesLV!` chunk's
/// struct, `declarations` the elements that declare the samples, and `count` the frames the
/// loops count.
pub(super) fn described(ir: &Ir, frame: &Frame, count: u64, key: u32, declarations: &[u32]) -> Res<(Vec<StoredArray>, Vec<Chain>, ChunkMap, (u32, u64))> {
    let named = |i: u32, name: &str| ir.children(i).iter().copied().find(|&c| ir.nidx[c as usize] == NO_INDEX && ir.names.get(ir.name[c as usize]) == name);
    let frames = named(0, "frames").ok_or("internal: an ND2 IR without frames")?;
    // the frame chunks the map names, as the finished IR holds them: a frame the loops count has
    // `pixels`, any other `data`, or nothing where its data is not in the file
    let mut placed: Vec<(u64, u32, Option<u64>)> = Vec::new();
    let mut beyond: Vec<Beyond> = Vec::new();
    for &c in ir.children(frames) {
        if ir.kind[c as usize] != STRUCT || ir.nidx[c as usize] == NO_INDEX {
            continue;
        }
        // a run's members are the frames after its first, alike but for their place
        let run = ir.run(c);
        let (pixels, data) = (named(c, "pixels"), named(c, "data"));
        let declared = named(c, "header").and_then(|h| ir.value_bytes(h)).filter(|h| h.len() == 16).map(|h| u64::le_at(h, 8));
        for j in 0..run.map_or(1, |r| r.0) {
            let Some(f) = ir.nidx[c as usize].checked_add(j) else { break };
            let member = run.map(|_| j);
            match pixels {
                Some(px) => placed.push((f, px, member)),
                None => beyond.push((f, data.zip(declared).map(|(e, d)| (e, member, d)))),
            }
        }
    }
    beyond.sort_by_key(|b| b.0);
    let place = |element: u32| Place::Element { element, member: None, range: None };
    let within = frame.shape();
    let array = |element: u32, index: Option<u64>, shape: Vec<u64>| StoredArray {
        element,
        index,
        declarations: declarations.iter().map(|&e| place(e)).collect(),
        shape: Some(shape),
        axes: None,
        item_bits: Some(frame.bits),
        pad_from: None,
        grid: None,
        chains: Vec::new(),
        fill: Fill::Undefined,
        unit_parameters: Vec::new(),
    };
    // one chain for every frame: none, or the zlib stream a compressed frame is
    let chain: Chain = if frame.compressed { vec![Codec { name: "zlib".into(), parameters: BTreeMap::new() }] } else { Vec::new() };
    let mut chains: Vec<Chain> = Vec::new();
    let mut arrays: Vec<StoredArray> = Vec::new();
    let mut map = ChunkMap::default();
    // what remains of the chunk map's limit, as describe fits the arrays to it, in this order: an
    // array whose rows would pass it has no grid, and its rows, which may be a row of the image
    // each for frames that share their bytes, are never made
    let mut room = ir.budget.as_ref().map_or(u64::MAX, |b| b.rows.saturating_sub(b.spent_rows));
    let mut fits = |frames: u64, rank: usize| match weight(frame, frames, rank) {
        Some(w) if w <= room => {
            room -= w;
            true
        }
        _ => false,
    };
    // the frames the loops count: the first axis, then a frame's
    let mut a = array(key, None, std::iter::once(count).chain(within.iter().copied()).collect());
    if fits(placed.len() as u64, 1 + within.len()) {
        a.grid = Some(frame.grid(&[1]));
        a.chains = vec![0];
        chains.push(chain.clone());
        for &(f, px, member) in &placed {
            let len = ir.len[px as usize];
            let first = frame.first(len, 0).ok_or_else(|| format!("internal: frame {f}'s pixels do not hold its rows"))?;
            frame.rows(&mut map, 0, &[f], (px, member, len), first)?;
        }
    }
    arrays.push(a);
    // each frame chunk past them: an array of one frame, where its chunk holds one as the
    // attributes say (the timestamp's 8 bytes, then the rows or the stream, within the data
    // length its header gives and within the file)
    for (index, &(_, data)) in beyond.iter().enumerate() {
        let mut a = array(frames, Some(index as u64), within.clone());
        let held = data.and_then(|(e, member, declared)| {
            let len = ir.len[e as usize];
            let whole = if frame.compressed { len == declared } else { frame.height.checked_mul(frame.width_bytes)?.checked_add(8)? <= declared };
            Some(((e, member, len), frame.first(len, 8).filter(|_| whole)?))
        });
        if let Some((element, first)) = held.filter(|_| fits(1, within.len())) {
            if chains.is_empty() {
                chains.push(chain.clone());
            }
            a.grid = Some(frame.grid(&[]));
            a.chains = vec![0];
            frame.rows(&mut map, arrays.len() as u32, &[], element, first)?;
        }
        arrays.push(a);
    }
    Ok((arrays, chains, map, (frames, beyond.len() as u64)))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn frame(height: u64, width_bytes: u64, row: u64, compressed: bool) -> Frame {
        Frame { height, width: row, components: 1, bits: 8, width_bytes, row, compressed }
    }

    /// The first unit of a frame in an element: unpadded rows together, a padded row alone, and a
    /// stream to the element's end, each after the bytes before the frame's.
    #[test]
    fn a_frames_first_unit_is_its_rows_its_first_row_or_its_stream() {
        assert_eq!(frame(4, 5, 5, false).first(20, 0), Some((0, 20)));
        assert_eq!(frame(4, 5, 5, false).first(40, 8), Some((8, 20)));
        // the last row's padding need not be held
        assert_eq!(frame(4, 8, 5, false).first(29, 0), Some((0, 5)));
        assert_eq!(frame(4, 8, 5, false).first(37, 8), Some((8, 5)));
        assert_eq!(frame(4, 5, 5, true).first(30, 8), Some((8, 22)));
    }

    #[test]
    fn an_element_too_short_for_the_rows_holds_no_frame() {
        assert_eq!(frame(4, 5, 5, false).first(19, 0), None);
        assert_eq!(frame(4, 8, 5, false).first(28, 0), None);
        assert_eq!(frame(4, 8, 5, false).first(36, 8), None);
    }

    #[test]
    fn an_element_of_no_stream_holds_no_frame() {
        assert_eq!(frame(4, 5, 5, true).first(8, 8), None);
    }

    #[test]
    fn rows_past_what_a_file_can_hold_hold_no_frame() {
        assert_eq!(frame(u64::MAX, u64::MAX, 5, false).first(u64::MAX, 8), None);
        assert_eq!(weight(&frame(u64::MAX, 8, 5, false), u64::MAX, 3), None);
    }
}
