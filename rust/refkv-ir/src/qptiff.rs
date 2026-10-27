//! The QPTIFF profile (spec/zarrify/qptiff.md): an Akoya QPTIFF is a TIFF, which the TIFF
//! parser reads (tiff.rs) and finds to be QPTIFF; its projection is project/qptiff.rs.

/// The QPTIFF profile's file run: a TIFF the TIFF parser found to be an Akoya QPTIFF
/// (spec/zarrify/qptiff.md §7
/// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify/qptiff.md#7-qptiff-profile>), as
/// NDPI. Its files are not sniffed: they are TIFFs.
pub struct Profile;

impl crate::run::FileProfile for Profile {
    fn parser(&self, size: u64) -> crate::run::AnyParser {
        crate::run::AnyParser::of::<crate::tiff::Tiff>(size)
    }
    fn project(&self, ir: &crate::ir::Ir, facts: &serde_json::Value, url: &str, copied: &crate::project::Copied) -> Result<crate::out::Out, String> {
        crate::project::qptiff::project(ir, facts, url, copied)
    }
    fn reads(&self, ir: &crate::ir::Ir, facts: &serde_json::Value) -> Result<Vec<(u64, u64)>, String> {
        crate::project::qptiff::reads(ir, facts)
    }
}
