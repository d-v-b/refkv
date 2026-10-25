//! A file input through the Rust core (python/src/refkv/ir/__init__.py and planner.py): the format
//! its first bytes give, a run of the format's parser driven by the read planner, its requests
//! performed by the transport, and the output the core projects and mirrors.

use crate::transport::Transport;
use crate::Fail;
use refkv_ir::run::{Format, Run};
use std::sync::atomic::{AtomicUsize, Ordering};

/// The rejection of a file no format's signature matches.
pub const NOT_SUPPORTED: &str = "not a file of any supported format";

/// Requests at a time to a remote host, by default (a browser has 6).
const CONNECTIONS: usize = 8;

/// `f` of each item, in the items' order, with at most `workers` threads at a time.
pub fn map<T: Sync, R: Send>(items: &[T], workers: usize, f: impl Fn(&T) -> R + Sync) -> Vec<R> {
    let n = workers.min(items.len());
    if n <= 1 {
        return items.iter().map(f).collect();
    }
    let next = AtomicUsize::new(0);
    let mut got: Vec<(usize, R)> = std::thread::scope(|s| {
        let threads: Vec<_> = (0..n)
            .map(|_| {
                s.spawn(|| {
                    let mut mine = Vec::new();
                    loop {
                        let i = next.fetch_add(1, Ordering::Relaxed);
                        if i >= items.len() {
                            return mine;
                        }
                        mine.push((i, f(&items[i])));
                    }
                })
            })
            .collect();
        threads.into_iter().flat_map(|t| t.join().expect("a reading thread panicked")).collect()
    });
    got.sort_unstable_by_key(|x| x.0);
    got.into_iter().map(|x| x.1).collect()
}

/// Requests at a time: `connections` (default 8, or REFKV_CONCURRENCY's) for a remote source, 4
/// for a local file.
pub fn concurrency(t: &Transport, connections: Option<usize>) -> Result<usize, Fail> {
    if !t.remote() {
        return Ok(4);
    }
    if let Some(c) = connections.filter(|&c| c > 0) {
        return Ok(c);
    }
    match std::env::var("REFKV_CONCURRENCY") {
        Ok(v) if !v.is_empty() => v.trim().parse().map_err(|_| Fail::Other(format!("REFKV_CONCURRENCY={v:?} is not a number"))),
        _ => Ok(CONNECTIONS),
    }
}

fn env_number<T: std::str::FromStr>(name: &str) -> Result<Option<T>, Fail> {
    match std::env::var(name) {
        Ok(v) if !v.is_empty() => v.trim().parse().map(Some).map_err(|_| Fail::Other(format!("{name}={v:?} is not a number"))),
        _ => Ok(None),
    }
}

/// A run of `fmt` over the transport, seeded with the bytes that opened it. `byte_cost`: seconds of
/// wall time a byte fetched costs the planner (default: the core's, 0.1 s per MB). For
/// experiments, REFKV_AMPLIFICATION sets the amplification cap, REFKV_BYTE_COST the byte cost (in
/// seconds per MB), REFKV_WHOLE_BELOW the size below which a file is read whole, and
/// REFKV_MULTIRANGE=0 turns multi-range requests off.
pub fn new_run(fmt: Format, t: &Transport, connections: Option<usize>, byte_cost: Option<f64>) -> Result<Run, Fail> {
    let mut run = Run::new(fmt, t.size(), t.remote(), concurrency(t, connections)?);
    run.planner.cap_override = env_number::<f64>("REFKV_AMPLIFICATION")?;
    if let Some(c) = byte_cost.or(env_number::<f64>("REFKV_BYTE_COST")?.map(|c| c * 1e-6)) {
        run.planner.byte_cost = c;
    }
    if let Some(w) = env_number::<u64>("REFKV_WHOLE_BELOW")? {
        run.planner.whole_below = w;
    }
    if std::env::var("REFKV_MULTIRANGE").as_deref() == Ok("0") {
        run.planner.multirange = Some(false);
    }
    let (head, dt) = t.head();
    if !head.is_empty() {
        run.planner.seed(0, head.to_vec());
        run.planner.observe(head.len() as u64, dt);
    }
    Ok(run)
}

/// Performs the requests a run asks for until it is done: a remote source's at most `conc` at a
/// time, a local file's one after the other.
pub fn drive(run: &mut Run, t: &Transport, conc: usize) -> Result<(), Fail> {
    while let Some(reqs) = run.poll().map_err(Fail::Rejected)? {
        let workers = if t.remote() && reqs.len() > 1 { conc } else { 1 };
        let results = map(&reqs, workers, |q| t.fetch(&q.spans));
        for (k, r) in results.into_iter().enumerate() {
            let (parts, dt) = r?;
            run.complete(k, parts, dt).map_err(Fail::Rejected)?;
        }
    }
    Ok(())
}

/// Whether the file `t` opened is a VSI file (spec/zarrify/vsi.md §9.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/vsi.md#91-the-vsi-file>), which is
/// zarrified with its tile files (vsi.rs) rather than as a file run.
pub fn is_vsi(t: &Transport) -> Result<bool, Fail> {
    let want = t.size().min(552);
    let (held, _) = t.head();
    let head = if held.len() as u64 >= want { held[..want as usize].to_vec() } else { t.get(0, want)? };
    Ok(Format::sniff(&head) == Some(Format::Vsi))
}

/// (format, output) of the file at `location`, read through `t`: the format its first bytes give
/// ("tiff" becomes "ndpi" once the parser finds the file is NDPI), its output naming the source
/// `url` (default: `location`, or for a local file the path's URI reference).
pub fn zarrify_file(location: &str, url: Option<&str>, t: &Transport, connections: Option<usize>, byte_cost: Option<f64>)
    -> Result<(Format, refkv_ir::out::Out), Fail> {
    let want = t.size().min(552);
    let (held, _) = t.head();
    let head = if held.len() as u64 >= want { held[..want as usize].to_vec() } else { t.get(0, want)? };
    let fmt = Format::sniff(&head).ok_or_else(|| Fail::Rejected(NOT_SUPPORTED.into()))?;
    let mut run = new_run(fmt, t, connections, byte_cost)?;
    drive(&mut run, t, concurrency(t, connections)?)?;
    let fmt = run.format;
    let name = match url {
        Some(u) => u.to_string(),
        None if t.remote() => location.to_string(),
        None => refkv_vzip::uri::path_reference(location).map_err(Fail::Other)?,
    };
    let out = run.into_output(&name, true).map_err(Fail::Rejected)?;
    Ok((fmt, out))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn maps_in_order() {
        let items: Vec<u64> = (0..100).collect();
        for workers in [0, 1, 3, 200] {
            assert_eq!(map(&items, workers, |x| x * 2), items.iter().map(|x| x * 2).collect::<Vec<_>>());
        }
        assert!(map(&[] as &[u8], 4, |x| *x).is_empty());
    }
}
