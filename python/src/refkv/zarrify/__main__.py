import json
import sys
from pathlib import Path

from refkv.errors import ResolutionError
from refkv.policy import Policy
from refkv.zarrify import Rejected, zarrify
from refkv.zarrify.store import WORKERS

# the store kinds, in the words of `vzip convert`'s usage (rust/refkv-cli/src/main.rs)
STORES = "an N5, Zarr v2 or OME-Zarr 0.4 store, or a Sentinel-2 .SAFE directory"
USAGE = ("usage: refkv zarrify <url or path> <out.vzip> [--url <source url>] [--checksums]"
         " [--allow-private-hosts] [--connections N] [--byte-cost SECONDS_PER_MB] [--workers N]\n"
         f"(a URL ending in / or a directory is {STORES}; a directory needs --url;\n"
         f"a store's documents are read by up to N requests at a time, default {WORKERS})")


def main(argv: list[str]) -> int:
    """Zarrifies the input `argv` names into the archive it names; the exit status: 0, 1 for a
    failure (such as a source the reader policy refuses), 2 for a usage error, 3 for an input
    rejected. `-h` or `--help` prints the usage to stdout."""
    args = list(argv)
    if "-h" in args or "--help" in args:
        print(USAGE)
        return 0
    url = None
    # spec/refkv.md §4.2 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#42-range>: reads every referenced byte
    checksums = "--checksums" in args
    if checksums:
        args.remove("--checksums")
    # spec/refkv.md §6.7
    # <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy> rule 3: an input on
    # a loopback or private host needs this (UNSAFE)
    private = "--allow-private-hosts" in args
    if private:
        args.remove("--allow-private-hosts")
    connections = byte_cost = None
    if "--connections" in args:  # requests at a time to a remote file (default 8)
        i = args.index("--connections")
        connections = int(args[i + 1])
        del args[i:i + 2]
    if "--byte-cost" in args:  # seconds per MB the read planner charges for bytes fetched (default 0.1)
        i = args.index("--byte-cost")
        byte_cost = float(args[i + 1]) * 1e-6
        del args[i:i + 2]
    if "--url" in args:
        i = args.index("--url")
        url = args[i + 1]
        del args[i : i + 2]
    workers = WORKERS
    if "--workers" in args:
        i = args.index("--workers")
        workers = int(args[i + 1])
        del args[i : i + 2]
    if len(args) != 2:
        print(USAGE, file=sys.stderr)
        return 2
    if url is None and Path(args[0]).is_dir():
        print("a local directory needs --url, the store URL (ending in /) it is served from",
              file=sys.stderr)
        return 2
    policy = Policy(allow_private_hosts=private)
    try:
        store = zarrify(args[0], url, checksums=checksums, policy=policy,
                           connections=connections, byte_cost=byte_cost, workers=workers)
    except Rejected as e:
        print(f"rejected: {e}", file=sys.stderr)
        return 3
    except ResolutionError as e:  # the reader policy refused the source
        print(f"error: {e}", file=sys.stderr)
        return 1
    try:  # the archive's sources are checked against the policy the input was read under
        store.save(args[1], policy=policy)
    except ResolutionError as e:  # a source a reader under that policy could not read
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(store.info))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
