"""vzip's Rust core (rust/vzip-ir), built into the vzip distribution: the compiled module
is vzip_ir.vzip_ir, and this package re-exports it, as maturin's own wrapper does."""

from .vzip_ir import *  # noqa: F403

if hasattr(vzip_ir, "__all__"):  # noqa: F405
    __all__ = vzip_ir.__all__  # noqa: F405
