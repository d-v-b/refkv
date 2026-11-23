//! A zarrifier's output as an archive, as Python writes it: the output's source table and
//! references (python/src/refkv/ir/output.py `Output.to_store`, and
//! python/src/refkv/ir/store.py `StoreIrOutput.to_store` for a store), laid out by
//! the core (`refkv_ir::host::archive`: the references first, in order of their keys, then the
//! bytes entries, a late one, read when the archive opens, the others compressed, 64 KiB pages),
//! and saved by
//! `ReferenceStore.save` (python/src/refkv/refstore.py): its sources checked against the reader
//! policy first, then written by the archive writer (rust/refkv-vzip).

use crate::policy::Policy;
use crate::Fail;
use refkv_ir::host::archive;
use refkv_ir::out::Out;
use refkv_vzip::{Options, Range, Source, Writer, INDEX_KEY, RESERVED_PREFIX, SOURCES_KEY};
use std::fs::File;
use std::collections::HashMap;
use std::io::BufWriter;

/// An archive to write: the source table, the references (key, ranges) and the bytes entries
/// (key, bytes, late), each in the order written, and the ranges whose bytes `checksums` records
/// the CRC-32C of (reference, range): the core's layout (`refkv_ir::host::archive`).
pub struct Archive {
    pub sources: Vec<Source>,
    pub refs: Vec<(String, Vec<Range>)>,
    pub entries: Vec<(String, Vec<u8>, bool)>,
    pub checksummed: Vec<(usize, usize)>,
}

impl From<archive::Layout> for Archive {
    fn from(l: archive::Layout) -> Archive {
        let checksummed = l.checksummed();
        let sources = l
            .sources
            .into_iter()
            .map(|s| match s {
                archive::Source::Url { url, size } => Source { size, ..Source::url(url.as_str()) },
                archive::Source::Data(d) => Source::data(d.as_slice()),
            })
            .collect();
        let refs = l
            .refs
            .into_iter()
            .map(|(k, ranges)| {
                let ranges = ranges
                    .into_iter()
                    .map(|r| match r {
                        archive::Range::Of { source, offset, length } => Range::of(source, offset, length),
                        archive::Range::Literal(b) => Range::literal(b),
                    })
                    .collect();
                (k, ranges)
            })
            .collect();
        Archive { sources, refs, entries: l.entries, checksummed }
    }
}

/// The archive of a file's output: source 0 the file, pinning its size (and its ETag, when given),
/// then the data sources.
pub fn file_archive(out: Out, size: u64, etag: Option<String>) -> Result<Archive, Fail> {
    let mut a = Archive::from(archive::file_layout(out)?);
    a.sources[0].size = Some(size);
    a.sources[0].etag = etag;
    Ok(a)
}

/// The archive of a store's output, and the object key of each url source
/// (`archive::store_layout`).
pub fn store_archive(out: Out, sizes: &HashMap<String, u64>) -> Result<(Archive, Vec<String>), Fail> {
    let l = archive::store_layout(out, sizes)?;
    let objects = l.objects.clone();
    Ok((Archive::from(l), objects))
}

/// Records on each range `a.checksummed` names the CRC-32C of its bytes (spec/refkv.md §4.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#42-range>), which `read(source, offset,
/// length)` reads, at most `workers` reads at a time.
pub fn checksums(a: &mut Archive, workers: usize, read: impl Fn(usize, u64, u64) -> Result<Vec<u8>, Fail> + Sync) -> Result<(), Fail> {
    let jobs: Vec<(usize, usize, usize, u64, u64)> = a
        .checksummed
        .iter()
        .map(|&(i, j)| {
            let r = &a.refs[i].1[j];
            (i, j, r.source as usize, r.offset, r.length)
        })
        .collect();
    let crcs = crate::run::map(&jobs, workers, |&(_, _, s, o, n)| read(s, o, n).map(|b| crc32c::crc32c(&b)));
    for ((i, j, ..), c) in jobs.into_iter().zip(crcs) {
        a.refs[i].1[j].crc32c = Some(c?);
    }
    Ok(())
}

/// The scheme and authority an absolute URL starts with (RFC 3986 §3), if it has them.
fn origin(url: &str) -> Option<&str> {
    let b = url.as_bytes();
    if !b.first()?.is_ascii_alphabetic() {
        return None;
    }
    let i = url.find("://")?;
    if !url[..i].bytes().all(|c| c.is_ascii_alphanumeric() || matches!(c, b'+' | b'.' | b'-')) {
        return None;
    }
    let end = url[i + 3..].find(['/', '?', '#']).map_or(url.len(), |j| i + 3 + j);
    Some(&url[..end])
}

