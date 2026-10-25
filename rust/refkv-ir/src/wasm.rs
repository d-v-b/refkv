//! A C-ABI surface for wasm32 hosts (a browser, Node): each parser's
//! step/feed/finish over linear memory. The host reads the ranges each step asks
//! for, writes their bytes into buffers it allocates here, and feeds them.
//!
//! `vz_new(format, n, size)` makes a parser of the format named by the UTF-8 `format` (a name of
//! `Format::from_name`: the profile's in spec/zarrify/profiles/); `vz_step`, `vz_feed`
//! and `vz_finish` drive it. The `nd2_*` names are round 2's, kept.

use crate::nd2::{Nd2, Step};
use crate::run::AnyParser;
use std::cell::RefCell;

thread_local! {
    static OUT: RefCell<Vec<u8>> = const { RefCell::new(Vec::new()) };
}

fn set_out(b: Vec<u8>) -> usize {
    OUT.with(|o| *o.borrow_mut() = b);
    OUT.with(|o| o.borrow().len())
}

#[unsafe(no_mangle)]
pub extern "C" fn vz_alloc(n: usize) -> *mut u8 {
    let mut v = vec![0u8; n];
    let p = v.as_mut_ptr();
    std::mem::forget(v);
    p
}

/// # Safety
/// `p` and `n` must come from `vz_alloc(n)`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vz_free(p: *mut u8, n: usize) {
    drop(unsafe { Vec::from_raw_parts(p, n, n) });
}

/// The bytes of the last result (ranges as little-endian u64 pairs, a message, or the facts).
#[unsafe(no_mangle)]
pub extern "C" fn vz_out() -> *const u8 {
    OUT.with(|o| o.borrow().as_ptr())
}

/// The `refkv.fits_tiles` codec (spec/zarrify/fits.md §3.3
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/fits.md#33-tile-compressed-images>):
/// decodes the chunk at `chunk` (`n` bytes) with the configuration JSON at `config` (`nc`
/// bytes) into the pixels of the chunk shape at `shape` (`rank` little-endian u64), in
/// `vz_out`: the pixels' length, or minus a message's.
///
/// # Safety
/// The pointers come from `vz_alloc` with those lengths.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vz_fits_tiles_decode(chunk: *const u8, n: usize, config: *const u8, nc: usize, shape: *const u8, rank: usize) -> i64 {
    let chunk = unsafe { std::slice::from_raw_parts(chunk, n) };
    let config = unsafe { std::slice::from_raw_parts(config, nc) };
    let shape = unsafe { std::slice::from_raw_parts(shape, rank * 8) };
    let shape: Vec<u64> = shape.chunks(8).map(|b| u64::from_le_bytes(b.try_into().unwrap())).collect();
    let decoded = std::str::from_utf8(config)
        .map_err(|e| e.to_string())
        .and_then(|s| serde_json::from_str::<serde_json::Value>(s).map_err(|e| e.to_string()))
        .and_then(|v| crate::fits_tiles::Config::from_json(&v))
        .and_then(|cfg| crate::fits_tiles::decode(chunk, &cfg, &shape));
    match decoded {
        Ok(b) => set_out(b) as i64,
        Err(e) => -(set_out(e.into_bytes()) as i64),
    }
}

fn step_out(r: Result<Step, String>) -> i64 {
    match r {
        Ok(Step::Read(r)) => 1 + set_out(r.iter().flat_map(|(o, n)| [o.to_le_bytes(), n.to_le_bytes()]).flatten().collect()) as i64,
        Ok(Step::Done) => 0,
        Err(e) => -(set_out(e.into_bytes()) as i64),
    }
}

fn finish_out(r: Result<(crate::ir::Ir, serde_json::Value), String>) -> i64 {
    match r {
        Ok((ir, facts)) => {
            let out = serde_json::json!({
                "elements": ir.len(),
                "runs": ir.runs.len(),
                "check": crate::check::check(&ir).is_ok(),
                "digest": format!("{:016x}", ir.digest()),
                "facts": facts,
            });
            set_out(out.to_string().into_bytes()) as i64
        }
        Err(e) => -(set_out(e.into_bytes()) as i64),
    }
}

