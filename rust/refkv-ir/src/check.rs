//! The invariants (design/ARCHITECTURE.md §3.3
//! <https://github.com/d-v-b/refkv/blob/main/design/ARCHITECTURE.md#33-invariants>) over an IR with
//! runs: injectivity and
//! coverage (`finish` establishes them, `check` verifies them), and the leaves in
//! source order (the rebuild).
//!
//! **How runs are checked.** A run's leaves are its *family*: member 0's leaves,
//! repeated `count` times at `stride`. A family whose member-0 leaves overlap one
//! another or span more than the stride is invalid. Families of one stride whose
//! member-0 leaves lie in one window `[a, a + stride)` form a *tile*. A tile is
//! **complete** when its member-0 leaves partition the window and its counts are
//! `c` for a prefix `[0, t)` of the window and `c` or `c − 1` for the rest: its
//! members then partition exactly `[a, a + (c − 1)·stride + t)` (`t` = stride when
//! all counts are `c`), which the check treats as one leaf, a **block**. Any
//! other tile is checked member by member (its members are expanded, within the
//! budget). Coverage and injectivity are then the round-1 sweep over the blocks
//! and the other leaves, sorted by offset (then by element id). The rebuild is
//! the same sweep: a block is one range.
//!
//! `finish` fills each incomplete tile that nothing else overlaps with gap runs
//! (count `c` for the holes before the last leaf of the window, `c − 1` for the
//! hole after it), so a parser's runs stay runs; it unfolds a run whose members
//! overlap another claim, and makes every other overlapping leaf an alias of its
//! first claimant.
//!
//! **Stores.** A store IR (conventions §8.9
//! <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#89-stores>) has many sources, its
//! objects. The checks run on **virtual offsets**: the objects end to end, in id order, each
//! byte of object `o` at `x` at `base(o) + x` ([`Sources`]). A referenced object (one of a
//! length with no children) is one leaf, its whole source; a held object's leaves partition it.
//! So the sweep over virtual offsets checks every object's coverage and injectivity at once,
//! once [`store_shape`] has checked that no leaf (nor run member) leaves its object. Tiles never
//! join families of two objects, and the gaps `finish` adds go to the object they are in.

use crate::ir::*;

#[derive(Debug, Clone)]
pub struct Fam {
    pub root: u32,
    /// the object the run is in (NONE: a file IR)
    pub obj: u32,
    pub count: u64,
    pub ext: Vec<(u64, u64, u32)>, // member 0's leaves: (start, len, id), sorted
}

#[derive(Debug, Clone)]
pub struct Tile {
    pub a0: u64,
    pub stride: u64,
    pub fams: Vec<Fam>,
}

impl Tile {
    /// (relative start, len, count, id) of member 0's leaves, sorted.
    fn rel(&self) -> Vec<(u64, u64, u64, u32)> {
        let mut v: Vec<_> = self
            .fams
            .iter()
            .flat_map(|f| {
                f.ext
                    .iter()
                    .map(move |&(s, n, id)| (s - self.a0, n, f.count, id))
            })
            .collect();
        v.sort();
        v
    }

    /// The length of the region the members partition, when the tile is complete.
    pub fn complete(&self) -> Option<u64> {
        let v = self.rel();
        let mut pos = 0;
        for &(r, n, _, _) in &v {
            if r != pos {
                return None;
            }
            pos += n;
        }
        if pos != self.stride || v.is_empty() {
            return None;
        }
        let c = v[0].2;
        let mut t = None;
        for &(r, _, k, _) in &v {
            if k == c {
                if t.is_some() {
                    return None; // a count c after a count c - 1
                }
            } else if k + 1 == c && c > 1 {
                t.get_or_insert(r);
            } else {
                return None;
            }
        }
        Some(match t {
            None => c * self.stride,
            Some(t) => (c - 1) * self.stride + t,
        })
    }

