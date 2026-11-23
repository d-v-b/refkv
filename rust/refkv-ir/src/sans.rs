//! The skeleton the sans-IO file parsers share around their own steps: the reads a round asks
//! for, counted, and the end of a parse.

use crate::error::Error;

use crate::check;
use crate::ir::Ir;
use crate::step::Step;
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

    /// The counts into what a parse returns, beside its description (`requests`, `requested`,
    /// `rounds`; `run::parse_counts` takes them apart).
    pub fn put(&self, description: &mut J) {
        description["requests"] = json!(self.requests);
        description["requested"] = json!(self.requested);
        description["rounds"] = json!(self.rounds);
    }
}

/// The IR and description of a parse that is `done`: its values sorted and its invariants checked.
pub fn finish(done: bool, mut ir: Ir, description: J) -> crate::error::Res<(Ir, J)> {
    if !done {
        return Err(Error::internal("the parse is not done"));
    }
    ir.sort_values();
    check::finish(&mut ir)?;
    Ok((ir, description))
}