/// The format a file's first bytes give (`Format::sniff`; `head`/`n` from `vz_alloc`, consumed):
/// its name's length at `vz_out()`, or 0 when they are no format's.
///
/// # Safety
/// `head` and `n` must come from `vz_alloc(n)`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vz_sniff(head: *mut u8, n: usize) -> i64 {
    let head = unsafe { Vec::from_raw_parts(head, n, n) };
    match crate::run::Format::sniff(&head) {
        Some(f) => set_out(f.name().as_bytes().to_vec()) as i64,
        None => 0,
    }
}

/// A parser of the format named `format` (UTF-8 of `n` bytes, from `vz_alloc`, consumed) over
/// a source of `size` bytes, or null for a name that is no format's.
///
/// # Safety
/// `format` and `n` must come from `vz_alloc(n)`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vz_new(format: *mut u8, n: usize, size: u64) -> *mut AnyParser {
    let name = String::from_utf8(unsafe { Vec::from_raw_parts(format, n, n) }).unwrap_or_default();
    match Format::from_name(&name) {
        Some(f) => Box::into_raw(Box::new(AnyParser::new(f, size))),
        None => std::ptr::null_mut(),
    }
}

/// The next batch: 1 + its byte length at `vz_out()` (16 per range; a batch may
/// be empty), 0 when done, or minus the length of the rejection's message.
///
/// # Safety
/// `p` must come from `vz_new`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vz_step(p: *mut AnyParser) -> i64 {
    step_out(unsafe { &mut *p }.step())
}

/// # Safety
/// `p` from `vz_new`; `data` and `n` from `vz_alloc(n)` (consumed).
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vz_feed(p: *mut AnyParser, offset: u64, data: *mut u8, n: usize) {
    let v = unsafe { Vec::from_raw_parts(data, n, n) };
    unsafe { &mut *p }.feed(offset, v);
}

/// Finishes: JSON of the element and run counts, the checker's verdict, the IR's
/// digest and the facts at `vz_out()`, its length, or minus the length of a
/// rejection's message.
///
/// # Safety
/// `p` from `vz_new` (consumed).
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vz_finish(p: *mut AnyParser) -> i64 {
    finish_out(unsafe { Box::from_raw(p) }.finish())
}

#[unsafe(no_mangle)]
pub extern "C" fn nd2_new(size: u64) -> *mut Nd2 {
    Box::into_raw(Box::new(Nd2::new(size)))
}

/// # Safety
/// `p` must come from `nd2_new`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn nd2_step(p: *mut Nd2) -> i64 {
    step_out(unsafe { &mut *p }.step())
}

/// # Safety
/// `p` from `nd2_new`; `data` and `n` from `vz_alloc(n)` (consumed).
#[unsafe(no_mangle)]
pub unsafe extern "C" fn nd2_feed(p: *mut Nd2, offset: u64, data: *mut u8, n: usize) {
    unsafe { &mut *p }.feed(offset, unsafe { Vec::from_raw_parts(data, n, n) });
}

/// Finishes: the facts' JSON (with the element count) at `vz_out()`, its length,
/// or minus the length of a rejection's message.
///
/// # Safety
/// `p` from `nd2_new` (consumed).
#[unsafe(no_mangle)]
pub unsafe extern "C" fn nd2_finish(p: *mut Nd2) -> i64 {
    let p = unsafe { Box::from_raw(p) };
    match p.finish() {
        Ok((ir, mut facts)) => {
            facts["elements"] = serde_json::json!(ir.len());
            facts["runs"] = serde_json::json!(ir.runs.len());
            facts["check"] = serde_json::json!(crate::check::check(&ir).is_ok());
            set_out(facts.to_string().into_bytes()) as i64
        }
        Err(e) => -(set_out(e.into_bytes()) as i64),
    }
}

// ---- runs: a parser, the read planner, the projection and the mirror

use crate::run::{Format, Run};

fn err_out(e: String) -> i64 {
    -(set_out(e.into_bytes()) as i64)
}

