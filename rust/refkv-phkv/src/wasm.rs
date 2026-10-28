//! A C-ABI surface for wasm32 hosts (a browser, a service worker, Node): the sans-IO reader of
//! [`crate::sansio`] over linear memory.
//!
//! A host makes a session for a store's file of a known size (`phkv_new`), then drives each
//! operation by rounds: it calls `phkv_open`, `phkv_get` or `phkv_list`; a result of
//! [`NEED`] means the operation read what was not fed, and `phkv_out` holds what it wants. The
//! host reads those ranges (a service worker `fetch`es them, in parallel), feeds them
//! (`phkv_feed` for the store's file, `phkv_feed_source` for a source), and calls the operation
//! again. The reader does no I/O: it keeps what is fed, so a key that was read is read again at
//! no cost.
//!
//! Results of operations are [`DONE`] (the payload is in `phkv_out`), [`ABSENT`] (a `get` of a key
//! the store does not have), [`NEED`], or [`FAILED`] (`phkv_out` is a class byte, then a UTF-8
//! message). Integers in payloads are little-endian.

use crate::reader::{Class, ReadError, Request};
use crate::sansio::{Needs, Session, Step};
use refkv_vzip::pb::SourceKind;
use std::cell::RefCell;

pub const DONE: i32 = 0;
pub const NEED: i32 = 1;
pub const ABSENT: i32 = 2;
pub const FAILED: i32 = -1;

thread_local! {
    static OUT: RefCell<Vec<u8>> = const { RefCell::new(Vec::new()) };
    static SESSIONS: RefCell<Vec<Option<Session>>> = const { RefCell::new(Vec::new()) };
}

fn set_out(b: Vec<u8>) {
    OUT.with(|o| *o.borrow_mut() = b);
}

/// A slice of linear memory the host wrote.
///
/// # Safety
/// `p` and `n` must come from `phkv_alloc(n)` (or be an empty slice).
unsafe fn host_bytes<'a>(p: *const u8, n: usize) -> &'a [u8] {
    if n == 0 { &[] } else { unsafe { std::slice::from_raw_parts(p, n) } }
}

fn class_code(c: Class) -> u8 {
    match c {
        Class::Archive => 0,
        Class::Entry => 1,
        Class::Body => 2,
        Class::Payload => 3,
        Class::Resolution => 4,
        Class::Request => 5,
        Class::Property => 6,
        Class::Io => 7,
    }
}

fn failed(e: ReadError) -> i32 {
    let mut b = vec![class_code(e.class)];
    b.extend_from_slice(e.message.as_bytes());
    set_out(b);
    FAILED
}

fn failed_message(class: Class, m: &str) -> i32 {
    failed(ReadError { class, message: m.into() })
}

/// What an operation needs, as `(source + 1 or 0 for the store's file, offset, length)` triples
/// of u64.
fn need(n: Needs) -> i32 {
    let mut b = Vec::with_capacity(24 * (n.ranges.len() + n.sources.len()));
    for (offset, length) in n.ranges {
        for v in [0u64, offset, length] {
            b.extend_from_slice(&v.to_le_bytes());
        }
    }
    for (i, offset, length) in n.sources {
        for v in [i as u64 + 1, offset, length] {
            b.extend_from_slice(&v.to_le_bytes());
        }
    }
    set_out(b);
    NEED
}

fn finish<T>(step: Step<T>, done: impl FnOnce(T) -> i32) -> i32 {
    match step {
        Step::Done(v) => done(v),
        Step::Need(n) => need(n),
        Step::Failed(e) => failed(e),
    }
}

/// Runs `f` on session `h`.
fn with_session(h: u32, f: impl FnOnce(&mut Session) -> i32) -> i32 {
    SESSIONS.with(|s| match s.borrow_mut().get_mut((h as usize).wrapping_sub(1)) {
        Some(Some(session)) => f(session),
        _ => failed_message(Class::Request, "no such session"),
    })
}

#[unsafe(no_mangle)]
pub extern "C" fn phkv_alloc(n: usize) -> *mut u8 {
    let mut v = vec![0u8; n];
    let p = v.as_mut_ptr();
    std::mem::forget(v);
    p
}

/// # Safety
/// `p` and `n` must come from `phkv_alloc(n)`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn phkv_free(p: *mut u8, n: usize) {
    drop(unsafe { Vec::from_raw_parts(p, n, n) });
}

/// The bytes of the last result.
#[unsafe(no_mangle)]
pub extern "C" fn phkv_out() -> *const u8 {
    OUT.with(|o| o.borrow().as_ptr())
}

#[unsafe(no_mangle)]
pub extern "C" fn phkv_out_len() -> usize {
    OUT.with(|o| o.borrow().len())
}

/// A session over a store's file of `file_size` bytes, none of them fed. Its handle is never 0.
#[unsafe(no_mangle)]
pub extern "C" fn phkv_new(file_size: u64) -> u32 {
    SESSIONS.with(|s| {
        let mut s = s.borrow_mut();
        let session = Some(Session::new(file_size));
        match s.iter().position(Option::is_none) {
            Some(i) => {
                s[i] = session;
                i as u32 + 1
            }
            None => {
                s.push(session);
                s.len() as u32
            }
        }
    })
}

#[unsafe(no_mangle)]
pub extern "C" fn phkv_close(h: u32) {
    SESSIONS.with(|s| {
        if let Some(slot) = s.borrow_mut().get_mut((h as usize).wrapping_sub(1)) {
            *slot = None;
        }
    });
}