    /// For a tile of one count: the region it would cover, and its holes as
    /// (relative start, len, count).
    fn holes(&self) -> Option<(u64, Vec<(u64, u64, u64)>)> {
        let c = self.fams[0].count;
        if self.fams.iter().any(|f| f.count != c) {
            return None;
        }
        let v = self.rel();
        let last = v.iter().map(|&(r, n, _, _)| r + n).max().unwrap_or(0);
        let mut holes = Vec::new();
        let mut pos = 0;
        for &(r, n, _, _) in &v {
            if r > pos {
                holes.push((pos, r - pos, c));
            }
            pos = pos.max(r + n);
        }
        if last < self.stride && c > 1 {
            holes.push((last, self.stride - last, c - 1));
        }
        Some(((c - 1) * self.stride + last, holes))
    }

    fn region(&self) -> u64 {
        if let Some(n) = self.complete() {
            return n;
        }
        let c = self.fams.iter().map(|f| f.count).max().unwrap_or(1);
        let last = self
            .rel()
            .iter()
            .map(|&(r, n, _, _)| r + n)
            .max()
            .unwrap_or(0);
        (c - 1) * self.stride + last
    }
}

pub struct Layout {
    pub singles: Vec<(u64, u64, u32)>,
    pub tiles: Vec<Tile>,
    /// runs whose members overlap one another (or span more than the stride)
    pub bad: Vec<u32>,
    pub src: Sources,
}

/// Whether element `i` is a leaf (conventions §8.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#81-elements>, §8.9
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#89-stores>): a value, data, derived
/// element or gap with a length in space 0, or a referenced object (`kids`: whether each element
/// has children).
pub fn claims(ir: &Ir, kids: &[bool], i: usize) -> bool {
    if ir.len[i] == 0 || ir.space[i] != 0 {
        return false;
    }
    is_leaf(ir.kind[i]) || (ir.kind[i] == OBJECT && !kids.get(i).copied().unwrap_or(false))
}

pub fn layout(ir: &Ir) -> Layout {
    let rr = ir.run_roots();
    let src = ir.sources();
    let kids = if ir.store { ir.has_children() } else { Vec::new() };
    let mut singles = Vec::new();
    let mut fams: std::collections::BTreeMap<u32, Vec<(u64, u64, u32)>> = Default::default();
    for i in 0..ir.len() {
        if !claims(ir, &kids, i) {
            continue;
        }
        let e = (src.vstart(ir, i), ir.len[i], i as u32);
        if rr[i] == NONE {
            singles.push(e);
        } else {
            fams.entry(rr[i]).or_default().push(e);
        }
    }
    let mut bad = Vec::new();
    let mut good: Vec<(u64, u64, Fam)> = Vec::new(); // (stride, base, family)
    for (root, mut ext) in fams {
        let (count, stride) = ir.run(root).unwrap();
        ext.sort();
        let ok = ext.windows(2).all(|w| w[0].0 + w[0].1 <= w[1].0)
            && ext.last().unwrap().0 + ext.last().unwrap().1 - ext[0].0 <= stride;
        if !ok {
            bad.push(root);
            continue;
        }
        let obj = src.of.get(root as usize).copied().unwrap_or(NONE);
        good.push((stride, ext[0].0, Fam { root, obj, count, ext }));
    }
    good.sort_by_key(|g| (g.0, g.1, g.2.root));
    let mut tiles: Vec<Tile> = Vec::new();
    for (stride, base, fam) in good {
        if let Some(t) = tiles.last_mut() {
            let end = fam.ext.last().map(|e| e.0 + e.1).unwrap_or(base);
            let overlaps = t
                .fams
                .iter()
                .flat_map(|f| f.ext.iter())
                .any(|&(s, n, _)| fam.ext.iter().any(|&(s2, n2, _)| s < s2 + n2 && s2 < s + n));
            if t.stride == stride && t.fams[0].obj == fam.obj && base >= t.a0 && end <= t.a0 + stride && !overlaps {
                t.fams.push(fam);
                continue;
            }
        }
        tiles.push(Tile {
            a0: base,
            stride,
            fams: vec![fam],
        });
    }
    Layout {
        singles,
        tiles,
        bad,
        src,
    }
}

/// Where the virtual offset `v` of object `obj` (NONE: a file IR, or unknown) is: (the
/// element a gap there goes under, its offset in that source).
fn local(ir: &Ir, src: &Sources, obj: u32, v: u64) -> (u32, u64) {
    let o = if obj != NONE { Some((obj, src.base[obj as usize])) } else { src.at(ir, v) };
    match o {
        Some((o, b)) => (o, v - b),
        None => (0, v),
    }
}