/// A run of the format named `format` (UTF-8 of `nf` bytes, from `vz_alloc`, consumed: a name of
/// `Format::from_name`, such as `tiff`; a TIFF run becomes an NDPI one when its parser finds the
/// file is NDPI: the output's summary names the format) over a source of `size` bytes, remote or
/// not, read by the host at most `concurrency` requests at a time: the run (a pointer, positive),
/// or minus the length of a rejection's message at `vz_out()` for a name that is no format's.
///
/// # Safety
/// `format` and `nf` from `vz_alloc(nf)`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vz_run_new(format: *mut u8, nf: usize, size: u64, remote: u32, concurrency: u32) -> i64 {
    let name = String::from_utf8(unsafe { Vec::from_raw_parts(format, nf, nf) }).unwrap_or_default();
    match Format::from_name(&name) {
        Some(f) => Box::into_raw(Box::new(Run::new(f, size, remote != 0, concurrency as usize))) as usize as i64,
        None => err_out(format!("no format {name:?}")),
    }
}

/// Bytes the host holds already (from `offset`; `data`/`n` from `vz_alloc`, consumed).
///
/// # Safety
/// `p` from `vz_run_new`; `data`, `n` from `vz_alloc(n)`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vz_run_seed(p: *mut Run, offset: u64, data: *mut u8, n: usize) {
    let v = unsafe { Vec::from_raw_parts(data, n, n) };
    unsafe { &mut *p }.planner.seed(offset, v);
}

/// A request the host made itself (`n` bytes in `seconds`), for the cost model.
///
/// # Safety
/// `p` from `vz_run_new`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vz_run_observe(p: *mut Run, n: u64, seconds: f64) {
    unsafe { &mut *p }.planner.observe(n, seconds);
}

/// The planner's settings: the cost of a byte fetched (seconds; negative: the
/// default) and the size at or below which a remote source is read whole.
///
/// # Safety
/// `p` from `vz_run_new`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vz_run_settings(p: *mut Run, byte_cost: f64, whole_below: u64) {
    let r = unsafe { &mut *p };
    if byte_cost >= 0.0 {
        r.planner.byte_cost = byte_cost;
    }
    r.planner.whole_below = whole_below;
}

/// Whether the server answers multi-range requests: −1 unknown (probe), 0 no, 1 yes.
///
/// # Safety
/// `p` from `vz_run_new`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vz_run_multirange(p: *mut Run, v: i32) {
    unsafe { &mut *p }.planner.multirange = match v {
        0 => Some(false),
        1 => Some(true),
        _ => None,
    };
}

/// The requests to make now: 1 + the byte length at `vz_out()` of u32 count, then per
/// request a u32 span count and its spans as u64 (start, end) pairs (one span: a
/// plain range request; several: a multi-range one); 0 when the run is done; or
/// minus the length of a rejection's message.
///
/// # Safety
/// `p` from `vz_run_new`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vz_run_poll(p: *mut Run) -> i64 {
    match unsafe { &mut *p }.poll() {
        Ok(None) => 0,
        Ok(Some(reqs)) => {
            let mut b = Vec::new();
            b.extend_from_slice(&(reqs.len() as u32).to_le_bytes());
            for q in reqs {
                b.extend_from_slice(&(q.spans.len() as u32).to_le_bytes());
                for (a, e) in q.spans {
                    b.extend_from_slice(&a.to_le_bytes());
                    b.extend_from_slice(&e.to_le_bytes());
                }
            }
            1 + set_out(b) as i64
        }
        Err(e) => err_out(e),
    }
}