/// What `convert` writes: a vzip archive, a standalone phkv store, or a vzip archive with a phkv
/// container in `__vz__/phkv` (spec/bindings/phkv.md §16.2
/// <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#162-profiles>).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Container {
    #[default]
    Vzip,
    Phkv,
    Hybrid,
}

impl Container {
    pub fn parse(s: &str) -> Option<Container> {
        match s {
            "vzip" => Some(Container::Vzip),
            "phkv" => Some(Container::Phkv),
            "hybrid" => Some(Container::Hybrid),
            _ => None,
        }
    }
}

/// Writes the archive at `path` (`ReferenceStore.save`), after checking every url source, resolved
/// against the archive's own URI, against the reader policy: a source a reader under it could not
/// read refuses the whole archive, and nothing is written.
pub fn save(a: &Archive, path: &str, policy: &Policy, container: Container) -> Result<(), Fail> {
    let base = refkv_vzip::uri::file_uri(std::path::Path::new(path)).map_err(Fail::Other)?;
    let mut by_origin: HashMap<String, Option<String>> = HashMap::new();
    let mut refused = Vec::new();
    for s in &a.sources {
        let Some(url) = s.url_str() else { continue };
        // the policy decides a valid absolute URL by its scheme and host: one check per origin
        let o = origin(url);
        let error = match o.and_then(|o| by_origin.get(o)) {
            Some(e) if refkv_vzip::uri::is_uri_reference(url) => e.clone(),
            _ => {
                
                match refkv_vzip::uri::resolve(&base, url) {
                    Err(e) => Some(e),
                    Ok(resolved) => {
                        let e = policy.check(&base, &resolved).err();
                        if let Some(o) = o {
                            by_origin.insert(o.to_string(), e.clone());
                        }
                        e
                    }
                }
            }
        };
        if let Some(e) = error {
            refused.push(e);
        }
    }
    if let Some(first) = refused.first() {
        return Err(Fail::Refused(format!(
            "not saving {path}: the reader policy given refuses {} of its {} sources, so a reader could not read them. The first: {first}",
            refused.len(),
            a.sources.len()
        )));
    }
    for (k, _) in &a.refs {
        check_key(k)?;
    }
    for (k, ..) in &a.entries {
        check_key(k)?;
    }
    let f = std::fs::File::create(path).map_err(|e| Fail::Other(format!("{path}: {e}")))?;
    let out = BufWriter::with_capacity(1 << 20, f);
    let zip = Options { mirror_refs: true, page_size: Some(archive::PAGE_SIZE) };
    let written = match container {
        Container::Vzip => write(a, Writer::new(out, zip)),
        Container::Hybrid => {
            let w = refkv_phkv::HybridWriter::new(out, zip, refkv_phkv::Options::default()).map_err(|e| Fail::Other(e.to_string()))?;
            write(a, w)
        }
        Container::Phkv => write(a, refkv_phkv::Writer::new(out, refkv_phkv::Options::default())),
    };
    if let Err(e) = written {
        let _ = std::fs::remove_file(path); // no partial archive
        return Err(e);
    }
    Ok(())
}

fn check_key(k: &str) -> Result<(), Fail> {
    if k.is_empty() || k == SOURCES_KEY || k == INDEX_KEY {
        return Err(Fail::Other(format!("{k:?} is not a key a store may hold")));
    }
    Ok(())
}

/// What the archive is written to: the vzip writer, or one of phkv's.
trait Sink: Sized {
    fn source(&mut self, s: Source) -> Result<u32, String>;
    fn add_ranges(&mut self, key: &str, ranges: Vec<Range>) -> Result<(), String>;
    /// A bytes entry, compressed unless `late` (a small document a reader fetches when opening).
    fn add_bytes(&mut self, key: &str, data: &[u8], late: bool) -> Result<(), String>;
    fn add_hidden(&mut self, name: &str, data: &[u8], late: bool) -> Result<(), String>;
    fn finish(self) -> Result<BufWriter<File>, String>;
}

impl Sink for Writer<BufWriter<File>> {
    fn source(&mut self, s: Source) -> Result<u32, String> {
        Writer::source(self, s).map_err(|e| e.to_string())
    }

    fn add_ranges(&mut self, key: &str, ranges: Vec<Range>) -> Result<(), String> {
        Writer::add_ranges(self, key, ranges).map_err(|e| e.to_string())
    }

    fn add_bytes(&mut self, key: &str, data: &[u8], late: bool) -> Result<(), String> {
        Writer::add_bytes(self, key, data, !late, late).map_err(|e| e.to_string())
    }

    fn add_hidden(&mut self, name: &str, data: &[u8], late: bool) -> Result<(), String> {
        Writer::add_hidden(self, name, data, !late, late).map(|_| ()).map_err(|e| e.to_string())
    }