/// Feeds bytes `[offset, offset + len)` of the store's file, which the host wrote at `p`.
///
/// # Safety
/// `p` and `len` must come from `phkv_alloc(len)`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn phkv_feed(h: u32, offset: u64, p: *const u8, len: usize) -> i32 {
    let bytes = unsafe { host_bytes(p, len) };
    with_session(h, |s| {
        s.fed().feed(offset, bytes);
        DONE
    })
}

/// Feeds bytes `[offset, offset + len)` of source `index` of the source table, which the host
/// wrote at `p`: the bytes a `NEED` asked for.
///
/// # Safety
/// `p` and `len` must come from `phkv_alloc(len)`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn phkv_feed_source(h: u32, index: u32, offset: u64, p: *const u8, len: usize) -> i32 {
    let bytes = unsafe { host_bytes(p, len) }.to_vec();
    with_session(h, |s| {
        let Some(source) = s.store().and_then(|st| st.sources().get(index as usize)).cloned() else {
            return failed_message(Class::Request, "no such source");
        };
        s.fed().feed_source(source, offset, bytes);
        DONE
    })
}

/// Opens the store: `DONE`, or `NEED` more of the file, or `FAILED` (an archive error).
#[unsafe(no_mangle)]
pub extern "C" fn phkv_open(h: u32) -> i32 {
    with_session(h, |s| finish(s.open(), |()| DONE))
}

/// The number of keys of an open store, or `-1`.
#[unsafe(no_mangle)]
pub extern "C" fn phkv_key_count(h: u32) -> i64 {
    SESSIONS.with(|s| match s.borrow().get((h as usize).wrapping_sub(1)) {
        Some(Some(session)) => session.store().map_or(-1, |st| st.key_count() as i64),
        _ => -1,
    })
}

/// The number of sources of an open store, or `-1`.
#[unsafe(no_mangle)]
pub extern "C" fn phkv_source_count(h: u32) -> i32 {
    SESSIONS.with(|s| match s.borrow().get((h as usize).wrapping_sub(1)) {
        Some(Some(session)) => session.store().map_or(-1, |st| st.sources().len() as i32),
        _ => -1,
    })
}

fn json_string(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
}

/// Source `index` as JSON in `phkv_out`: `{"kind":"url","url":...}` or `{"kind":"key","key":...}`
/// or `{"kind":"data"}`, with the pins of a url source (`size`, `etag`, `modified_not_after`) when
/// it has them, which the host checks as it reads (spec/refkv.md §5.1).
#[unsafe(no_mangle)]
pub extern "C" fn phkv_source(h: u32, index: u32) -> i32 {
    with_session(h, |s| {
        let Some(src) = s.store().and_then(|st| st.sources().get(index as usize)) else {
            return failed_message(Class::Request, "no such source");
        };
        let mut j = String::from("{");
        match &src.kind {
            SourceKind::Url(u) => {
                j.push_str("\"kind\":\"url\",\"url\":");
                json_string(u, &mut j);
            }
            SourceKind::Key(k) => {
                j.push_str("\"kind\":\"key\",\"key\":");
                json_string(k, &mut j);
            }
            SourceKind::Data(_) => j.push_str("\"kind\":\"data\""),
        }
        if let Some(n) = src.size {
            j.push_str(&format!(",\"size\":{n}"));
        }
        if let Some(e) = &src.etag {
            j.push_str(",\"etag\":");
            json_string(e, &mut j);
        }
        if let Some(t) = src.modified_not_after {
            j.push_str(&format!(",\"modified_not_after\":{t}"));
        }
        j.push('}');
        set_out(j.into_bytes());
        DONE
    })
}

/// `get` of the key at `key`/`key_len` (UTF-8): `mode` 0 is the whole value, 1 the window
/// `[a, b)`, 2 from `a` on, 3 the last `a` bytes. `DONE` with the bytes in `phkv_out`, or `ABSENT`.
///
/// # Safety
/// `key` and `key_len` must come from `phkv_alloc(key_len)`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn phkv_get(h: u32, key: *const u8, key_len: usize, mode: u32, a: u64, b: u64) -> i32 {
    let key = unsafe { host_bytes(key, key_len) };
    let Ok(key) = std::str::from_utf8(key) else {
        return failed_message(Class::Request, "a key that is not UTF-8");
    };
    let request = match mode {
        0 => Request::Whole,
        1 => Request::Range(a, b),
        2 => Request::Offset(a),
        3 => Request::Suffix(a),
        _ => return failed_message(Class::Request, "no such request mode"),
    };
    with_session(h, |s| {
        finish(s.get(key, request), |v| match v {
            Some(bytes) => {
                set_out(bytes);
                DONE
            }
            None => ABSENT,
        })
    })
}

/// `list` of the keys that start with the UTF-8 prefix: `DONE` with each key as a u32 length and
/// its bytes, in order, in `phkv_out`.
///
/// # Safety
/// `prefix` and `len` must come from `phkv_alloc(len)`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn phkv_list(h: u32, prefix: *const u8, len: usize) -> i32 {
    let prefix = unsafe { host_bytes(prefix, len) };
    let Ok(prefix) = std::str::from_utf8(prefix) else {
        return failed_message(Class::Request, "a prefix that is not UTF-8");
    };
    with_session(h, |s| {
        finish(s.list(prefix), |keys| {
            let mut b = Vec::new();
            for k in keys {
                b.extend_from_slice(&(k.len() as u32).to_le_bytes());
                b.extend_from_slice(k.as_bytes());
            }
            set_out(b);
            DONE
        })
    })
}
