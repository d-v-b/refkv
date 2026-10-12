use vzip_ir::{ims::Ims, parse_bytes, ir::KINDS};
use std::collections::BTreeMap;
fn main() {
    let mut seen: BTreeMap<String, (String, String)> = BTreeMap::new();
    for p in std::env::args().skip(1) {
        let data = std::fs::read(&p).unwrap();
        if let Ok((ir, _)) = parse_bytes::<Ims>(&data) {
            for i in 0..ir.len() as u32 {
                let path = ir.path(i);
                let norm: String = path.split('/').map(|s| if s.chars().all(|c| c.is_ascii_digit()) && !s.is_empty() { "<n>".to_string() } else { s.to_string() }).collect::<Vec<_>>().join("/");
                let ty = ir.types.get(ir.ty[i as usize]).to_string();
                let ty = if ty.starts_with("bytes[") { "bytes[n]".into() } else { ty };
                seen.entry(norm).or_insert((KINDS[ir.kind[i as usize] as usize].to_string(), ty));
            }
        }
    }
    for (k, (kind, ty)) in seen { println!("{k}\t{kind}\t{}", &ty[..ty.len().min(90)]); }
}
