//! The source table a writer builds, and the writer requirements of spec/refkv.md §7
//! <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#7-writing> that concern it: shared by
//! this crate's writer and rust/refkv-phkv's, so that the two refuse the same stores with the
//! same errors.

use crate::pb::{self, Range, Source, SourceKind};
use crate::writer::{invalid, SPEC_REVISION};
use crate::Error;
use std::collections::HashMap;

/// A source of the table: one interned, or one added in bulk (url, size pin).
enum Slot {
    Source(Source),
    Bulk(String, Option<u64>),
}

impl Slot {
    fn url(&self) -> Option<&str> {
        match self {
            Slot::Source(s) => s.url_str(),
            Slot::Bulk(u, _) => Some(u),
        }
    }
}

/// What [`SourceTable::check`] needs to know of the store's keys.
pub trait Keys {
    /// Whether the store has `key`, a bytes entry or a reference.
    fn has(&self, key: &str) -> bool;
    /// Whether `key` is a reference.
    fn is_ref(&self, key: &str) -> bool;
    /// The size a range of a `key` source is checked against, if any.
    fn size(&self, key: &str) -> Option<u64>;
}

/// The sources a writer has been given, in index order, and the ranges it checks at close.
#[derive(Default)]
pub struct SourceTable {
    slots: Vec<Slot>,
    interned: HashMap<Source, u32>,
    /// the ranges of the sources that are not url sources, for the bounds checks of spec/refkv.md
    /// §7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#7-writing>
    range_checks: Vec<(String, Range)>,
}

impl SourceTable {
    /// Interns `src`, and gives its index for `Range::source`: equal sources share one.
    pub fn add(&mut self, src: Source) -> Result<u32, Error> {
        if let Some(&i) = self.interned.get(&src) {
            return Ok(i);
        }
        src.check().map_err(Error::Invalid)?;
        let i = self.next_index()?;
        self.interned.insert(src.clone(), i);
        self.slots.push(Slot::Source(src));
        Ok(i)
    }

    /// Adds a url source without interning it, for the bulk path of many objects; `size` pins its
    /// size. The url is checked at [`Self::check`].
    pub fn add_bulk(&mut self, url: String, size: Option<u64>) -> Result<u32, Error> {
        let i = self.next_index()?;
        self.slots.push(Slot::Bulk(url, size));
        Ok(i)
    }

    fn next_index(&self) -> Result<u32, Error> {
        u32::try_from(self.slots.len()).or_else(|_| invalid("more than 2^32 sources"))
    }

    pub fn len(&self) -> usize {
        self.slots.len()
    }

    pub fn is_empty(&self) -> bool {
        self.slots.is_empty()
    }

    /// The checks of a reference's `ranges` that need no other key. The writer calls
    /// [`Self::keep`] once it has taken the reference.
    pub fn check_ranges(&self, key: &str, ranges: &[Range]) -> Result<(), Error> {
        for r in ranges {
            if r.data.is_none() && r.source as usize >= self.slots.len() {
                return invalid(format!("range of {key:?} uses unregistered source {}", r.source));
            }
            if r.data.is_some() && r.crc32c.is_some() {
                return invalid(format!("literal range of {key:?} with a crc32c (spec/refkv.md §7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#7-writing>)"));
            }
            if r.offset.checked_add(r.length).is_none() {
                return invalid(format!("a range of {key:?}: offset + length exceeds 2^64 - 1"));
            }
        }
        // spec/refkv.md §7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#7-writing> (which python/src/refkv/vzip/archive.py does not check)
        if ranges.iter().try_fold(0u64, |n, r| n.checked_add(r.size())).is_none() {
            return invalid(format!("the reference {key:?} exceeds 2^64 - 1 bytes"));
        }
        Ok(())
    }

