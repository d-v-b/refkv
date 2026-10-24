//! The bytes a sans-IO parser was fed, held until the step that uses them.

use std::collections::BTreeMap;

/// What a feed does to the bytes already held where it overlaps or touches them. A parser is
/// fed one file's bytes, so overlapping feeds agree; the mode decides which ranges a read can
/// find its bytes in, and how many bytes are held.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Overlap {
    /// Ranges kept apart; of two feeds at one offset, the longer is kept.
    #[default]
    Longer,
    /// Ranges kept apart; a feed replaces the one at its offset.
    Replace,
    /// Touching and overlapping ranges merged into one; the new bytes win.
    MergeNew,
    /// Touching and overlapping ranges merged into one; a feed that starts within (or right
    /// after) a held range only extends it, and wins over the ranges that start within it.
    MergeKeep,
    /// One contiguous run: a feed at its end extends it, one within it (or at its start and
    /// at least as long) replaces its bytes from there on, and any other replaces the run.
    Run,
}

/// The bytes a parser was fed, as ranges of the file.
#[derive(Default)]
pub struct Fed {
    got: BTreeMap<u64, Vec<u8>>,
    held: u64,
    /// the longest range held since the last `clear` (ranges kept apart: how far back a read
    /// looks for a range that holds its bytes)
    longest: u64,
    mode: Overlap,
}

impl Fed {
    pub fn new(mode: Overlap) -> Fed {
        Fed { mode, ..Fed::default() }
    }

    pub fn feed(&mut self, offset: u64, data: Vec<u8>) {
        match self.mode {
            Overlap::Longer => match self.got.get(&offset) {
                Some(b) if b.len() >= data.len() => {}
                _ => self.put(offset, data),
            },
            Overlap::Replace => self.put(offset, data),
            Overlap::MergeNew | Overlap::MergeKeep => self.merge(offset, data),
            Overlap::Run => self.extend_run(offset, data),
        }
    }

    fn put(&mut self, o: u64, data: Vec<u8>) {
        self.held += data.len() as u64;
        self.longest = self.longest.max(data.len() as u64);
        if let Some(old) = self.got.insert(o, data) {
            self.held -= old.len() as u64;
        }
    }

    fn take_range(&mut self, s: u64) -> Vec<u8> {
        let b = self.got.remove(&s).unwrap_or_default();
        self.held -= b.len() as u64;
        b
    }

    fn merge(&mut self, o: u64, data: Vec<u8>) {
        if data.is_empty() {
            return;
        }
        let end = o + data.len() as u64;
        // the held ranges that touch [o, end), from the last
        let touch: Vec<(u64, u64)> = self
            .got
            .range(..=end)
            .rev()
            .map(|(&s, b)| (s, s + b.len() as u64))
            .take_while(|&(_, e)| e >= o)
            .collect();
        if let [(s, e)] = touch[..] {
            if s <= o && end <= e {
                return; // already held
            }
        }
        let lo = touch.last().map_or(o, |r| r.0.min(o));
        let hi = touch.first().map_or(end, |r| r.1.max(end));
        let mut buf = vec![0u8; (hi - lo) as usize];
        let mut paint = |at: u64, b: &[u8]| buf[(at - lo) as usize..(at - lo) as usize + b.len()].copy_from_slice(b);
        // the held ranges the new bytes win over, the new bytes, then the held ranges that win
        let keeps = |s: u64| self.mode == Overlap::MergeKeep && s <= o;
        let (over, under): (Vec<(u64, u64)>, Vec<(u64, u64)>) = touch.iter().partition(|r| keeps(r.0));
        for &(s, _) in &under {
            paint(s, &self.take_range(s));
        }
        paint(o, &data);
        for &(s, _) in &over {
            paint(s, &self.take_range(s));
        }
        self.put(lo, buf);
    }

    fn extend_run(&mut self, o: u64, data: Vec<u8>) {
        let Some((&s, b)) = self.got.iter().next() else {
            if !data.is_empty() {
                self.put(o, data);
            }
            return;
        };
        let e = s + b.len() as u64;
        let (lo, mut run) = if o == e {
            (s, self.take_range(s))
        } else if o == s && data.len() >= b.len() {
            self.clear();
            (o, Vec::new())
        } else if o > s && o < e {
            let mut run = self.take_range(s);
            run.truncate((o - s) as usize);
            (s, run)
        } else {
            self.clear();
            (o, Vec::new())
        };
        run.extend_from_slice(&data);
        if !run.is_empty() {
            self.put(lo, run);
        }
    }

