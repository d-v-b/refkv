//! phkv stores, refkv's perfect-hash binding (spec/bindings/phkv.md
//! <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md>): the container's bytes
//! ([`format`]), its perfect hash ([`ptrhash`]), the [`Writer`], and the [`Store`] reader.

pub mod format;
pub mod ptrhash;
mod reader;
mod writer;

pub use format::{Codec, Prop};
pub use reader::{DEFAULT_RECORD_READ, DEFAULT_SLOT_BLOCK, DEFAULT_TAIL, Class, EntryInfo, Kind, Limits, NoUrls, ReadAt, ReadError, Request, Resolver, Store};
pub use writer::{Options, Writer};

/// A writer's failure: what it was asked to write breaks a writer requirement (spec/refkv.md §7
/// <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#7-writing>, spec/bindings/phkv.md §22.1
/// <https://github.com/d-v-b/refkv/blob/main/spec/bindings/phkv.md#221-requirements>), or the
/// output could not be written.
#[derive(Debug)]
pub enum Error {
    Invalid(String),
    Io(std::io::Error),
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Error::Invalid(m) => f.write_str(m),
            Error::Io(e) => write!(f, "{e}"),
        }
    }
}

impl std::error::Error for Error {}

impl From<std::io::Error> for Error {
    fn from(e: std::io::Error) -> Error {
        Error::Io(e)
    }
}

impl From<refkv_vzip::Error> for Error {
    fn from(e: refkv_vzip::Error) -> Error {
        match e {
            refkv_vzip::Error::Invalid(m) => Error::Invalid(m),
            refkv_vzip::Error::Io(e) => Error::Io(e),
        }
    }
}
