//! The reader without I/O of its own, for hosts that cannot block on a read (a service worker,
//! whose reads are `fetch`es), as `refkv-ir`'s parsers are: **sans-IO by replay**.
//!
//! A [`Fed`] is a [`ReadAt`] over the byte ranges the host has fed it. A read of a range that is
//! not there records the range as *wanted* and fails; the operation that made it fails too, or
//! goes on to fail some other way. The host then reads every wanted range, feeds them, and runs
//! the operation again from the start (a *round*). A run that recorded no wanted range is the
//! outcome a blocking reader would have given; a run that did is thrown away, whatever it
//! returned, because it may have returned an error, or an answer, that came from a missing read.
//! The reader is the one of [`Store`], unchanged, so its answers are its answers.
//!
//! The rounds of an operation are the depth of its dependent reads: opening a store reads its
//! tail, then whatever sections lie before it; a `get` reads a block of slots, then a record,
//! then the bytes of the sources it names. The ranges a host feeds stay in memory while the
//! [`Fed`] lives.

use crate::reader::{Limits, ReadAt, ReadError, Request, Resolver, Store};
use refkv_vzip::pb::Source;
use std::cell::RefCell;
use std::collections::{BTreeMap, HashMap};
use std::io;
use std::rc::Rc;

#[derive(Default)]
struct State {
    /// the ranges fed: start -> bytes. They do not overlap or touch.
    fed: BTreeMap<u64, Vec<u8>>,
    /// the ranges of the store's file that this run read and did not find
    wanted: Vec<(u64, u64)>,
    /// the bytes of source ranges fed: (source, offset, length) -> bytes
    sources: HashMap<(Source, u64, u64), Vec<u8>>,
    /// the source ranges this run read and did not find
    wanted_sources: Vec<(Source, u64, u64)>,
}

/// The bytes of a store's file, as fed. Cloning gives another handle on the same bytes.
#[derive(Clone)]
pub struct Fed {
    size: u64,
    st: Rc<RefCell<State>>,
}

impl Fed {
    /// A store's file of `size` bytes, none of them fed yet.
    pub fn new(size: u64) -> Fed {
        Fed { size, st: Rc::new(RefCell::new(State::default())) }
    }

    /// Feeds bytes `[offset, offset + bytes.len())` of the file. Ranges that overlap or touch are
    /// merged, the new bytes replacing the old where they overlap.
    pub fn feed(&self, offset: u64, bytes: &[u8]) {
        if bytes.is_empty() {
            return;
        }
        let mut st = self.st.borrow_mut();
        let end = offset + bytes.len() as u64;
        let touching: Vec<u64> = st.fed.range(..=end).filter(|(s, v)| **s + v.len() as u64 >= offset).map(|(s, _)| *s).collect();
        let start = touching.first().map_or(offset, |s| (*s).min(offset));
        let stop = touching.iter().map(|s| *s + st.fed[s].len() as u64).fold(end, u64::max);
        let mut merged = vec![0u8; (stop - start) as usize];
        for s in touching {
            let old = st.fed.remove(&s).unwrap();
            merged[(s - start) as usize..][..old.len()].copy_from_slice(&old);
        }
        merged[(offset - start) as usize..][..bytes.len()].copy_from_slice(bytes);
        st.fed.insert(start, merged);
    }

    /// Feeds the bytes of `source` at `[offset, offset + bytes.len())`, which a `get` of a
    /// reference asked for.
    pub fn feed_source(&self, source: Source, offset: u64, bytes: Vec<u8>) {
        let length = bytes.len() as u64;
        self.st.borrow_mut().sources.insert((source, offset, length), bytes);
    }

    fn clear_wanted(&self) {
        let mut st = self.st.borrow_mut();
        st.wanted.clear();
        st.wanted_sources.clear();
    }

    fn missed(&self) -> bool {
        let st = self.st.borrow();
        !st.wanted.is_empty() || !st.wanted_sources.is_empty()
    }

    /// What the last run read and did not find, each once, in order of offset.
    fn wanted(&self) -> (Vec<(u64, u64)>, Vec<(Source, u64, u64)>) {
        let st = self.st.borrow();
        let mut ranges = st.wanted.clone();
        ranges.sort_unstable();
        ranges.dedup();
        let mut sources: Vec<(Source, u64, u64)> = Vec::new();
        for w in &st.wanted_sources {
            if !sources.contains(w) {
                sources.push(w.clone());
            }
        }
        (ranges, sources)
    }
}

