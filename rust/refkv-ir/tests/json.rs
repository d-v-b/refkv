//! How a store format holds a document in its IR (src/json.rs `hold`, which the N5 and Zarr v2
//! formats share): a value per top-level member, named apart from the gaps and from the name of a
//! document held whole; a document of more than `MAX_MEMBERS` members is one value `json`.

use vzip_ir::ir::{Ir, VALUE, GAP};
use vzip_ir::json::{hold, parse, MAX_MEMBERS};

/// The children of the one object of a store IR holding `doc`: (name, name index, kind).
fn held(doc: &str) -> Vec<(String, u64, u8)> {
    let mut ir = Ir::new_store();
    let objects = ir.listing(&[("d".to_string(), doc.len() as u64)]).unwrap();
    hold(&mut ir, objects[0], doc.len() as u64, &parse(doc.as_bytes()).unwrap().members).unwrap();
    vzip_ir::check::finish(&mut ir).unwrap();
    (0..ir.len() as u32).filter(|&i| ir.parent[i as usize] == objects[0])
        .map(|i| (ir.names.get(ir.name[i as usize]).to_string(), ir.nidx[i as usize], ir.kind[i as usize])).collect()
}

#[test]
fn documents_are_held_a_value_per_member_named_apart() {
    let none = u64::MAX;
    let kids = held(r#"{"gaps": 1, "json": 2, "a": 3, "a": 4}"#);
    let values: Vec<(String, u64)> = kids.iter().filter(|k| k.2 == VALUE).map(|k| (k.0.clone(), k.1)).collect();
    assert_eq!(values, [("gaps~".into(), 0), ("json~".into(), 0), ("a".into(), none), ("a~".into(), 1)]);
    assert!(kids.iter().any(|k| k.2 == GAP && k.0 == "gaps/"));
    assert!(held("{}").iter().all(|k| k.2 == GAP));
    let many = format!("{{{}}}", (0..=MAX_MEMBERS).map(|i| format!("\"k{i}\":{i}")).collect::<Vec<_>>().join(","));
    assert_eq!(held(&many), [("json".to_string(), none, VALUE)]);
}