    /// Keeps the ranges of a reference the writer has taken, for the bounds checks at close. url
    /// sources are not bounds-checked (spec/refkv.md §7
    /// <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#7-writing>): only the others are kept.
    pub fn keep(&mut self, key: &str, ranges: &[Range]) {
        for r in ranges {
            if r.data.is_none() && self.slots[r.source as usize].url().is_none() {
                self.range_checks.push((key.to_string(), r.clone()));
            }
        }
    }

    /// The checks that need every key: url sources are valid URI references, `key` sources name
    /// bytes entries the store has (and none of `reserved`), and the ranges of `key` and `data`
    /// sources lie within them.
    pub fn check(&self, keys: &impl Keys, reserved: &[&str]) -> Result<(), Error> {
        let bad: Vec<&str> = self
            .slots
            .iter()
            .filter_map(|s| match s {
                Slot::Bulk(u, _) if u.is_empty() || !crate::uri::is_uri_reference(u) => Some(u.as_str()),
                _ => None,
            })
            .take(5)
            .collect();
        if !bad.is_empty() {
            return invalid(format!("empty or invalid url sources: {bad:?}"));
        }
        let source = |i: usize| match &self.slots[i] {
            Slot::Source(s) => Some(s),
            Slot::Bulk(..) => None, // a url source
        };
        if self.slots.iter().any(|s| matches!(s, Slot::Source(Source { kind: SourceKind::Key(k), .. }) if k.is_empty())) {
            return invalid("empty key source");
        }
        for (key, r) in &self.range_checks {
            let n = match source(r.source as usize).map(|s| &s.kind) {
                Some(SourceKind::Data(d)) => Some(d.len() as u64),
                Some(SourceKind::Key(k)) => keys.size(k),
                _ => None,
            };
            if let Some(n) = n
                && r.offset.checked_add(r.length).is_none_or(|e| e > n)
            {
                return invalid(format!(
                    "{key:?}: range [{}, {}) is past the end of source {} ({n} bytes)",
                    r.offset,
                    r.offset as u128 + r.length as u128,
                    r.source
                ));
            }
        }
        let key_sources = || {
            self.slots.iter().filter_map(|s| match s {
                Slot::Source(Source { kind: SourceKind::Key(k), .. }) => Some(k.as_str()),
                _ => None,
            })
        };
        let mut missing: Vec<&str> = key_sources().filter(|k| !keys.has(k)).collect();
        missing.sort_unstable();
        if !missing.is_empty() {
            return invalid(format!("internal references to missing entries: {missing:?}"));
        }
        let mut format: Vec<&str> = key_sources().filter(|k| reserved.contains(k)).collect();
        format.sort_unstable();
        if !format.is_empty() {
            return invalid(format!("key sources naming format entries: {format:?}"));
        }
        let bad: Vec<&str> = self
            .slots
            .iter()
            .filter_map(|s| match s {
                Slot::Source(s) => s.url_str().filter(|u| u.is_empty() || !crate::uri::is_uri_reference(u)),
                Slot::Bulk(..) => None,
            })
            .collect();
        if !bad.is_empty() {
            return invalid(format!("empty or invalid url sources: {bad:?}"));
        }
        let mut to_refs: Vec<&str> = key_sources().filter(|k| keys.is_ref(k)).collect();
        to_refs.sort_unstable();
        if !to_refs.is_empty() {
            return invalid(format!("internal references to reference entries: {to_refs:?}"));
        }
        Ok(())
    }

    /// The sources, in index order.
    pub fn sources(&self) -> Vec<Source> {
        self.slots
            .iter()
            .map(|s| match s {
                Slot::Source(s) => s.clone(),
                Slot::Bulk(u, size) => Source { size: *size, ..Source::url(u.as_str()) },
            })
            .collect()
    }

    /// The source table message (spec/refkv.md §5
    /// <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#5-source-table>), encoded.
    pub fn encode(&self) -> Vec<u8> {
        pb::encode_source_table(&self.sources(), Some(SPEC_REVISION))
    }
}