/// The virtual range [a, a + n) cut at the objects' ends: (object, offset in it, length)
/// (one piece of the root, element 0, in a file IR).
fn pieces(ir: &Ir, src: &Sources, mut a: u64, mut n: u64) -> Res<Vec<(u32, u64, u64)>> {
    if !ir.store {
        return Ok(vec![(0, a, n)]);
    }
    let mut v = Vec::new();
    while n > 0 {
        let (o, b) = src.at(ir, a).ok_or(format!("virtual byte {a} is in no object"))?;
        let end = b + ir.len[o as usize];
        if a >= end {
            return Err(format!("virtual byte {a} is in no object"));
        }
        let m = n.min(end - a);
        v.push((o, a - b, m));
        a += m;
        n -= m;
    }
    Ok(v)
}

/// The virtual range [s, e) as text: bytes of the source, or of an object of a store.
fn bytes_text(ir: &Ir, src: &Sources, s: u64, e: u64) -> String {
    match ir.store.then(|| src.at(ir, s)).flatten() {
        Some((o, b)) => format!("bytes [{}, {}) of object {:?}", s - b, e.saturating_sub(b), ir.key(o)),
        None => format!("bytes [{s}, {e})"),
    }
}

/// The shape of a store IR (conventions §8.9
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#89-stores>), or of a file IR's
/// lack of objects: outside its objects a store IR has only the root and directories (structs
/// with no extent, in space 0, not runs); an object's parent is the root or a directory, its
/// extent is `(0, size)` in space 0, and it is not a run; an empty object (size 0) has no
/// element with a length; every other element with a length in space 0, each member of a run
/// included, ends within its object; and the objects number `ir.objects`. Parents precede
/// their children (checked first).
pub fn store_shape(ir: &Ir) -> Res<()> {
    let n = ir.len();
    if !ir.store {
        if let Some(i) = (0..n).find(|&i| ir.kind[i] == OBJECT) {
            return Err(format!("{} is an object, in a file IR", who(ir, i as u32)));
        }
        return Ok(());
    }
    let src = ir.sources();
    if src.objects.len() as u64 != ir.objects {
        return Err(format!("the store IR has {} objects, its description {}", src.objects.len(), ir.objects));
    }
    if src.objects.len() as u64 > MAX_OBJECTS {
        return Err(format!("budget: more than {MAX_OBJECTS} objects"));
    }
    for i in 1..n {
        let o = src.of[i];
        let (k, iu) = (ir.kind[i], i as u32);
        if ir.parent[i] == NONE || ir.parent[i] as usize >= i {
            return Err(format!("{} has parent {}", who(ir, iu), ir.parent[i]));
        }
        if k == OBJECT {
            let p = ir.parent[i] as usize;
            if src.of[p] != NONE {
                return Err(format!("{} is inside object {:?}", who(ir, iu), ir.key(src.of[p])));
            }
            if ir.start[i] != 0 || ir.space[i] != 0 || ir.run(iu).is_some() {
                return Err(format!("{} does not span its source (extent ({}, {}), space {})", who(ir, iu), ir.start[i], ir.len[i], ir.space[i]));
            }
            continue;
        }
        if o == NONE {
            if k != STRUCT || ir.len[i] != 0 || ir.space[i] != 0 || ir.run(iu).is_some() {
                return Err(format!("{} is outside every object of the store, and is not a directory", who(ir, iu)));
            }
            continue;
        }
        if ir.space[i] != 0 || ir.len[i] == 0 {
            continue;
        }
        let size = ir.len[o as usize];
        if size == 0 {
            return Err(format!("{} has a length in the empty object {:?}", who(ir, iu), ir.key(o)));
        }
        let (count, stride) = ir.run(iu).unwrap_or((1, 0));
        let end = (count - 1).checked_mul(stride).and_then(|x| x.checked_add(ir.start[i])).and_then(|x| x.checked_add(ir.len[i]));
        if end.is_none_or(|e| e > size) {
            return Err(format!("{} claims bytes past the end of object {:?} ({size} bytes)", who(ir, iu), ir.key(o)));
        }
    }
    // a run's members below its root lie in the root's object, as the root does
    Ok(())
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum What {
    Single,
    Block(usize),
    Member(u64), // member j of the run the element belongs to
}

/// The sweep's items: (start, end, element, what), sorted. Incomplete tiles (and,
/// with `expand_all`, every tile intersected by something else) are expanded.
fn items(ir: &Ir, lay: &Layout, blocks: &[bool], limit: u64, rank: Option<&[u32]>) -> Res<Vec<(u64, u64, u32, What)>> {
    let mut v: Vec<(u64, u64, u32, What)> = lay
        .singles
        .iter()
        .map(|&(s, n, id)| (s, s + n, id, What::Single))
        .collect();
    let mut expanded = 0u64;
    for (k, t) in lay.tiles.iter().enumerate() {
        if blocks[k] {
            let n = t.region();
            let id = t.fams[0].ext[0].2;
            v.push((t.a0, t.a0 + n, id, What::Block(k)));
            continue;
        }
        for f in &t.fams {
            let (_, stride) = ir.run(f.root).unwrap();
            expanded += f.count * f.ext.len() as u64;
            if expanded > limit {
                return Err(format!(
                    "budget: more than {limit} leaves of runs to check one by one"
                ));
            }
            for j in 0..f.count {
                for &(s, n, id) in &f.ext {
                    v.push((s + j * stride, s + j * stride + n, id, What::Member(j)));
                }
            }
        }
    }
    // of leaves that start at one byte, the first in the canonical order claims it
    v.sort_by_key(|x| (x.0, rank.map_or(x.2, |r| r[x.2 as usize]), x.3));
    Ok(v)
}

/// Which tiles something else intersects (their would-be regions overlap another
/// leaf or tile).
fn intersected(lay: &Layout) -> Vec<bool> {
    let mut iv: Vec<(u64, u64, i64)> = lay
        .singles
        .iter()
        .map(|&(s, n, _)| (s, s + n, -1))
        .collect();
    for (k, t) in lay.tiles.iter().enumerate() {
        iv.push((t.a0, t.a0 + t.region(), k as i64));
    }
    iv.sort();
    let mut hit = vec![false; lay.tiles.len()];
    let mut best: Option<(u64, i64)> = None; // (end, owner) of the interval reaching furthest
    for &(s, e, who) in &iv {
        if let Some((end, owner)) = best {
            if s < end {
                if who >= 0 {
                    hit[who as usize] = true;
                }
                if owner >= 0 {
                    hit[owner as usize] = true;
                }
            }
            if e > end {
                best = Some((e, who));
            }
        } else {
            best = Some((e, who));
        }
    }
    hit
}

fn who(ir: &Ir, i: u32) -> String {
    format!(
        "element {i} ({} {:?})",
        KINDS[ir.kind[i as usize] as usize],
        ir.path(i)
    )
}

fn expansion_limit(ir: &Ir) -> u64 {
    ir.budget
        .as_ref()
        .map(|b| b.records)
        .unwrap_or((1 << 22) + ir.size / 4)
}

/// Injectivity, then coverage: claims of claimed bytes become aliases of their
/// first claimant (a run that overlaps is unfolded first), and the bytes nobody
/// claims become gaps (gap runs inside a tile).
pub fn finish(ir: &mut Ir) -> Res<()> {
    store_shape(ir)?;
    let mut rounds = 0;
    loop {
        rounds += 1;
        if rounds > 64 {
            return Err("finish: too many runs to unfold".into());
        }
        let lay = layout(ir);
        if let Some(&r) = lay.bad.first() {
            ir.unfold(r)?;
            continue;
        }
        let hit = intersected(&lay);
        // fill the holes of tiles nothing else reaches
        let mut blocks = vec![false; lay.tiles.len()];
        let mut filled = false;
        for (k, t) in lay.tiles.iter().enumerate() {
            if t.complete().is_some() {
                blocks[k] = !hit[k];
                continue;
            }
            if hit[k] {
                continue;
            }
            if let Some((_, holes)) = t.holes() {
                for (r, n, c) in holes {
                    let (parent, o) = local(ir, &lay.src, t.fams[0].obj, t.a0 + r);
                    let g = ir.add(GAP, parent, &format!("gaps/{o}/"), 0, "", 0, Some((o, n)))?;
                    if c > 1 {
                        let pos = ir.runs.partition_point(|e| e.0 < g);
                        ir.runs.insert(pos, (g, c, t.stride));
                    }
                }
                filled = true;
            }
        }
        if filled {
            continue; // the tiles are complete now: lay them out again
        }
        ir.build_children();
        let (_, rank) = crate::canon::order(ir);
        let v = items(ir, &lay, &blocks, expansion_limit(ir), Some(&rank))?;
        let mut pos = 0u64;
        let mut owner = NONE;
        let mut owner_what = What::Single;
        let mut gaps = Vec::new();
        let mut unfold = None;
        for &(s, e, id, what) in &v {
            if ir.kind[id as usize] == ALIAS {
                continue;
            }
            if s < pos {
                match what {
                    // a leaf over a run's member: the run is unfolded, so that the alias
                    // names the element that claims the byte
                    What::Single if owner_what != What::Single => {
                        unfold = Some(ir.run_roots()[owner as usize]);
                        break;
                    }
                    What::Single => {
                        let k = ir.kind[id as usize];
                        ir.kind[id as usize] = ALIAS;
                        let at = ir.was.partition_point(|x| x.0 < id);
                        ir.was.insert(at, (id, k));
                        let at = ir.targets.partition_point(|x| x.0 < id);
                        ir.targets.insert(at, (id, owner));
                    }
                    _ => {
                        unfold = Some(ir.run_roots()[id as usize]);
                        break;
                    }
                }
                continue;
            }
            if s > pos {
                gaps.push((pos, s - pos));
            }
            pos = e;
            owner = id;
            owner_what = what;
        }
        if let Some(r) = unfold {
            ir.unfold(r)?;
            continue;
        }
        if pos < ir.size {
            gaps.push((pos, ir.size - pos));
        }
        for (a, n) in gaps {
            // named `gaps/<offset>`: the offset is the name's index, so that gaps fold
            for (parent, o, n) in pieces(ir, &lay.src, a, n)? {
                ir.add(GAP, parent, "gaps/", o, "", 0, Some((o, n)))?;
            }
        }
        break;
    }
    ir.finished = true;
    ir.build_children();
    Ok(())
}

/// The sweep of a finished IR: its leaves (blocks for complete tiles) in source order.
fn sweep(ir: &Ir) -> Res<(Vec<(u64, u64, u32, What)>, Sources)> {
    let lay = layout(ir);
    if let Some(&r) = lay.bad.first() {
        return Err(format!(
            "the members of run {} overlap one another",
            who(ir, r)
        ));
    }
    let blocks: Vec<bool> = lay.tiles.iter().map(|t| t.complete().is_some()).collect();
    Ok((items(ir, &lay, &blocks, expansion_limit(ir), None)?, lay.src))
}

fn who_member(ir: &Ir, i: u32, j: u64) -> String {
    match j {
        0 => who(ir, i),
        j => format!("member {j} of the run {}", who(ir, i)),
    }
}

/// The root and the names (conventions §8.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#81-elements>). Element 0 is the
/// **root**: a struct named `""` with no name index, in space 0, whose extent is `(0, size)`, the
/// whole source, and which is not a run. Every other element has a full name that
/// is not empty. Among the children of one element (each member of a run a child),
/// no full name is another's, or another's followed by `/` and more: so every
/// element's path is its own, and the root's, `""`, is no other's.
pub fn names(ir: &Ir) -> Res<()> {
    let n = ir.len();
    if n == 0 {
        return Err("the IR has no root (element 0)".into());
    }
    let kind = ir.kind[0];
    if kind != STRUCT {
        let k = KINDS.get(kind as usize).copied().unwrap_or("element of no kind");
        return Err(format!("the root (element 0) is a {k}, not a struct"));
    }
    let name = ir.names.get(ir.name[0]);
    if !name.is_empty() {
        return Err(format!("the root (element 0) is named {name:?}, not \"\""));
    }
    if ir.nidx[0] != NO_INDEX {
        return Err(format!("the root (element 0) has the name index {}", ir.nidx[0]));
    }
    if ir.space[0] != 0 {
        return Err(format!("the root (element 0) is in space {}, not 0", ir.space[0]));
    }
    if ir.store && (ir.start[0], ir.len[0]) != (0, 0) {
        return Err(format!("the root's extent is ({}, {}): a store's root has none", ir.start[0], ir.len[0]));
    }
    if !ir.store && (ir.start[0], ir.len[0]) != (0, ir.size) {
        return Err(format!(
            "the root's extent is ({}, {}), not (0, {}): the root spans the source",
            ir.start[0], ir.len[0], ir.size
        ));
    }
    if ir.run(0).is_some() {
        return Err("the root (element 0) is a run".into());
    }
    let limit = expansion_limit(ir);
    let mut extra = 0u64;
    // (parent, full name, element, member)
    let mut v: Vec<(u32, String, u32, u64)> = Vec::with_capacity(n);
    for i in 1..n as u32 {
        let iu = i as usize;
        let (count, _) = ir.run(i).unwrap_or((1, 0));
        extra = extra.saturating_add(count.saturating_sub(1));
        if extra > limit {
            return Err(format!("budget: more than {limit} members of runs to name"));
        }
        for j in 0..count.max(1) {
            let full = if j == 0 {
                ir.name_of(i)
            } else {
                match ir.nidx[iu].checked_add(j).filter(|&x| x != NO_INDEX && ir.nidx[iu] != NO_INDEX) {
                    Some(x) => format!("{}{x}", ir.names.get(ir.name[iu])),
                    None => ir.name_of(i), // no index to increase: member 0's name again
                }
            };
            if full.is_empty() {
                return Err(format!("{} has an empty full name", who_member(ir, i, j)));
            }
            v.push((ir.parent[iu], full, i, j));
        }
    }
    v.sort_unstable();
    let path = |p: u32, full: &str| -> String {
        if p == 0 { full.to_string() } else { format!("{}/{full}", ir.path(p)) }
    };
    let mut a = 0;
    while a < v.len() {
        let p = v[a].0;
        let b = a + v[a..].partition_point(|e| e.0 == p);
        let group = &v[a..b];
        for k in 0..group.len() {
            let (_, f, i, j) = &group[k];
            if let Some((_, g, i2, j2)) = group.get(k + 1).filter(|e| &e.1 == f) {
                return Err(format!(
                    "{} and {} have one path, {:?}",
                    who_member(ir, *i, *j),
                    who_member(ir, *i2, *j2),
                    path(p, g)
                ));
            }
            let key = format!("{f}/");
            let at = group.partition_point(|e| e.1.as_str() < key.as_str());
            if let Some((_, g, i2, j2)) = group.get(at).filter(|e| e.1.starts_with(&key)) {
                return Err(format!(
                    "the full name {g:?} of {} starts with that of its sibling {} and \"/\"",
                    who_member(ir, *i2, *j2),
                    who_member(ir, *i, *j)
                ));
            }
        }
        a = b;
    }
    Ok(())
}

/// Coverage and injectivity (the leaves partition [0, size)); parents precede
/// their children; aliases name an element; the root and the names (`names`);
/// each derived space with a size is partitioned by its leaves.
pub fn check(ir: &Ir) -> Res<()> {
    let n = ir.len();
    for i in 0..n {
        let p = ir.parent[i];
        if (i == 0) != (p == NONE) || (p != NONE && p as usize >= i) {
            return Err(format!("{} has parent {p}", who(ir, i as u32)));
        }
        if ir.kind[i] == ALIAS {
            let mut seen = 0;
            let mut j = ir.target(i as u32);
            while j != NONE && (j as usize) < n && ir.kind[j as usize] == ALIAS && seen < 64 {
                j = ir.target(j);
                seen += 1;
            }
            if j == NONE || j as usize >= n || seen >= 64 {
                return Err(format!(
                    "{} names no element (target {})",
                    who(ir, i as u32),
                    ir.target(i as u32)
                ));
            }
        }
    }
    names(ir)?;
    store_shape(ir)?;
    let (v, src) = sweep(ir)?;
    let mut pos = 0u64;
    let mut last = NONE;
    for &(s, e, id, _) in &v {
        if ir.kind[id as usize] == ALIAS {
            continue;
        }
        if e > ir.size {
            return Err(format!(
                "{} claims [{s}, {e}), outside [0, {})",
                who(ir, id),
                ir.size
            ));
        }
        if s < pos {
            return Err(format!(
                "{} are claimed by {} and {}",
                bytes_text(ir, &src, s, pos.min(e)),
                who(ir, last),
                who(ir, id)
            ));
        }
        if s > pos {
            return Err(format!(
                "{} are claimed by no element (next: {})",
                bytes_text(ir, &src, pos, s),
                who(ir, id)
            ));
        }
        pos = e;
        last = id;
    }
    if pos != ir.size {
        return Err(format!(
            "{} are claimed by no element",
            bytes_text(ir, &src, pos, ir.size)
        ));
    }
    check_derived(ir)
}

/// Each derived element whose form gives its derived size: its leaves partition it.
fn check_derived(ir: &Ir) -> Res<()> {
    let mut spaces: std::collections::BTreeMap<u32, Vec<(u64, u64, u32)>> = Default::default();
    for i in 0..ir.len() {
        if ir.space[i] != 0 && is_leaf(ir.kind[i]) && ir.len[i] > 0 {
            spaces
                .entry(ir.space[i])
                .or_default()
                .push((ir.start[i], ir.len[i], i as u32));
        }
    }
    for i in 0..ir.len() as u32 {
        if ir.kind[i as usize] != DERIVED {
            continue;
        }
        let Some(form) = ir.form(i) else { continue };
        let Ok(j) = serde_json::from_str::<serde_json::Value>(form) else {
            continue;
        };
        let Some(size) = j.get("size").and_then(|x| x.as_u64()) else {
            continue;
        };
        let mut v = spaces.remove(&i).unwrap_or_default();
        v.sort();
        let mut pos = 0;
        for (s, n, id) in v {
            if s != pos {
                return Err(format!(
                    "derived bytes [{}, {}) of {}: claimed twice or by no element (at {})",
                    pos.min(s),
                    pos.max(s),
                    who(ir, i),
                    who(ir, id)
                ));
            }
            pos = s + n;
        }
        if pos != size {
            return Err(format!(
                "derived bytes [{pos}, {size}) of {} are claimed by no element",
                who(ir, i)
            ));
        }
    }
    Ok(())
}

/// The leaves in source order as ranges (offset, length), adjacent ranges merged:
/// the rebuild reads and concatenates them.
pub fn leaves(ir: &Ir) -> Res<Vec<(u64, u64)>> {
    let (v, _) = sweep(ir)?;
    let mut out: Vec<(u64, u64)> = Vec::new();
    for (s, e, id, _) in v {
        if ir.kind[id as usize] == ALIAS {
            continue;
        }
        match out.last_mut() {
            Some(l) if l.0 + l.1 == s => l.1 += e - s,
            _ => out.push((s, e - s)),
        }
    }
    Ok(out)
}

/// The rebuild of a store IR's held objects (conventions §8.9
/// <https://github.com/d-v-b/refkv/blob/main/spec/conventions.md#89-stores>): for each held object
/// (one with children), in id order, its leaves in order of their start as ranges (offset,
/// length) of its own source, adjacent ranges merged. Referenced objects are not rebuilt.
pub fn held_leaves(ir: &Ir) -> Res<Vec<(u32, Vec<(u64, u64)>)>> {
    let (v, src) = sweep(ir)?;
    let mut out: Vec<(u32, Vec<(u64, u64)>)> = Vec::new();
    for (s, e, id, _) in v {
        let k = ir.kind[id as usize];
        if k == ALIAS || k == OBJECT {
            continue;
        }
        for (o, a, n) in pieces(ir, &src, s, e - s)? {
            if out.last().is_none_or(|l| l.0 != o) {
                out.push((o, Vec::new()));
            }
            let r = &mut out.last_mut().unwrap().1;
            match r.last_mut() {
                Some(l) if l.0 + l.1 == a => l.1 += n,
                _ => r.push((a, n)),
            }
        }
    }
    Ok(out)
}
