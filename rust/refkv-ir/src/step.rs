//! What a file parser asks of its host next (`Parser::step`), shared by every file profile's
//! parser.

/// A file parser's next step.
pub enum Step {
    /// The ranges (offset, length) to read next.
    Read(Vec<(u64, u64)>),
    /// The file names siblings (spec/zarrify.md §1.9
    /// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#19-sources-beside-the-input>), so
    /// that it is a file read as a store: the host starts over with a store run of its format.
    Beside,
    /// The parse is done: the host calls `finish`.
    Done,
}
