//! Why the core gives no result: one error type for the parsers, the rules, the run and store
//! pipeline and the seal (d-v-b/refkv#459, step 2a). A host maps it by its variant, never by its
//! message: a rejection of the input (`Rejected`, `Budget`, `OutOfScope`) is the host's rejection
//! (Python's `refkv.zarrify.Rejected`, the CLI's status 3, the JS host's `ImageError`), and a
//! failure of refkv's on an input it accepted (`Failed`, `Internal`) is the host's failure
//! (Python's `refkv.zarrify.Failed`, the CLI's status 1, a plain `Error` in JS).
//!
//! A rejection's message is an output (the manifests, `conformance/zarrify/rejects/*.json`): each
//! variant holds its whole message, as hosts print it, so that a message given context
//! (`Error::context`) keeps both its words and its variant.

use std::fmt;

/// Why the core gives no result.
#[derive(Clone, Debug, PartialEq)]
pub enum Error {
    /// The input breaks a rule of its profile: the message is the rejection's.
    Rejected(String),
    /// The input passes one of its profile's limits (a budget of elements, reads, depth ...): a
    /// rejection, its message `budget: ...`.
    Budget(String),
    /// The input names a file outside its scope (spec/zarrify.md §1.9
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#19-sources-beside-the-input>,
    /// **Scope**): a rejection, its message `out of scope: the input names the file "<key>"`.
    OutOfScope { key: String },
    /// refkv cannot write an output of an input it accepted, through no bug of its own: a payload
    /// over the limit only with its checksums (spec/zarrify.md §1.2
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#12-input>).
    Failed(String),
    /// A bug of refkv's, never the input's fault: a broken invariant, or a host's misuse of the
    /// core. Its message is mostly `internal: ...`.
    Internal(String),
}

/// A result of the core.
pub type Res<T> = Result<T, Error>;

impl Error {
    /// A rejection of the input, `message` its words.
    pub fn rejected(message: impl Into<String>) -> Error {
        Error::Rejected(message.into())
    }
    /// A limit of the profile passed: `budget: <what>`.
    pub fn budget(what: impl fmt::Display) -> Error {
        Error::Budget(format!("budget: {what}"))
    }
    /// A bug of refkv's: `internal: <what>`.
    pub fn internal(what: impl fmt::Display) -> Error {
        Error::Internal(format!("internal: {what}"))
    }
    /// Whether the error rejects the input, rather than being a failure of refkv's.
    pub fn is_rejection(&self) -> bool {
        matches!(self, Error::Rejected(_) | Error::Budget(_) | Error::OutOfScope { .. })
    }
    /// The same error, its message rewritten by `f` (a context put before it, such as the file it
    /// is about): its variant is kept. An `OutOfScope`, whose message names its file already, is
    /// kept as it is.
    pub fn context(self, f: impl FnOnce(&str) -> String) -> Error {
        match self {
            Error::Rejected(m) => Error::Rejected(f(&m)),
            Error::Budget(m) => Error::Budget(f(&m)),
            Error::Failed(m) => Error::Failed(f(&m)),
            Error::Internal(m) => Error::Internal(f(&m)),
            e @ Error::OutOfScope { .. } => e,
        }
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Rejected(m) | Error::Budget(m) | Error::Failed(m) | Error::Internal(m) => f.write_str(m),
            Error::OutOfScope { key } => write!(f, "out of scope: the input names the file \"{key}\""),
        }
    }
}

impl std::error::Error for Error {}

impl From<Error> for String {
    fn from(e: Error) -> String {
        e.to_string()
    }
}

/// For the migration of d-v-b/refkv#459's step 2a only, removed once every module builds an
/// `Error` itself: a message of a module that still returns `Result<_, String>`, by the prefix that
/// module wrote it with (`internal:`, `budget:`), else a rejection, as the hosts read every message
/// before.
impl From<String> for Error {
    fn from(m: String) -> Error {
        if m.starts_with("internal:") {
            Error::Internal(m)
        } else if m.starts_with("budget:") {
            Error::Budget(m)
        } else {
            Error::Rejected(m)
        }
    }
}

/// `From<String>`, for a module's `&str` message during the migration.
impl From<&str> for Error {
    fn from(m: &str) -> Error {
        Error::from(m.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The migration's conversion reads a module's message by the prefix it was written with, at
    /// its start only: a message given context before its prefix is read as a rejection, as every
    /// host read every message before.
    #[test]
    fn a_string_is_read_by_its_prefix() {
        let cases = [
            ("internal: no IR", Error::Internal("internal: no IR".into())),
            ("budget: more than 8 reads", Error::Budget("budget: more than 8 reads".into())),
            ("a short file", Error::Rejected("a short file".into())),
            ("k: internal: x", Error::Rejected("k: internal: x".into())),
        ];
        for (m, want) in cases {
            assert_eq!(Error::from(m.to_string()), want, "{m}");
            assert_eq!(Error::from(m), want, "{m}");
        }
    }

    #[test]
    fn a_message_keeps_its_words_and_its_variant_through_context() {
        let cases = [
            (Error::rejected("a short file"), "a short file", true),
            (Error::budget("objects more than 64 links deep"), "budget: objects more than 64 links deep", true),
            (Error::OutOfScope { key: "a/b.raw".into() }, "out of scope: the input names the file \"a/b.raw\"", true),
            (Error::Failed("a payload over the limit".into()), "a payload over the limit", false),
            (Error::internal("no IR"), "internal: no IR", false),
        ];
        for (e, message, rejection) in cases {
            assert_eq!((e.to_string(), e.is_rejection()), (message.to_string(), rejection));
            let put = e.clone().context(|m| format!("its MP4 file: {m}"));
            assert_eq!(std::mem::discriminant(&put), std::mem::discriminant(&e));
            let want = if matches!(e, Error::OutOfScope { .. }) { message.to_string() } else { format!("its MP4 file: {message}") };
            assert_eq!(put.to_string(), want);
        }
    }
}
