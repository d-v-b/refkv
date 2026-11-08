Changed 2026-11-08: an `Integer` is a JSON integer, exact whatever its magnitude, where one past 2^53 − 1
was the string of its decimal digits; a consumer that needs such an integer exactly parses it as a
big integer (in JavaScript, `JSON.parse` with a reviver that reads `context.source`), and a
convention that reads an integer from decimal text reads one of more than 4300 digits as text
([§6](../../../conventions.md#6-source-metadata-as-json)). The view of the IR mirror shows an
integer so too, and has no `{"$vz": "int"}` form; an integer shown so is shorter, so a document
of the view may hold more of its children within its budget
([§8.7](../../../conventions.md#87-the-view)). Every output with an integer past 2^53 − 1 in its
source metadata or its mirror's view changes.