    /// The held range that starts at or before `o` and runs past it, if any, as (start, bytes).
    fn holding(&self, o: u64, end: u64) -> Option<(u64, &[u8])> {
        let mut before = self.got.range(..=o).rev();
        if !matches!(self.mode, Overlap::Longer | Overlap::Replace) {
            // merged ranges are disjoint: only the last one at or before o can hold it
            let (&s, b) = before.next()?;
            return (end <= s + b.len() as u64).then_some((s, b.as_slice()));
        }
        for (&s, b) in before {
            if end <= s + b.len() as u64 {
                return Some((s, b));
            }
            if s.saturating_add(self.longest) < end {
                break; // no range that starts earlier is that long
            }
        }
        None
    }

    /// The bytes [o, o + n), when one fed range holds them.
    pub fn get(&self, o: u64, n: u64) -> Option<&[u8]> {
        let end = o.checked_add(n)?;
        let (s, b) = self.holding(o, end)?;
        Some(&b[(o - s) as usize..(end - s) as usize])
    }

    pub fn has(&self, o: u64, n: u64) -> bool {
        self.get(o, n).is_some()
    }

    /// Where the fed range holding `o` ends (o itself when none does).
    pub fn reach(&self, o: u64) -> u64 {
        match o.checked_add(1).and_then(|e| self.holding(o, e)) {
            Some((s, b)) => s + b.len() as u64,
            None => o,
        }
    }

    /// The fed bytes from `o` to the end of the range holding `o` (none when no range does).
    pub fn from(&self, o: u64) -> &[u8] {
        match o.checked_add(1).and_then(|e| self.holding(o, e)) {
            Some((s, b)) => &b[(o - s) as usize..],
            None => &[],
        }
    }

    /// Keeps only the bytes `from(o)` gives: the range holding `o`, from `o` on.
    pub fn keep_from(&mut self, o: u64) {
        let s = o.checked_add(1).and_then(|e| self.holding(o, e)).map(|r| r.0);
        let kept = s.map(|s| self.take_range(s));
        self.clear();
        if let (Some(s), Some(mut b)) = (s, kept) {
            b.drain(..(o - s) as usize);
            self.put(o, b);
        }
    }

    /// The range fed at `o`, taken out.
    pub fn take(&mut self, o: u64) -> Option<Vec<u8>> {
        self.got.contains_key(&o).then(|| self.take_range(o))
    }

    pub fn clear(&mut self) {
        self.got.clear();
        self.held = 0;
        self.longest = 0;
    }

    /// The bytes held.
    pub fn held(&self) -> u64 {
        self.held
    }

    /// The number of ranges held.
    pub fn ranges(&self) -> usize {
        self.got.len()
    }
}

/// A number of fixed width, read from bytes in either byte order: `u32::le_at(b, at)`,
/// `i16::be_at(b, at)`, `u64::at(b, at, little)`, and `u16::try_at(b, at, little)`, None past the
/// end of `b`. The unchecked forms panic past the end of `b`.
pub trait Word: Sized {
    const SIZE: usize;
    /// The number of `b`, exactly `SIZE` bytes.
    fn of(b: &[u8], little: bool) -> Self;

    fn at(b: &[u8], at: usize, little: bool) -> Self {
        Self::of(&b[at..at + Self::SIZE], little)
    }
    fn le_at(b: &[u8], at: usize) -> Self {
        Self::at(b, at, true)
    }
    fn be_at(b: &[u8], at: usize) -> Self {
        Self::at(b, at, false)
    }
    fn try_at(b: &[u8], at: usize, little: bool) -> Option<Self> {
        b.get(at..at.checked_add(Self::SIZE)?).map(|a| Self::of(a, little))
    }
}

