//! The checker of a source model schema made of roles (`schema/tiff.json`,
//! `schema/czi.json`). A role is a kind of thing a parser meets (an IFD read, a
//! directory entry, a level of an image): the schema names what the parser gives
//! for it (`given`), the quantities derived from that (`derive`), and the
//! constraints it must meet, each with the message a file that fails is rejected
//! with. The parser's code reads the binary layer and decides which role each
//! thing plays, in the order the format's profile does; the schema decides
//! whether it is valid. The schema's `limits` and `tables` are names every
//! expression can use.

use crate::expr::{self, E, Env, Layered, Scope, V};

/// What a parser gives for a role, and what the role derives: a few names, looked up
/// in order (cheaper than a map for the handful a role has).
#[derive(Default, Debug, Clone)]
pub struct Vars<'a>(pub Vec<(&'a str, V)>);

impl<'a> Vars<'a> {
    pub fn insert(&mut self, k: &'a str, v: V) {
        match self.0.iter_mut().find(|x| x.0 == k) {
            Some(x) => x.1 = v,
            None => self.0.push((k, v)),
        }
    }
}

impl Scope for Vars<'_> {
    fn lookup(&self, name: &str) -> V {
        self.0.iter().find(|x| x.0 == name).map(|x| x.1.clone()).unwrap_or(V::Absent)
    }
}
use serde_json::Value as J;
use std::borrow::BorrowMut;
use std::collections::HashMap;
use std::sync::OnceLock;

pub type Res<T> = Result<T, String>;

// The source models, schema/<name>.json, by name (build.rs)
include!(concat!(env!("OUT_DIR"), "/schemas.rs"));

struct Role {
    derive: Vec<(String, E)>,
    constraints: Vec<(E, String)>,
}

pub struct Rules {
    pub json: J,
    consts: Env,
    roles: HashMap<String, Role>,
}

/// The rules of the source model schema/<name>.json, parsed once.
pub fn of(name: &str) -> &'static Rules {
    static S: [OnceLock<Rules>; SCHEMAS.len()] = [const { OnceLock::new() }; SCHEMAS.len()];
    let i = SCHEMAS.iter().position(|(n, _)| *n == name).unwrap_or_else(|| panic!("no source model schema/{name}.json"));
    S[i].get_or_init(|| Rules::new(SCHEMAS[i].1).unwrap_or_else(|e| panic!("the {name} schema is not valid: {e}")))
}

/// A value as a message shows it: integers without a fraction, text as is.
pub fn show(v: &V) -> String {
    match v {
        V::Num(x) if x.fract() == 0.0 && x.abs() < 1e18 => format!("{}", *x as i64),
        V::Num(x) => format!("{x}"),
        V::Str(s) => s.clone(),
        V::Bool(b) => b.to_string(),
        V::Absent => "none".into(),
        V::List(v) => format!("[{}]", v.iter().map(show).collect::<Vec<_>>().join(", ")),
        other => other.to_json().to_string(),
    }
}

/// `{name}` in a message, replaced by the value of `name`.
fn format<S: Scope>(msg: &str, scope: &S) -> String {
    let mut out = String::new();
    let mut rest = msg;
    while let Some(a) = rest.find('{') {
        let Some(b) = rest[a..].find('}') else { break };
        out.push_str(&rest[..a]);
        let name = &rest[a + 1..a + b];
        let path: Vec<&str> = name.split('.').collect();
        let mut v = scope.lookup(path[0]);
        for k in &path[1..] {
            v = v.get(k);
        }
        out.push_str(&show(&v));
        rest = &rest[a + b + 1..];
    }
    out.push_str(rest);
    out
}