/// Request `k` of the last poll completed in `seconds`: with `ok`, `data`/`n` (from
/// `vz_alloc`, consumed) are its spans' bytes one after another; without, a
/// multi-range request the server did not answer with its ranges (`data` null).
/// 0, or minus the length of a rejection's message.
///
/// # Safety
/// `p` from `vz_run_new`; `data`, `n` from `vz_alloc(n)` or null and 0.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vz_run_complete(p: *mut Run, k: u32, ok: u32, data: *mut u8, n: usize, seconds: f64) -> i64 {
    let r = unsafe { &mut *p };
    let parts = if ok != 0 {
        let v = if data.is_null() { Vec::new() } else { unsafe { Vec::from_raw_parts(data, n, n) } };
        let reqs = r.planner.round_spans(k as usize);
        let mut parts = Vec::with_capacity(reqs.len());
        let mut at = 0usize;
        for (a, e) in reqs {
            let len = (e - a) as usize;
            if at + len > v.len() {
                return err_out("a response of the wrong length".into());
            }
            parts.push(v[at..at + len].to_vec());
            at += len;
        }
        if at != v.len() {
            return err_out("a response of the wrong length".into());
        }
        Some(parts)
    } else {
        None
    };
    match r.complete(k as usize, parts, seconds) {
        Ok(()) => 0,
        Err(e) => err_out(e),
    }
}

/// The run's planner counts and model (JSON at `vz_out()`): its length.
///
/// # Safety
/// `p` from `vz_run_new`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vz_run_stats(p: *mut Run) -> i64 {
    set_out(unsafe { &*p }.planner.stats_json().to_string().into_bytes()) as i64
}

/// Finishes the run (consumed), projects it and, with `flags` bit 0, mirrors it, for a host
/// that records the ranges' checksums with bit 1 (`Run::into_output`): the output
/// (`Out::encode`) at `vz_out()`, its length, or minus the length of a rejection's message.
/// `url` (UTF-8, from `vz_alloc`, consumed) is the source's.
///
/// # Safety
/// `p` from `vz_run_new`; `url`, `n` from `vz_alloc(n)`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vz_run_output(p: *mut Run, url: *mut u8, n: usize, flags: u32) -> i64 {
    let r = *unsafe { Box::from_raw(p) };
    let url = String::from_utf8(unsafe { Vec::from_raw_parts(url, n, n) }).unwrap_or_default();
    match r.into_output(&url, flags & 1 != 0, flags & 2 != 0) {
        Ok(o) => set_out(o.encode()) as i64,
        Err(e) => err_out(e),
    }
}

/// Drops a run without finishing it.
///
/// # Safety
/// `p` from `vz_run_new`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vz_run_free(p: *mut Run) {
    drop(unsafe { Box::from_raw(p) });
}

// ---- store runs (src/store.rs; conventions §8.9
// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#89-stores>)

use crate::store::{StoreFormat, StoreRun};

/// A store run of the store format named `format` (UTF-8, e.g. `zarr2`) over the
/// listing `listing` (the JSON `[[key, size], ...]`; both from `vz_alloc`, consumed), the
/// host reading at most `concurrency` objects at a time: the run (a pointer, positive), or
/// minus the length of a rejection's message at `vz_out()`.
///
/// # Safety
/// `format`/`nf` and `listing`/`nl` from `vz_alloc`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vz_store_new(format: *mut u8, nf: usize, listing: *mut u8, nl: usize, concurrency: u32) -> i64 {
    let f = String::from_utf8(unsafe { Vec::from_raw_parts(format, nf, nf) }).unwrap_or_default();
    let l = unsafe { Vec::from_raw_parts(listing, nl, nl) };
    let made = (|| -> Result<StoreRun, String> {
        let f = StoreFormat::from_name(&f).ok_or(format!("no store format {f:?}"))?;
        let keys: Vec<(String, u64)> = serde_json::from_slice(&l).map_err(|e| format!("the listing: {e}"))?;
        StoreRun::new(f, keys, concurrency as usize)
    })();
    match made {
        Ok(r) => Box::into_raw(Box::new(r)) as usize as i64,
        Err(e) => err_out(e),
    }
}