    fn finish(self) -> Result<BufWriter<File>, String> {
        self.close().map_err(|e| e.to_string())
    }
}

impl Sink for refkv_phkv::HybridWriter<BufWriter<File>> {
    fn source(&mut self, s: Source) -> Result<u32, String> {
        refkv_phkv::HybridWriter::source(self, s).map_err(|e| e.to_string())
    }

    fn add_ranges(&mut self, key: &str, ranges: Vec<Range>) -> Result<(), String> {
        refkv_phkv::HybridWriter::add_ranges(self, key, ranges).map_err(|e| e.to_string())
    }

    fn add_bytes(&mut self, key: &str, data: &[u8], late: bool) -> Result<(), String> {
        refkv_phkv::HybridWriter::add_bytes(self, key, data, !late, late).map_err(|e| e.to_string())
    }

    fn add_hidden(&mut self, name: &str, data: &[u8], late: bool) -> Result<(), String> {
        refkv_phkv::HybridWriter::add_hidden(self, name, data, !late, late).map(|_| ()).map_err(|e| e.to_string())
    }

    fn finish(self) -> Result<BufWriter<File>, String> {
        self.close().map_err(|e| e.to_string())
    }
}

/// A standalone phkv store: values Zstandard-compressed, but the small documents a reader fetches
/// when opening, which are stored as they are, inline in their records when short.
impl Sink for refkv_phkv::Writer<BufWriter<File>> {
    fn source(&mut self, s: Source) -> Result<u32, String> {
        refkv_phkv::Writer::source(self, s).map_err(|e| e.to_string())
    }

    fn add_ranges(&mut self, key: &str, ranges: Vec<Range>) -> Result<(), String> {
        refkv_phkv::Writer::add_ranges(self, key, &ranges).map_err(|e| e.to_string())
    }

    fn add_bytes(&mut self, key: &str, data: &[u8], late: bool) -> Result<(), String> {
        let codec = if late { refkv_phkv::Codec::None } else { refkv_phkv::Codec::Zstd };
        refkv_phkv::Writer::add_bytes_with(self, key, data, codec).map_err(|e| e.to_string())
    }

    fn add_hidden(&mut self, name: &str, _: &[u8], _: bool) -> Result<(), String> {
        Err(format!("{RESERVED_PREFIX}{name}: a standalone phkv store has no hidden entries"))
    }

    fn finish(self) -> Result<BufWriter<File>, String> {
        self.close().map_err(|e| e.to_string())
    }
}