macro_rules! word {
    ($($t:ty),*) => {$(
        impl Word for $t {
            const SIZE: usize = size_of::<$t>();
            fn of(b: &[u8], little: bool) -> Self {
                let a = b.try_into().unwrap();
                if little { <$t>::from_le_bytes(a) } else { <$t>::from_be_bytes(a) }
            }
        }
    )*};
}
word!(u8, i8, u16, i16, u32, i32, u64, i64, f32, f64);

/// The unsigned integer of the `n` bytes at `at` of `b` (n at most 8), little-endian if
/// `little`; panics past the end of `b`.
pub fn uint(b: &[u8], at: usize, n: usize, little: bool) -> u64 {
    let b = &b[at..at + n];
    if little { b.iter().rev().fold(0, |a, &c| a << 8 | c as u64) } else { b.iter().fold(0, |a, &c| a << 8 | c as u64) }
}

/// `uint`, or None past the end of `b`.
pub fn try_uint(b: &[u8], at: usize, n: usize, little: bool) -> Option<u64> {
    (at.checked_add(n)? <= b.len()).then(|| uint(b, at, n, little))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// (start, bytes) of every range held.
    fn held(f: &Fed) -> Vec<(u64, Vec<u8>)> {
        f.got.iter().map(|(&s, b)| (s, b.clone())).collect()
    }

    #[test]
    fn feeds_by_overlap_mode() {
        // feeds whose bytes differ where they overlap, so that the winner shows
        let feeds: [(u64, &[u8]); 4] = [(10, &[1, 1, 1, 1]), (12, &[2, 2, 2, 2]), (10, &[3, 3]), (20, &[4])];
        let cases: [(Overlap, Vec<(u64, Vec<u8>)>); 5] = [
            (Overlap::Longer, vec![(10, vec![1, 1, 1, 1]), (12, vec![2, 2, 2, 2]), (20, vec![4])]),
            (Overlap::Replace, vec![(10, vec![3, 3]), (12, vec![2, 2, 2, 2]), (20, vec![4])]),
            (Overlap::MergeNew, vec![(10, vec![1, 1, 2, 2, 2, 2]), (20, vec![4])]),
            (Overlap::MergeKeep, vec![(10, vec![1, 1, 1, 1, 2, 2]), (20, vec![4])]),
            (Overlap::Run, vec![(20, vec![4])]),
        ];
        for (mode, want) in cases {
            let mut f = Fed::new(mode);
            for (o, b) in feeds {
                f.feed(o, b.to_vec());
            }
            assert_eq!(held(&f), want, "{mode:?}");
            assert_eq!(f.held(), want.iter().map(|r| r.1.len() as u64).sum::<u64>(), "{mode:?}");
        }
        // merged: a feed that starts within a held range and runs past it, and one that
        // starts before a held range and ends within it
        for (mode, want) in [(Overlap::MergeNew, vec![5, 5, 6, 6, 6]), (Overlap::MergeKeep, vec![5, 5, 1, 1, 6])] {
            let mut f = Fed::new(mode);
            f.feed(10, vec![1; 3]);
            f.feed(11, vec![6; 3]);
            f.feed(9, vec![5; 2]);
            assert_eq!(held(&f), vec![(9, want)], "{mode:?}");
        }
        // a run: extended at its end, replaced from a feed within it on
        let mut f = Fed::new(Overlap::Run);
        f.feed(10, vec![1; 4]);
        f.feed(14, vec![2; 2]);
        f.feed(12, vec![3]);
        assert_eq!(held(&f), vec![(10, vec![1, 1, 3])]);
        // reads: a range kept apart is found behind a later, shorter one
        let mut f = Fed::default();
        f.feed(0, vec![7; 100]);
        f.feed(40, vec![8; 2]);
        assert_eq!(f.get(50, 10), Some(&[7u8; 10][..]));
        assert_eq!(f.get(41, 1), Some(&[8u8][..]));
        assert_eq!(f.get(95, 10), None);
        assert_eq!((f.reach(50), f.reach(100), f.from(98)), (100, 100, &[7u8, 7][..]));
        f.keep_from(98);
        assert_eq!(held(&f), vec![(98, vec![7, 7])]);
        assert_eq!(f.take(98), Some(vec![7, 7]));
        assert_eq!((f.ranges(), f.held()), (0, 0));
    }
}
