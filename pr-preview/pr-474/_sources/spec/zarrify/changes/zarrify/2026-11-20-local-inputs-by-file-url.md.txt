Changed 2026-11-20, with no change to any output for an http(s) or registered input: a local file
or directory may be given by its `file:` URL, read as the local file or directory it names whether
or not a store is registered for it ([§1.2](../../../zarrify.md#12-input)), and a local directory
may be named by its own `file:` URL, which names no other store
([§1.4](../../../zarrify.md#14-store-inputs)). A plugin layout may read a store input's base, of
any store profile its formats name ([spec/layout.md](../../../layout.md)).
