Changed 2026-11-22, with no change to any output: a form's `geometry` and `codec` are in the
source's own terms, never an output's data type, codec or other metadata
([§8.5](../../../conventions.md#85-forms-and-recipes)). A test reads every fixture and checks its
IR's forms and its description (`python/tests/test_ir_has_no_zarr.py`); the profiles whose forms
still give a Zarr data type or codec are its expected failures until each is changed.
