//! The skeleton the sans-IO file parsers share around their own steps: the reads a round asks
//! for, counted, and the end of a parse.

use crate::check;
use crate::ir::Ir;
use crate::nd2::Step;
use serde_json::{Value as J, json};

/// What a parser asked for: its requests, their bytes, and its rounds.
#[derive(Default)]
pub struct Asked {
    pub requests: u64,
    pub requested: u64,
    pub rounds: u64,
}

impl Asked {
    /// A round's reads: the nonempty ranges, sorted and each once, counted.
    pub fn ask(&mut self, mut r: Vec<(u64, u64)>) -> Step {
        r.retain(|x| x.1 > 0);
        r.sort_unstable();
        r.dedup();
        self.requests += r.len() as u64;
        self.requested += r.iter().map(|x| x.1).sum::<u64>();
        self.rounds += 1;
        Step::Read(r)
    }

    /// The counts into a parse's facts (`requests`, `requested`, `rounds`).
    pub fn put(&self, facts: &mut J) {
        facts["requests"] = json!(self.requests);
        facts["requested"] = json!(self.requested);
        facts["rounds"] = json!(self.rounds);
    }
}

/// The IR and facts of a parse that is `done`: its values sorted and its invariants checked.
pub fn finish(done: bool, mut ir: Ir, facts: J) -> Result<(Ir, J), String> {
    if !done {
        return Err("internal: the parse is not done".into());
    }
    ir.sort_values();
    check::finish(&mut ir)?;
    Ok((ir, facts))
}
