"""What the js/test/<format>/verify.py scripts share: the reader policy their archives are opened
under, and the loop that zarrifies each fixture with the browser code under Node and checks it.

A script imports it once `js/test` is on `sys.path` (as it is for `ir_mirror`):

    sys.path.insert(0, str(HERE.parent))
    from verify_common import LOOPBACK_SOURCES, run, serve  # noqa: E402

and its `main` is the inputs, the check of an accepted one, and how to read them:

    paths = sorted(fixtures.glob("*.mrc"))
    return run(paths, lambda path, out, _: problems_of(path, out), url=serve(paths), width=40)
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "conformance" / "vzip"))
from http_server import Server  # noqa: E402

from refkv.policy import Policy  # noqa: E402

# The sources are served on 127.0.0.1, which spec §6.7
# <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy> rule 3 refuses by
# default.
LOOPBACK_SOURCES = Policy(allow_private_hosts=True)
# The browser code under Node (js/conformance/zarrify.ts), allowed to read 127.0.0.1.
WEB = ["node", str(ROOT / "js" / "conformance" / "zarrify.ts"), "--allow-private-hosts"]

# What a check gives: the problems of an accepted input (none: it passed), or the problems and a
# note to print beside the verdict.
Check = Callable[[Path, Path, subprocess.CompletedProcess], "list | tuple[list, str]"]


def serve(paths: Iterable[Path]) -> Callable[[Path], str]:
    """The URL of each file of `paths` on a local HTTP server, one server per directory."""
    servers: dict[Path, Server] = {}

    def url(path: Path) -> str:
        server = servers.get(path.parent)
        if server is None:
            server = servers[path.parent] = Server(path.parent)
        return server.base + path.name

    for path in paths:
        url(path)
    return url


def rejected_by_name(name: str) -> bool:
    """Whether a fixture is one the profile must reject: its name says so (`<format>_reject_*`,
    conformance/zarrify/HARNESS.md)."""
    return "reject" in name


def run(inputs: Iterable[Path], check: Check, *, url: Callable[[Path], str],
        rejected: Callable[[str], bool] = rejected_by_name, command: list[str] | None = None,
        width: int = 44, failure_tail: int = 300) -> int:
    """Zarrifies each input (a file, or a store's directory) at `url(input)` with `command`
    (the browser code under Node, by default) into an archive in a temporary directory of its
    own; an input `rejected` by its name must be rejected (exit status 3, no archive), any other
    accepted, and then `check(input, archive, process)` must give no problems. Prints a line
    per input and the failure count; returns the exit status (1 when any failed)."""
    failures = 0
    for item in inputs:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / f"{item.stem if item.is_file() else item.name}.vzip"
            p = subprocess.run([*(command or WEB), url(item), str(out)], capture_output=True, text=True)
            label = f"{item.name:{width}s}"
            if rejected(item.name):
                ok = p.returncode == 3 and not out.exists()
                failures += not ok
                print(f"{label} {'rejected: ' + p.stderr.strip()[-90:] if ok else 'NOT REJECTED ' + p.stderr[-200:]}")
                continue
            if p.returncode:
                failures += 1
                print(f"{label} FAILED: {p.stderr.strip()[-failure_tail:]}")
                continue
            result = check(item, out, p)
            problems, note = result if isinstance(result, tuple) else (result, "")
            failures += bool(problems)
            print(f"{label} {'ok' if not problems else problems}{' ' + note if note else ''}")
    print(f"\n{failures} failures")
    return 1 if failures else 0