impl ReadAt for Fed {
    fn size(&self) -> io::Result<u64> {
        Ok(self.size)
    }

    fn read_at(&self, offset: u64, buf: &mut [u8]) -> io::Result<()> {
        if buf.is_empty() {
            return Ok(());
        }
        let mut st = self.st.borrow_mut();
        let end = offset.checked_add(buf.len() as u64).ok_or(io::ErrorKind::UnexpectedEof)?;
        if let Some((s, v)) = st.fed.range(..=offset).next_back()
            && s + v.len() as u64 >= end
        {
            buf.copy_from_slice(&v[(offset - s) as usize..(end - s) as usize]);
            return Ok(());
        }
        st.wanted.push((offset, buf.len() as u64));
        Err(io::Error::new(io::ErrorKind::WouldBlock, "range not fed"))
    }
}

/// The [`Resolver`] of a [`Fed`]: a `url` source's bytes are the ones fed for exactly that
/// range, else the range is wanted.
struct FedResolver(Fed);

impl Resolver for FedResolver {
    fn read(&self, source: &Source, offset: u64, length: u64) -> Result<Vec<u8>, String> {
        let mut st = self.0.st.borrow_mut();
        let key = (source.clone(), offset, length);
        if let Some(b) = st.sources.get(&key) {
            return Ok(b.clone());
        }
        st.wanted_sources.push(key);
        Err("source range not fed".into())
    }
}

/// What a run needs before it can be run again.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Needs {
    /// ranges `(offset, length)` of the store's file
    pub ranges: Vec<(u64, u64)>,
    /// ranges `(index in the source table, offset, length)` of sources
    pub sources: Vec<(usize, u64, u64)>,
}

/// The outcome of one run of an operation.
#[derive(Debug)]
pub enum Step<T> {
    Done(T),
    /// the run read what was not fed: feed it, and run the operation again
    Need(Needs),
    Failed(ReadError),
}

/// A store being read from fed bytes: open it, then `get` and `list`, each by rounds.
pub struct Session {
    fed: Fed,
    limits: Limits,
    store: Option<Store<Fed>>,
}

impl Session {
    pub fn new(file_size: u64) -> Session {
        Session::with_limits(file_size, Limits::default())
    }

    pub fn with_limits(file_size: u64, limits: Limits) -> Session {
        Session { fed: Fed::new(file_size), limits, store: None }
    }

    /// The bytes fed to this session.
    pub fn fed(&self) -> &Fed {
        &self.fed
    }

    /// The opened store, once `open` is `Done`.
    pub fn store(&self) -> Option<&Store<Fed>> {
        self.store.as_ref()
    }

    /// Runs `open` again; `Done` once the store is open (and from then on).
    pub fn open(&mut self) -> Step<()> {
        if self.store.is_some() {
            return Step::Done(());
        }
        self.fed.clear_wanted();
        let r = Store::open_with(self.fed.clone(), self.limits);
        if self.fed.missed() {
            let (ranges, _) = self.fed.wanted();
            return Step::Need(Needs { ranges, sources: Vec::new() });
        }
        match r {
            Ok(s) => {
                self.store = Some(s);
                Step::Done(())
            }
            Err(e) => Step::Failed(e),
        }
    }

    /// `get`: the value of `key` (or a window of it), or `None` if the store has no such key.
    pub fn get(&mut self, key: &str, request: Request) -> Step<Option<Vec<u8>>> {
        self.run(|s, r| s.get(key, request, r))
    }

    /// `list`: the keys that start with `prefix`.
    pub fn list(&mut self, prefix: &str) -> Step<Vec<String>> {
        self.run(|s, _| s.list(prefix))
    }

    fn run<T>(&mut self, f: impl FnOnce(&Store<Fed>, &dyn Resolver) -> Result<T, ReadError>) -> Step<T> {
        let Some(store) = &self.store else {
            return Step::Failed(ReadError { class: crate::reader::Class::Request, message: "the store is not open".into() });
        };
        self.fed.clear_wanted();
        let r = f(store, &FedResolver(self.fed.clone()));
        if self.fed.missed() {
            let (ranges, wanted) = self.fed.wanted();
            let sources = wanted.into_iter().filter_map(|(s, o, n)| store.sources().iter().position(|t| *t == s).map(|i| (i, o, n))).collect();
            return Step::Need(Needs { ranges, sources });
        }
        match r {
            Ok(v) => Step::Done(v),
            Err(e) => Step::Failed(e),
        }
    }
}
