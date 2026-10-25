# Scratch: summarize an output JSON.
import base64
import json
import sys
from pathlib import Path

o = json.loads(Path(sys.argv[1]).read_text())
refs = [k for k, v in o["entries"].items() if "ranges" in v]
for k, v in sorted(o["entries"].items()):
    if "json" in v:
        j = v["json"]
        if j.get("node_type") == "array":
            print(k, "shape", j["shape"], "chunks", j["chunk_grid"]["configuration"]["chunk_shape"], j["data_type"],
                  json.dumps(j["codecs"]), j["dimension_names"])
        else:
            print(k, json.dumps(j["attributes"]))
    elif "base64" in v:
        print(k, "bytes", len(base64.b64decode(v["base64"])))
print(len(refs), "refs")
for k in sorted(refs)[:int(sys.argv[2]) if len(sys.argv) > 2 else 6]:
    print(" ", k, o["entries"][k]["ranges"][:3], len(o["entries"][k]["ranges"]))
