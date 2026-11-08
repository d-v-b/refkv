Changed 2026-11-08: an integer past 2^53 − 1 in magnitude, of a lite variant or an XML variant, is a JSON
integer, exact, where it was the tag `{"int": D}`, which is no more; so an object with one record
named `int` is an object, where it was pairs; and an XML variant's integer outside its runtype's
width is the value as a string, where it was a number or the tag
([§5.1](../../nd2.md#51-decoded-chunks)). The output of a file with such
an integer or record changes.