/// The object reads to make now: 1 + the byte length at `vz_out()` of a u32 count, then per
/// request the u32 index of its object in the listing, a u32 span count and the spans as
/// u64 (start, end) pairs (each a range request; a whole object is one span); 0 when the
/// run is done; or minus the length of a rejection's message.
///
/// # Safety
/// `p` from `vz_store_new`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vz_store_poll(p: *mut StoreRun) -> i64 {
    match unsafe { &mut *p }.poll() {
        Ok(None) => 0,
        Ok(Some(reqs)) => {
            let mut b = Vec::new();
            b.extend_from_slice(&(reqs.len() as u32).to_le_bytes());
            for q in reqs {
                b.extend_from_slice(&(q.object as u32).to_le_bytes());
                b.extend_from_slice(&(q.spans.len() as u32).to_le_bytes());
                for (a, e) in q.spans {
                    b.extend_from_slice(&a.to_le_bytes());
                    b.extend_from_slice(&e.to_le_bytes());
                }
            }
            1 + set_out(b) as i64
        }
        Err(e) => err_out(e),
    }
}

/// Request `k` of the last poll completed: `data`/`n` (from `vz_alloc`, consumed) are its
/// spans' bytes one after another. 0, or minus the length of a rejection's message.
///
/// # Safety
/// `p` from `vz_store_new`; `data`, `n` from `vz_alloc(n)` (or null and 0).
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vz_store_complete(p: *mut StoreRun, k: u32, data: *mut u8, n: usize) -> i64 {
    let r = unsafe { &mut *p };
    let v = if data.is_null() { Vec::new() } else { unsafe { Vec::from_raw_parts(data, n, n) } };
    let lens: Vec<u64> = r.round_spans(k as usize).iter().map(|s| s.1 - s.0).collect();
    let mut parts = Vec::with_capacity(lens.len());
    let mut at = 0usize;
    for len in lens {
        let len = len as usize;
        if at + len > v.len() {
            return err_out("a response of the wrong length".into());
        }
        parts.push(v[at..at + len].to_vec());
        at += len;
    }
    if at != v.len() {
        return err_out("a response of the wrong length".into());
    }
    match r.complete(k as usize, parts) {
        Ok(()) => 0,
        Err(e) => err_out(e),
    }
}

/// Finishes the store run (consumed) and, with `mirror`, mirrors it: the output
/// (`Out::encode`, its references to objects of part kind 3) at `vz_out()`, its length, or
/// minus the length of a rejection's message. `url` (UTF-8, from `vz_alloc`, consumed) is
/// the store's.
///
/// # Safety
/// `p` from `vz_store_new`; `url`, `n` from `vz_alloc(n)`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vz_store_output(p: *mut StoreRun, url: *mut u8, n: usize, mirror: u32) -> i64 {
    let r = *unsafe { Box::from_raw(p) };
    let url = String::from_utf8(unsafe { Vec::from_raw_parts(url, n, n) }).unwrap_or_default();
    match r.into_output(&url, mirror != 0) {
        Ok(o) => set_out(o.encode()) as i64,
        Err(e) => err_out(e),
    }
}

/// The keys the listing ignores, before the first poll (`StoreRun::ignore`): `json` (from
/// `vz_alloc`, consumed) is `[[key, ...], [folder, ...]]`, the relative keys the store reader
/// ignored and recorded, which the projection records, and the empty `d/` keys, which a SAFE
/// directory's parser reads as directories. 0, or minus the length of a rejection's message.
///
/// # Safety
/// `p` from `vz_store_new`; `json`, `n` from `vz_alloc(n)`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vz_store_ignore(p: *mut StoreRun, json: *mut u8, n: usize) -> i64 {
    let r = unsafe { &mut *p };
    let b = unsafe { Vec::from_raw_parts(json, n, n) };
    let got = serde_json::from_slice::<(Vec<String>, Vec<String>)>(&b).map_err(|e| format!("the ignored keys: {e}"))
        .and_then(|(keys, folders)| r.ignore(keys, folders));
    match got {
        Ok(()) => 0,
        Err(e) => err_out(e),
    }
}

/// The keys of a pending batch of probes (`StoreRun::probes`; `vz_store_poll` gave a count of
/// 0): a JSON array of keys at `vz_out()`, its length. The host answers each, in order, with
/// `vz_store_found`, then polls again.
///
/// # Safety
/// `p` from `vz_store_new`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vz_store_probes(p: *mut StoreRun) -> i64 {
    let keys: Vec<String> = unsafe { &*p }.probes().to_vec();
    set_out(serde_json::to_string(&keys).unwrap_or_default().into_bytes()) as i64
}

