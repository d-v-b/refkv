"""Virtualization (metadata reads) of the NDPI slides, today (the frozen reference) against the IR,
over design/experiments/ir_round2/rangeserver.py serving the sparse copies: time, peak RSS,
requests, bytes, archive size. Usage: virt_bench.py <sparse dir> <out.json> <profile,...>"""
import ast, json, re, subprocess, sys, time, urllib.request
from pathlib import Path
W = Path.cwd()
PROFILES = {"local": (18331, 0, 0, None), "h1-20ms": (18332, 20, 0, 6.25), "openslide": (18333, 500, 0, 0.66)}

def stats(port):
    return ast.literal_eval(urllib.request.urlopen(f"http://127.0.0.1:{port}/stats").read().decode())

def run(cmd, out, port):
    out.unlink(missing_ok=True)
    s0 = stats(port); t = time.time()
    p = subprocess.run(["/usr/bin/time", "-l"] + cmd + [str(out)], capture_output=True, text=True, timeout=7200, cwd=W)
    dt = time.time() - t; s1 = stats(port)
    m = re.search(r"(\d+)\s+maximum resident set size", p.stderr)
    return {"status": p.returncode, "s": round(dt, 2), "rss": int(m[1]) >> 20 if m else -1,
            "requests": s1["requests"] - s0["requests"], "bytes": s1["bytes"] - s0["bytes"],
            "size": out.stat().st_size if out.exists() else 0}

sparse, res_path, profiles = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3].split(",")
res = json.loads(res_path.read_text()) if res_path.exists() else {}
servers = []
try:
    for pr in profiles:
        port, lat, mp, rate = PROFILES[pr]
        servers.append(subprocess.Popen([sys.executable, str(W / "design/experiments/ir_round2/rangeserver.py"), str(sparse),
                                         str(port), str(lat), str(mp)] + ([str(rate)] if rate else []),
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    time.sleep(1.5)
    for name in ["CMU-1.ndpi", "Hamamatsu-2.ndpi", "Hamamatsu-1.ndpi"]:
        for pr in profiles:
            port = PROFILES[pr][0]
            url = f"http://127.0.0.1:{port}/{name}"
            r = {"today": run(["uv", "run", "python", "conformance/virtualize/reference/cli.py", "--allow-private-hosts", url],
                              sparse.parent / "t.vzip", port)}
            for c in (6, 8):
                r[f"ir{c}"] = run(["uv", "run", "python", "-m", "vzip.virtualize", "--allow-private-hosts", "--connections", str(c), url],
                                  sparse.parent / "i.vzip", port)
            res[f"{name}|{pr}"] = r
            res_path.write_text(json.dumps(res, indent=1))
            print(name, pr, json.dumps(r), flush=True)
finally:
    for s in servers:
        s.terminate(); s.wait()