fn write(a: &Archive, mut w: impl Sink) -> Result<(), Fail> {
    let invalid = Fail::Other;
    let mut keys = std::collections::HashSet::new();
    for k in a.refs.iter().map(|r| &r.0).chain(a.entries.iter().map(|e| &e.0)) {
        if !keys.insert(k.as_str()) {
            return Err(Fail::Other(format!("duplicate key {k:?}")));
        }
    }
    // equal sources share an index
    let index = a.sources.iter().map(|s| w.source(s.clone())).collect::<Result<Vec<u32>, _>>().map_err(invalid)?;
    for (k, ranges) in &a.refs {
        let ranges = ranges
            .iter()
            .map(|r| {
                if r.data.is_some() {
                    return Ok(r.clone());
                }
                match index.get(r.source as usize) {
                    Some(&i) => Ok(Range { source: i, ..r.clone() }),
                    None => Err(Fail::Other(format!("{k:?}: a range uses source {}; the table has {}", r.source, index.len()))),
                }
            })
            .collect::<Result<Vec<_>, _>>()?;
        w.add_ranges(k, ranges).map_err(invalid)?;
    }
    for (k, v, late) in &a.entries {
        if let Some(name) = k.strip_prefix(RESERVED_PREFIX) {
            w.add_hidden(name, v, *late).map_err(invalid)?;
        } else {
            w.add_bytes(k, v, *late).map_err(invalid)?;
        }
    }
    let out = w.finish().map_err(invalid)?;
    out.into_inner().map_err(|e| Fail::Other(e.to_string()))?.sync_all().map_err(|e| Fail::Other(e.to_string()))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn origins() {
        assert_eq!(origin("http://h:1/a?b"), Some("http://h:1"));
        assert_eq!(origin("https://h"), Some("https://h"));
        assert_eq!(origin("file:///x"), Some("file://"));
        assert_eq!(origin("a/b"), None);
        assert_eq!(origin("1a://h/"), None);
    }

    #[test]
    fn refuses_a_private_source_without_writing() {
        let dir = std::env::temp_dir().join(format!("refkv-cli-save-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("a.vzip").to_string_lossy().into_owned();
        let a = Archive { sources: vec![Source::url("http://127.0.0.1/x")], refs: vec![], entries: vec![], checksummed: vec![] };
        assert!(matches!(save(&a, &path, &Policy::default(), Container::Vzip), Err(Fail::Refused(_))));
        assert!(!std::path::Path::new(&path).exists());
        save(&a, &path, &Policy { allow_private_hosts: true }, Container::Vzip).unwrap();
        assert!(std::path::Path::new(&path).exists());
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn refuses_an_invalid_source_url() {
        let a = Archive { sources: vec![Source::url("a b.tif")], refs: vec![], entries: vec![], checksummed: vec![] };
        assert!(matches!(save(&a, "never.vzip", &Policy::default(), Container::Vzip), Err(Fail::Refused(_))));
    }

    #[test]
    fn refuses_a_duplicate_key() {
        let dir = std::env::temp_dir().join(format!("refkv-cli-dup-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("a.vzip").to_string_lossy().into_owned();
        let a = Archive { sources: vec![], refs: vec![("k".into(), vec![Range::literal(vec![1])])], entries: vec![("k".into(), vec![], false)], checksummed: vec![] };
        assert!(matches!(save(&a, &path, &Policy::default(), Container::Vzip), Err(Fail::Other(_))));
        assert!(!std::path::Path::new(&path).exists());
        std::fs::remove_dir_all(&dir).unwrap();
    }

    /// The archive the container tests write: a reference to a url source, a value written
    /// compressed, and a late document.
    fn small_archive() -> Archive {
        Archive {
            sources: vec![Source { size: Some(100), ..Source::url("https://example.org/o.bin") }],
            refs: vec![("c/0".into(), vec![Range::of(0, 10, 20)])],
            entries: vec![("c/1".into(), vec![7; 1000], false), ("zarr.json".into(), b"{}".to_vec(), true)],
            checksummed: vec![],
        }
    }

    /// Saves `a` as `container` in a new directory, and gives the directory and the file's path.
    fn save_in_dir(name: &str, a: &Archive, container: Container) -> (std::path::PathBuf, String, Result<(), Fail>) {
        let dir = std::env::temp_dir().join(format!("refkv-cli-{name}-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("a.out").to_string_lossy().into_owned();
        let r = save(a, &path, &Policy::default(), container);
        (dir, path, r)
    }

    /// Reads `path` back with the phkv reader, and checks it holds `small_archive`, the values
    /// with `codec`, and the late document as it is.
    fn check_phkv(path: &str, codec: refkv_phkv::Codec) {
        let s = refkv_phkv::Store::open(File::open(path).unwrap()).unwrap();
        assert_eq!(s.list("").unwrap(), ["c/0", "c/1", "zarr.json"]);
        assert_eq!(s.classify("c/0").unwrap(), Some(refkv_phkv::Kind::Reference));
        assert_eq!(s.sources()[0], Source { size: Some(100), ..Source::url("https://example.org/o.bin") });
        let get = |k| s.get(k, refkv_phkv::Request::Whole, &refkv_phkv::NoUrls).unwrap().unwrap();
        assert_eq!(get("c/1"), vec![7; 1000]);
        assert_eq!(get("zarr.json"), b"{}");
        let info = |k| match s.entry_info(k).unwrap() {
            Some(refkv_phkv::EntryInfo::Bytes { codec, .. }) => codec,
            _ => panic!("{k} is not a bytes entry"),
        };
        assert_eq!(info("c/1"), codec);
        assert_eq!(info("zarr.json"), refkv_phkv::Codec::None);
    }

    #[test]
    fn saves_a_standalone_phkv_store() {
        let (dir, path, r) = save_in_dir("phkv", &small_archive(), Container::Phkv);
        r.unwrap();
        check_phkv(&path, refkv_phkv::Codec::Zstd);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn saves_a_hybrid_store() {
        let mut a = small_archive();
        a.entries.push((format!("{RESERVED_PREFIX}extra"), b"x".to_vec(), false));
        let (dir, path, r) = save_in_dir("hybrid", &a, Container::Hybrid);
        r.unwrap();
        check_phkv(&path, refkv_phkv::Codec::Deflate);
        // the hidden entry is in the archive, but not in the store's mapping
        let s = refkv_phkv::Store::open(File::open(&path).unwrap()).unwrap();
        assert_eq!(s.classify(&format!("{RESERVED_PREFIX}extra")).unwrap(), None);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn refuses_a_hidden_entry_in_a_standalone_phkv_store() {
        let mut a = small_archive();
        a.entries.push((format!("{RESERVED_PREFIX}extra"), b"x".to_vec(), false));
        let (dir, path, r) = save_in_dir("phkv-hidden", &a, Container::Phkv);
        assert!(matches!(r, Err(Fail::Other(_))));
        assert!(!std::path::Path::new(&path).exists());
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