/// The answer to the next probe of the batch: the key's size, or −1 when it does not exist. 0,
/// or minus the length of a rejection's message.
///
/// # Safety
/// `p` from `vz_store_new`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vz_store_found(p: *mut StoreRun, size: i64) -> i64 {
    match unsafe { &mut *p }.found(if size < 0 { None } else { Some(size as u64) }) {
        Ok(()) => 0,
        Err(e) => err_out(e),
    }
}

/// The profile the run's store is decided to be so far (`StoreRun::profile`): its UTF-8 at
/// `vz_out()`, the length returned.
///
/// # Safety
/// `p` from `vz_store_new`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vz_store_profile(p: *mut StoreRun) -> i64 {
    set_out(unsafe { &*p }.profile().as_bytes().to_vec()) as i64
}

/// Drops a store run without finishing it.
///
/// # Safety
/// `p` from `vz_store_new`.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vz_store_free(p: *mut StoreRun) {
    drop(unsafe { Box::from_raw(p) });
}

// ---- test hooks (feature `test-hooks`, built by `wasm/build.sh test` into
// target/web-test; never in the shipped module)

/// The mirror of a crafted IR, for the browser's tests of the mirror's rejections:
/// `json` (from `vz_alloc`, consumed) is `{"size", "kind", "parent", "name", "nidx",
/// "start", "len", "runs": [[root, count, stride]]}` (a parent of -1: none; a name
/// index of -1: none), `profile` (UTF-8 of `np` bytes, from `vz_alloc`, consumed) the name of
/// a format of `Format::from_name`. The output's length, or minus the rejection's.
///
/// # Safety
/// `json`/`n` and `profile`/`np` from `vz_alloc`.
#[cfg(feature = "test-hooks")]
#[unsafe(no_mangle)]
pub unsafe extern "C" fn vz_test_mirror(json: *mut u8, n: usize, profile: *mut u8, np: usize) -> i64 {
    let b = unsafe { Vec::from_raw_parts(json, n, n) };
    let profile = String::from_utf8(unsafe { Vec::from_raw_parts(profile, np, np) }).unwrap_or_default();
    let built = (|| -> Result<crate::out::Out, String> {
        let v: serde_json::Value = serde_json::from_slice(&b).map_err(|e| e.to_string())?;
        let nums = |k: &str| -> Vec<i64> { v[k].as_array().map(|a| a.iter().filter_map(|x| x.as_i64()).collect()).unwrap_or_default() };
        let mut ir = crate::ir::Ir { size: v["size"].as_u64().unwrap_or(0), budget: None, ..Default::default() };
        ir.types.id("");
        let names = v["name"].as_array().cloned().unwrap_or_default();
        let (kind, parent, nidx, start, len) = (nums("kind"), nums("parent"), nums("nidx"), nums("start"), nums("len"));
        for k in 0..kind.len() {
            ir.kind.push(kind[k] as u8);
            ir.parent.push(if parent[k] < 0 { crate::ir::NONE } else { parent[k] as u32 });
            let name = names.get(k).and_then(|x| x.as_str()).unwrap_or("");
            let id = ir.names.id(name);
            ir.name.push(id);
            ir.nidx.push(nidx[k] as u64);
            ir.ty.push(0);
            ir.space.push(0);
            ir.start.push(start[k] as u64);
            ir.len.push(len[k] as u64);
        }
        for r in v["runs"].as_array().cloned().unwrap_or_default() {
            let r: Vec<u64> = r.as_array().map(|a| a.iter().filter_map(|x| x.as_u64()).collect()).unwrap_or_default();
            ir.runs.push((r[0] as u32, r[1], r[2]));
        }
        ir.finished = true;
        ir.build_children();
        let format = Format::from_name(&profile).ok_or("no such profile")?;
        let mut out = crate::out::Out::new("");
        crate::mirror::mirror(&ir, &mut out, format.name())?;
        Ok(out)
    })();
    match built {
        Ok(o) => set_out(o.encode()) as i64,
        Err(e) => err_out(e),
    }
}