impl Rules {
    pub fn new(text: &str) -> Res<Rules> {
        let json: J = serde_json::from_str(text).map_err(|e| e.to_string())?;
        let mut consts = Env::new();
        for section in ["limits", "tables"] {
            if let Some(J::Object(m)) = json.get(section) {
                for (k, v) in m {
                    // a table's `about` describes it and names no entry (`Rules::entry`)
                    let v = match v {
                        J::Object(t) if section == "tables" => J::Object(t.iter().filter(|e| e.0 != "about").map(|(a, b)| (a.clone(), b.clone())).collect()),
                        _ => v.clone(),
                    };
                    consts.insert(k.clone(), V::from_json(&v));
                }
            }
        }
        let mut roles = HashMap::new();
        if let Some(J::Object(m)) = json.get("roles") {
            for (name, r) in m {
                let mut derive = Vec::new();
                if let Some(J::Object(d)) = r.get("derive") {
                    for (k, v) in d {
                        if consts.contains_key(k) {
                            return Err(format!("{name}: the derived {k} shadows a limit or table"));
                        }
                        let s = v.as_str().ok_or(format!("{name}: derive {k} is not text"))?;
                        derive.push((k.clone(), expr::parse(s).map_err(|e| format!("{name}.{k}: {e}"))?));
                    }
                }
                // a given or derived name absent from an env reads the limit or table of its name
                // (`Layered`), so neither may share a name with one
                if let Some(J::Object(g)) = r.get("given") {
                    if let Some(k) = g.keys().find(|k| consts.contains_key(*k)) {
                        return Err(format!("{name}: the given {k} shadows a limit or table"));
                    }
                }
                let mut constraints = Vec::new();
                for c in r.get("constraints").and_then(|c| c.as_array()).into_iter().flatten() {
                    let req = c["require"].as_str().ok_or(format!("{name}: a constraint lacks require"))?;
                    let msg = c["message"].as_str().ok_or(format!("{name}: a constraint lacks message"))?;
                    constraints.push((expr::parse(req).map_err(|e| format!("{name}: {e}"))?, msg.to_string()));
                }
                roles.insert(name.clone(), Role { derive, constraints });
            }
        }
        Ok(Rules { json, consts, roles })
    }

    pub fn limit(&self, name: &str) -> f64 {
        self.json["limits"][name].as_f64().unwrap_or(f64::NAN)
    }

    pub fn table(&self, name: &str) -> &J {
        &self.json["tables"][name]
    }

    /// The entry `key` of the table `name`: none for `about`, which describes the table and names
    /// no entry (and which the schema's expressions do not see either).
    pub fn entry(&self, name: &str, key: &str) -> Option<&J> {
        self.table(name).get(key).filter(|_| key != "about")
    }

    pub fn has_role(&self, role: &str) -> bool {
        self.roles.contains_key(role)
    }

    /// Derives the role's quantities into `env`, then checks its constraints in
    /// order: the first that fails rejects, with its message.
    /// The environment is given or lent (a parser's own, or one it reads the derived
    /// quantities of afterwards).
    pub fn check<'a>(&'a self, role: &str, mut env: impl BorrowMut<Vars<'a>>) -> Res<()> {
        let env = env.borrow_mut();
        let r = self.roles.get(role).ok_or(format!("internal: no role {role}"))?;
        for (k, e) in &r.derive {
            let v = expr::eval(e, &Layered(&*env, &self.consts));
            env.insert(k.as_str(), v);
        }
        for (e, msg) in &r.constraints {
            let scope = Layered(&*env, &self.consts);
            if !expr::eval(e, &scope).truthy() {
                return Err(format(msg, &scope));
            }
        }
        Ok(())
    }
}

/// What a parser gives for a role, from (name, value) pairs.
pub fn env<const N: usize>(pairs: [(&'static str, V); N]) -> Vars<'static> {
    Vars(pairs.into())
}

pub fn num(x: impl Into<f64>) -> V {
    V::Num(x.into())
}

pub fn int(x: i128) -> V {
    V::Num(x as f64)
}

pub fn flag(b: bool) -> V {
    V::Bool(b)
}

pub fn text(s: &str) -> V {
    V::Str(s.to_string())
}
