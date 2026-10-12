"""Hostile N5 stores (in the style of the round-3 probes, python/tests/ir/gen_probes.py): deep and
wide candidates, a document of 800,000 members or of one name repeated, a grid at the integer
limit, a multiscale group whose levels repeat, many empty objects, a dataset full of nested
documents, deep JSON, and member names that break the IR's naming rules. Each must be refused or
virtualized as the frozen reference does it, never hang or blow up.
Usage: uv run python python/tests/ir/gen_n5_probes.py <out dir> [--small]
(--small writes the tests' sizes: each store under 2 MB and 3000 objects.)"""

import json
import sys
from pathlib import Path

out = Path(sys.argv[1])
small = "--small" in sys.argv
RAW = {"dimensions": [4], "blockSize": [2], "dataType": "uint8", "compression": {"type": "raw"}}


def store(name: str, files: dict[str, bytes | str | dict]) -> None:
    root = out / name
    for k, v in files.items():
        p = root / k
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(v if isinstance(v, bytes) else (v if isinstance(v, str) else json.dumps(v)).encode())


# candidates nested as deep as a key allows: one round per level
depth = 120 if small else 300  # a local path is at most 1024 bytes on macOS
store("n5_probe_deep", {"/".join(["a"] * k + ["attributes.json"]): {"n5": "4.0.0"} for k in range(depth)})
# many candidates at one level: one round of many reads
wide = 1500 if small else 20000
store("n5_probe_wide", {"attributes.json": {}, **{f"g{k}/attributes.json": {"k": k} for k in range(wide)}})
# a document of 800,000 members (more than 4096: one value json in the IR)
n = 100_000 if small else 800_000  # under the 16 MiB a document may have
store("n5_probe_members", {"attributes.json": "{" + ",".join(f'"m{k}":{k}' for k in range(n)) + "}"})
# one name repeated, more than 4096 times (one value json in the IR); the hierarchy keeps the last
store("n5_probe_repeated", {"attributes.json": "{" + ",".join(f'"m":{k}' for k in range(n // 4)) + "}"})
# 4096 members, the most a document has one value per member for: m, then m~1 to m~4095
store("n5_probe_repeated_4096", {"attributes.json": "{" + ",".join(f'"m":{k}' for k in range(4096)) + "}"})
# a grid at the integer limit, with chunk keys at its edges and past them
big = 2**53 - 1
store("n5_probe_grid", {
    "attributes.json": {"dimensions": [big, big, 3], "blockSize": [1, 1, 1], "dataType": "uint16",
                        "compression": {"type": "raw"}},
    f"{big - 1}/0/2": b"x", f"{big}/0/0": b"y", "00/0/0": b"z", "0/0/3": b"w", "1e3/0/0": b"v",
})
# a multiscale group whose levels all name one dataset, many times
levels = 20_000 if small else 200_000
store("n5_probe_levels", {
    "attributes.json": {"multiscales": [{"datasets": [{"path": "s0"}] * levels}]},
    "s0/attributes.json": {**RAW, "dimensions": [4, 4], "blockSize": [2, 2],
                           "transform": {"axes": ["y", "x"], "scale": [1.0, 2.0]}},
    "v/attributes.json": {"scales": [[1, 1]] * levels},
    "v/s0/attributes.json": {**RAW, "dimensions": [4, 4], "blockSize": [2, 2]},
})
# many empty objects, which the mirror lists
empties = 2500 if small else 200_000
store("n5_probe_empty", {"attributes.json": RAW, **{f"x/{k}": b"" for k in range(empties)}})
# a dataset whose directory holds many documents of nested datasets, none of which is read
nested = 2000 if small else 50_000
store("n5_probe_nested", {"attributes.json": RAW, **{f"n{k}/attributes.json": b"not JSON" for k in range(nested)}})
# JSON 256 deep in a member, and 257 deep (rejected)
store("n5_probe_deep_json", {"attributes.json": '{"d":' + "[" * 255 + "]" * 255 + "}"})
store("n5_probe_too_deep_json", {"attributes.json": '{"d":' + "[" * 256 + "]" * 256 + "}"})
# member names the IR must rename: empty, '/', '~', 'gaps', unpaired surrogates, repeats
store("n5_probe_names", {"attributes.json": '{"": 1, "a/b": 2, "~": 3, "gaps": 4, "gaps/0": 5, "\\ud800": "\\udc00x",'
                                            ' "\\udbff": 6, "a/b": 7, "$vz": 8, "%": 9}'})
